"""四阶段执行器:prepare → transcribe → minutes → index。

统一约定:
- 外部模型调用一律在事务之外(不持数据库锁调模型);
- 每个阶段的所有业务产物与阶段成功状态在同一短事务提交(leased_transaction);
- 执行器幂等:重复执行不产生重复版本/纪要/索引(对账 + 唯一约束);
- 时间信息只来自切片边界(prepare 计算),模型不产生时间;
- 外部调用经 _call_provider 包装:单次调用超时 + 阶段执行期限(deadline),
  超时抛 ProviderResultUnknownError(结果未知),超期抛 StageDeadlineError
  (可重试)——防止心跳活着但任务永久卡在外部调用上。
"""
import concurrent.futures as cf
import hashlib
import time
import wave
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.ai.providers import (
    ASRProvider,
    EmbedProvider,
    LLMProvider,
    ProviderResultUnknownError,
    SegmentForLLM,
)
from app.services.minutes_validator import validate_minutes, MinutesValidationError
from app.worker.lease import LeaseLostError, leased_transaction


class StageError(Exception):
    """阶段执行失败(可重试),携带用户可读原因。"""


class StageDeadlineError(Exception):
    """阶段执行超期:放弃本次执行并回队重试(心跳随之停止)。"""


def _call_provider(func, timeout: float | None, *args, **kwargs):
    """外部调用超时兜底:timeout 秒未返回 → ProviderResultUnknownError。

    timeout=None 直接调用(禁用)。线程无法强杀,超时后泄漏线程最终自行
    结束(能力边界);真实 Provider 应自带网络超时,本层为最后防线。
    """
    if timeout is None:
        return func(*args, **kwargs)
    if timeout <= 0:
        raise StageDeadlineError("阶段执行期限已耗尽,不再发出外部调用")
    executor = cf.ThreadPoolExecutor(max_workers=1)
    try:
        fut = executor.submit(func, *args, **kwargs)
        try:
            return fut.result(timeout=timeout)
        except cf.TimeoutError as e:
            raise ProviderResultUnknownError(
                f"外部调用超时({timeout:.1f}s),结果未知"
            ) from e
    finally:
        executor.shutdown(wait=False)


def _call_timeout(provider_timeout: float, deadline: float | None) -> float | None:
    """单次调用超时 = min(单调用超时, 阶段剩余期限)。"""
    if deadline is None:
        return provider_timeout if provider_timeout > 0 else None
    remaining = deadline - time.monotonic()
    if provider_timeout <= 0:
        return remaining
    return min(provider_timeout, remaining)


@dataclass
class JobContext:
    job_id: int
    recording_id: int
    meeting_id: int
    org_id: int
    file_path: str


def load_context(eng: Engine, job_id: int) -> JobContext:
    with eng.connect() as conn:
        row = conn.execute(
            text(
                "SELECT j.id, r.id, r.meeting_id, m.org_id, r.file_path"
                " FROM processing_job j"
                " JOIN recording r ON r.id = j.recording_id"
                " JOIN meeting m ON m.id = r.meeting_id"
                " WHERE j.id = :job_id"
            ),
            {"job_id": job_id},
        ).fetchone()
    if row is None:
        raise StageError(f"job {job_id} 上下文缺失")
    return JobContext(
        job_id=row[0], recording_id=row[1], meeting_id=row[2],
        org_id=row[3], file_path=row[4],
    )


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _mark_stage(eng, job_id, epoch, stage, status: str, artifact_ref: dict | None = None):
    """更新阶段状态(小事务,租约守卫)。"""
    with leased_transaction(eng, job_id, epoch) as conn:
        conn.execute(
            text(
                "UPDATE job_stage SET status = :st,"
                " started_at = COALESCE(started_at, clock_timestamp()),"
                " artifact_ref = COALESCE(:ref, artifact_ref)"
                " WHERE job_id = :job_id AND stage_name = :stage"
            ),
            {"st": status, "ref": artifact_ref and _to_json(artifact_ref),
             "job_id": job_id, "stage": stage},
        )


def _to_json(obj) -> str | None:
    import json

    return json.dumps(obj, ensure_ascii=False) if obj is not None else None


# ---------- prepare ----------

def _read_wav_slices(path: str, slice_seconds: int):
    """读取 WAV 并按固定时长切片。返回 (duration_ms, [(start_ms, end_ms, bytes)])。

    切片边界即片段级时间信息的唯一来源(设计约定,模型不得产生时间)。
    """
    try:
        with wave.open(path, "rb") as w:
            rate = w.getframerate()
            nframes = w.getnframes()
            channels = w.getnchannels()
            sampwidth = w.getsampwidth()
            duration_ms = nframes * 1000 // rate
            bytes_per_frame = max(1, sampwidth * channels)
            slice_frames = rate * slice_seconds
            slices = []
            for start_f in range(0, nframes, slice_frames):
                w.setpos(start_f)
                n = min(slice_frames, nframes - start_f)
                data = w.readframes(n)
                start_ms = start_f * 1000 // rate
                end_ms = (start_f + n) * 1000 // rate
                slices.append((start_ms, end_ms, data))
            return duration_ms, slices
    except (wave.Error, EOFError) as e:
        raise StageError(f"音频准备失败(当前仅支持 WAV): {e}") from e


def run_prepare(eng, ctx: JobContext, epoch: int, slice_seconds: int, should_abort):
    """切片并建分片行;分片进度自此以数据库为准。"""
    # 对账:分片行已存在(同事务产物)则直接补标成功
    with eng.connect() as conn:
        n = conn.execute(
            text("SELECT count(*) FROM transcribe_slice WHERE job_id = :j"),
            {"j": ctx.job_id},
        ).scalar()
    if n > 0:
        _mark_stage(eng, ctx.job_id, epoch, "prepare", "succeeded")
        return

    duration_ms, slices = _read_wav_slices(ctx.file_path, slice_seconds)
    if not slices:
        raise StageError("音频为空或时长为零")

    rows = []
    for idx, (start_ms, end_ms, data) in enumerate(slices):
        # input_hash 含分片序号:相同字节在不同位置的切片不共享缓存
        # (真实 ASR 依赖上下文,位置不同结果可能不同)
        input_hash = _sha(data + f"|slice_seconds={slice_seconds}|idx={idx}".encode())
        rows.append({
            "job_id": ctx.job_id, "slice_index": idx,
            "start_ms": start_ms, "end_ms": end_ms,
            "input_hash": input_hash,
        })

    # 产物与阶段成功状态同事务提交
    with leased_transaction(eng, ctx.job_id, epoch) as conn:
        for r in rows:
            conn.execute(
                text(
                    "INSERT INTO transcribe_slice"
                    " (job_id, slice_index, start_ms, end_ms, status, input_hash)"
                    " VALUES (:job_id, :slice_index, :start_ms, :end_ms, 'pending', :input_hash)"
                    " ON CONFLICT DO NOTHING"
                ),
                r,
            )
        conn.execute(
            text(
                "UPDATE job_stage SET status = 'succeeded',"
                " finished_at = clock_timestamp(),"
                " artifact_ref = :ref"
                " WHERE job_id = :job_id AND stage_name = 'prepare'"
            ),
            {"ref": _to_json({"duration_ms": duration_ms, "slice_seconds": slice_seconds,
                              "slice_count": len(rows)}),
             "job_id": ctx.job_id},
        )
        conn.execute(
            text("UPDATE recording SET duration_ms = :d WHERE id = :id"),
            {"d": duration_ms, "id": ctx.recording_id},
        )


# ---------- transcribe ----------

def run_transcribe(eng, ctx: JobContext, epoch: int, asr: ASRProvider, should_abort,
                   provider_timeout: float = 60.0, deadline: float | None = None):
    """逐片转写:成功片写回并可复用;全部完成后单事务发布版本。"""
    # 对账:版本已发布
    with eng.connect() as conn:
        active = conn.execute(
            text("SELECT active_transcript_version_id FROM recording WHERE id = :id"),
            {"id": ctx.recording_id},
        ).scalar()
    if active is not None:
        _mark_stage(eng, ctx.job_id, epoch, "transcribe", "succeeded")
        return

    with eng.connect() as conn:
        slices = conn.execute(
            text(
                "SELECT id, slice_index, input_hash FROM transcribe_slice"
                " WHERE job_id = :j ORDER BY slice_index"
            ),
            {"j": ctx.job_id},
        ).fetchall()
    if not slices:
        raise StageError("缺少分片数据(prepare 未完成?)")

    for sid, slice_index, input_hash in slices:
        if should_abort():
            raise LeaseLostError("租约丢失,放弃任务")
        with eng.connect() as conn:
            status = conn.execute(
                text("SELECT status FROM transcribe_slice WHERE id = :id"),
                {"id": sid},
            ).scalar()
        if status == "succeeded":
            continue  # 成功片复用,不重复调用

        # 结果缓存:同一逻辑任务内相同输入+配置的成功调用直接复用
        # (设计约定:缓存按业务范围隔离,不跨任务合并相同内容)
        cached = None
        with eng.connect() as conn:
            cached = conn.execute(
                text(
                    "SELECT output_ref FROM model_run"
                    " WHERE input_hash = :h AND stage = 'transcribe'"
                    " AND job_id = :job_id AND status = 'succeeded'"
                    " ORDER BY id LIMIT 1"
                ),
                {"h": input_hash, "job_id": ctx.job_id},
            ).fetchone()
        if cached is not None:
            out = cached[0]
        else:
            audio = _read_slice_audio(eng, ctx, slice_index)
            # 两阶段记录:前置 unknown(结果未知),返回后补 succeeded;
            # 超时/断连类异常保留 unknown(结果未知),明确失败才记 failed
            run_id = _begin_model_run(
                eng, ctx, stage="transcribe", provider=asr.name,
                model_name=asr.model_name, input_hash=input_hash,
            )
            try:
                result = _call_provider(
                    asr.transcribe_slice,
                    _call_timeout(provider_timeout, deadline),
                    audio, slice_index,
                )
            except ProviderResultUnknownError:
                raise  # 结果未知:保留 unknown,重发时同键留痕
            except Exception:
                _fail_model_run(eng, run_id)
                raise
            out = {"speaker": result.speaker, "text": result.text}
            _finish_model_run(eng, run_id, out)
        # 分片结果短事务提交(租约守卫)
        with leased_transaction(eng, ctx.job_id, epoch) as conn:
            conn.execute(
                text(
                    "UPDATE transcribe_slice SET status = 'succeeded',"
                    " output_ref = :out, error = NULL WHERE id = :id"
                ),
                {"out": _to_json(out), "id": sid},
            )

    # 发布版本:版本行 + 全部片段 + active 指针,单事务(未发布前不可见)
    with leased_transaction(eng, ctx.job_id, epoch) as conn:
        version_no = conn.execute(
            text(
                "SELECT count(*) + 1 FROM transcript_version WHERE recording_id = :rid"
            ),
            {"rid": ctx.recording_id},
        ).scalar()
        version_id = conn.execute(
            text(
                "INSERT INTO transcript_version"
                " (recording_id, version_no, source_type)"
                " VALUES (:rid, :no, 'asr') RETURNING id"
            ),
            {"rid": ctx.recording_id, "no": version_no},
        ).scalar()
        conn.execute(
            text(
                "INSERT INTO transcript_segment"
                " (version_id, seq, start_ms, end_ms, speaker, text, timestamp_precision)"
                " SELECT :vid, s.slice_index + 1, s.start_ms, s.end_ms,"
                "  (s.output_ref->>'speaker'), (s.output_ref->>'text'),"
                "  'segment_level_from_slice'"
                " FROM transcribe_slice s WHERE s.job_id = :j"
            ),
            {"vid": version_id, "j": ctx.job_id},
        )
        conn.execute(
            text("UPDATE recording SET active_transcript_version_id = :vid WHERE id = :rid"),
            {"vid": version_id, "rid": ctx.recording_id},
        )
        conn.execute(
            text(
                "UPDATE job_stage SET status = 'succeeded', finished_at = clock_timestamp()"
                " WHERE job_id = :job_id AND stage_name = 'transcribe'"
            ),
            {"job_id": ctx.job_id},
        )


def _read_slice_audio(eng, ctx: JobContext, slice_index: int) -> bytes:
    """从原始 WAV 提取分片字节(音频文件为不可变原件,只读)。"""
    with eng.connect() as conn:
        row = conn.execute(
            text(
                "SELECT start_ms, end_ms FROM transcribe_slice"
                " WHERE job_id = :j AND slice_index = :i"
            ),
            {"j": ctx.job_id, "i": slice_index},
        ).fetchone()
    start_ms, end_ms = row
    with wave.open(ctx.file_path, "rb") as w:
        rate = w.getframerate()
        start_f = start_ms * rate // 1000
        end_f = end_ms * rate // 1000
        w.setpos(start_f)
        return w.readframes(end_f - start_f)


# ---------- minutes ----------

def run_minutes(eng, ctx: JobContext, epoch: int, llm: LLMProvider, should_abort,
                provider_timeout: float = 60.0, deadline: float | None = None):
    """基于 active 转写版本生成结构化纪要(经层1校验),幂等。"""
    # 对账:本 job 已产出纪要
    with eng.connect() as conn:
        existing = conn.execute(
            text("SELECT id FROM minutes_version WHERE job_id = :j"),
            {"j": ctx.job_id},
        ).fetchone()
    if existing is not None:
        _mark_stage(eng, ctx.job_id, epoch, "minutes", "succeeded")
        return

    if should_abort():
        raise LeaseLostError("租约丢失,放弃任务")

    segments = _active_segments(eng, ctx.recording_id)
    if not segments:
        raise StageError("无可用的转写片段")

    input_hash = _sha(
        llm.prompt_version.encode()
        + "|".join(f"{s[1]}:{s[3]}" for s in segments).encode("utf-8")
    )
    cached = None
    with eng.connect() as conn:
        cached = conn.execute(
            text(
                "SELECT id, output_ref FROM model_run"
                " WHERE input_hash = :h AND stage = 'minutes'"
                " AND job_id = :job_id AND status = 'succeeded'"
                " ORDER BY id LIMIT 1"
            ),
            {"h": input_hash, "job_id": ctx.job_id},
        ).fetchone()
    if cached is not None:
        run_id, content = cached[0], cached[1]
    else:
        # 两阶段记录:前置 unknown(结果未知),返回后补 succeeded
        run_id = _begin_model_run(
            eng, ctx, stage="minutes", provider=llm.name,
            model_name=llm.model_name, input_hash=input_hash,
            prompt_version=llm.prompt_version,
        )
        try:
            draft = _call_provider(
                llm.generate_minutes,
                _call_timeout(provider_timeout, deadline),
                [SegmentForLLM(segment_id=s[0], seq=s[1], speaker=s[2], text=s[3])
                 for s in segments],
            )
        except ProviderResultUnknownError:
            raise  # 结果未知:保留 unknown
        except Exception:
            _fail_model_run(eng, run_id)
            raise
        content = {
            "items": [
                {
                    "type": it.type, "text": it.text, "certainty": it.certainty,
                    "owner": it.owner, "due_date": it.due_date,
                    "evidence_segment_ids": it.evidence_segment_ids,
                    "needs_confirmation": it.needs_confirmation,
                }
                for it in draft.items
            ]
        }
        _finish_model_run(eng, run_id, content)

    # 层1校验:引用存在 + 空值策略;失败则本阶段失败(不落库)
    try:
        validate_minutes(content, valid_segment_ids={s[0] for s in segments})
    except MinutesValidationError as e:
        raise StageError(f"纪要校验失败: {e}") from e

    with leased_transaction(eng, ctx.job_id, epoch) as conn:
        version_no = conn.execute(
            text("SELECT count(*) + 1 FROM minutes_version WHERE meeting_id = :mid"),
            {"mid": ctx.meeting_id},
        ).scalar()
        conn.execute(
            text(
                "INSERT INTO minutes_version"
                " (meeting_id, job_id, version_no, source_type, status,"
                "  content, prompt_version, model_run_id)"
                " VALUES (:mid, :jid, :no, 'ai_draft', 'pending_review',"
                "  CAST(:content AS jsonb), :pv, :run_id)"
            ),
            {
                "mid": ctx.meeting_id, "jid": ctx.job_id, "no": version_no,
                "content": _to_json(content), "pv": llm.prompt_version,
                "run_id": run_id,
            },
        )
        conn.execute(
            text(
                "UPDATE job_stage SET status = 'succeeded', finished_at = clock_timestamp()"
                " WHERE job_id = :job_id AND stage_name = 'minutes'"
            ),
            {"job_id": ctx.job_id},
        )


def _active_segments(eng, recording_id: int):
    """当前 active 转写版本的片段 (id, seq, speaker, text),按序。"""
    with eng.connect() as conn:
        return conn.execute(
            text(
                "SELECT ts.id, ts.seq, ts.speaker, ts.text"
                " FROM transcript_segment ts"
                " JOIN recording r ON r.active_transcript_version_id = ts.version_id"
                " WHERE r.id = :rid ORDER BY ts.seq"
            ),
            {"rid": recording_id},
        ).fetchall()


# ---------- index ----------

_INDEX_BATCH = 50


def run_index(eng, ctx: JobContext, epoch: int, embed: EmbedProvider, should_abort,
              provider_timeout: float = 60.0, deadline: float | None = None):
    """为 active 转写版本建索引:分批插入(is_active=false),全部完成后原子切换。"""
    with eng.connect() as conn:
        active_search, active_transcript = conn.execute(
            text(
                "SELECT active_search_version_id, active_transcript_version_id"
                " FROM recording WHERE id = :id"
            ),
            {"id": ctx.recording_id},
        ).fetchone()
    if active_transcript is None:
        raise StageError("缺少可索引的转写版本")
    if active_search == active_transcript:
        _mark_stage(eng, ctx.job_id, epoch, "index", "succeeded")
        return

    version_id = active_transcript
    segments = _active_segments(eng, ctx.recording_id)
    existing = {
        row[0] for row in (
            _chunks_for_version(eng, version_id)
        )
    }
    todo = [s for s in segments if s[0] not in existing]

    for i in range(0, len(todo), _INDEX_BATCH):
        if should_abort():
            raise LeaseLostError("租约丢失,放弃任务")
        batch = todo[i : i + _INDEX_BATCH]
        texts = [s[3] for s in batch]
        input_hash = _sha(("|".join(str(s[0]) for s in batch)).encode())
        cached = None
        with eng.connect() as conn:
            cached = conn.execute(
                text(
                    "SELECT output_ref FROM model_run"
                    " WHERE input_hash = :h AND stage = 'index'"
                    " AND job_id = :job_id AND status = 'succeeded'"
                    " ORDER BY id LIMIT 1"
                ),
                {"h": input_hash, "job_id": ctx.job_id},
            ).fetchone()
        if cached is not None:
            vectors = [tuple(v) for v in cached[0]["vectors"]]
        else:
            # 两阶段记录:前置 unknown(结果未知),返回后补 succeeded
            run_id = _begin_model_run(
                eng, ctx, stage="index", provider=embed.name,
                model_name=embed.model_name, input_hash=input_hash,
            )
            try:
                vectors = _call_provider(
                    embed.embed_batch,
                    _call_timeout(provider_timeout, deadline),
                    texts,
                )
            except ProviderResultUnknownError:
                raise  # 结果未知:保留 unknown
            except Exception:
                _fail_model_run(eng, run_id)
                raise
            _finish_model_run(eng, run_id, {"vectors": vectors})
        # 批量写入短事务(租约守卫);is_active=false,发布前检索不可见
        with leased_transaction(eng, ctx.job_id, epoch) as conn:
            for seg, vec in zip(batch, vectors):
                conn.execute(
                    text(
                        "INSERT INTO embedding_chunk"
                        " (segment_id, version_id, org_id, meeting_id, is_active,"
                        "  embedding, text)"
                        " VALUES (:sid, :vid, :org, :mid, false, CAST(:vec AS vector), :text)"
                        " ON CONFLICT (version_id, segment_id) DO NOTHING"
                    ),
                    {"sid": seg[0], "vid": version_id, "org": ctx.org_id,
                     "mid": ctx.meeting_id,
                     "vec": "[" + ",".join(str(x) for x in vec) + "]",
                     "text": seg[3]},
                )

    # 全部完成:原子切换检索版本(指针 + is_active 翻转同事务)。
    # 翻转范围 = 本录音的旧版本 chunk(P2 修复:原为 meeting 级,同会谈
    # 多录音时会误踢其他录音已发布的索引)
    with leased_transaction(eng, ctx.job_id, epoch) as conn:
        conn.execute(
            text(
                "UPDATE embedding_chunk SET is_active = false"
                " WHERE is_active = true AND version_id != :vid"
                " AND version_id IN (SELECT id FROM transcript_version"
                "                    WHERE recording_id = :rid)"
            ),
            {"vid": version_id, "rid": ctx.recording_id},
        )
        conn.execute(
            text("UPDATE embedding_chunk SET is_active = true WHERE version_id = :vid"),
            {"vid": version_id},
        )
        conn.execute(
            text("UPDATE recording SET active_search_version_id = :vid WHERE id = :rid"),
            {"vid": version_id, "rid": ctx.recording_id},
        )
        conn.execute(
            text(
                "UPDATE job_stage SET status = 'succeeded', finished_at = clock_timestamp()"
                " WHERE job_id = :job_id AND stage_name = 'index'"
            ),
            {"job_id": ctx.job_id},
        )


def _chunks_for_version(eng, version_id: int):
    with eng.connect() as conn:
        return conn.execute(
            text("SELECT segment_id FROM embedding_chunk WHERE version_id = :v"),
            {"v": version_id},
        ).fetchall()


def _begin_model_run(eng, ctx: JobContext, *, stage, provider, model_name,
                     input_hash, prompt_version=None, is_mock=True) -> int:
    """调用前置记录:status=unknown(调用即将发出,结果未知)。

    进程死于此后的任意时刻,该行即为"结果未知的调用"审计证据——
    重发是允许的(不宣称避免重复调用/计费),unknown 行如实留痕。
    幂等键为服务商请求级去重键,同键可多行(每次物理调用一行)。
    """
    with eng.begin() as conn:
        return conn.execute(
            text(
                "INSERT INTO model_run"
                " (org_id, job_id, stage, provider, model_name, prompt_version,"
                "  input_hash, status, is_mock, idempotency_key, idempotency_key_sent)"
                " VALUES (:org, :job, :stage, :provider, :model, :pv, :ih,"
                "  'unknown', :mock, :idem, false)"
                " RETURNING id"
            ),
            {
                "org": ctx.org_id, "job": ctx.job_id, "stage": stage,
                "provider": provider, "model": model_name, "pv": prompt_version,
                "ih": input_hash, "mock": is_mock,
                "idem": f"org{ctx.org_id}-job{ctx.job_id}-{stage}-{input_hash}",
            },
        ).scalar()


def _finish_model_run(eng, run_id: int, output_ref) -> None:
    """调用成功:unknown → succeeded,写回输出(独立短事务)。"""
    with eng.begin() as conn:
        conn.execute(
            text(
                "UPDATE model_run SET status = 'succeeded',"
                " output_ref = CAST(:out AS jsonb) WHERE id = :id"
            ),
            {"out": _to_json(output_ref), "id": run_id},
        )


def _fail_model_run(eng, run_id: int) -> None:
    """调用明确失败(Provider 抛异常,结果确定):unknown → failed。
    区别于 unknown(进程死亡,结果未知)。"""
    with eng.begin() as conn:
        conn.execute(
            text("UPDATE model_run SET status = 'failed' WHERE id = :id"),
            {"id": run_id},
        )
