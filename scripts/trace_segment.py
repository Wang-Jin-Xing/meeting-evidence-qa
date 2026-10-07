"""证据片段行踪追踪:一句原文为什么没成为证据。

用法(仓库根目录):
    python scripts/trace_segment.py --recording 7 --keyword "资质文件"
    python scripts/trace_segment.py --recording 7 --keyword "资质文件" \\
        --question "注册证什么时候补齐?"
    python scripts/trace_segment.py --segment-id 120
    python scripts/trace_segment.py --recording 7 --keyword "资质文件" \\
        --query-log-id 42

--query-log-id 是修饰符,必须和定位方式一起用(--segment-id,或
--recording 配 --keyword):它去复盘某次历史问答的候选,回答
"这条片段当时在不在候选里"。单独给 --query-log-id 无法定位片段。

做什么:按「原文 → 索引行 → 排名」三层追一个片段的行踪,并把它归到
五支病因里的某一支(见 docs/EVAL_BASELINE.md 的分层排查)。

为什么用"按字找"而不是"按意思找":排查要回答的是"这张纸条在不在",
这件事不该让模型来告诉你 —— 分数的解释有歧义,"这几个字在不在文本里"
没有。所以这里用文本包含匹配,不走向量。

只读声明(评审约定):
- 只 SELECT:不写库、不改发布指针、不建索引、不动任何数据;
- 调用 Embedding 只为把问题变成向量,不经 call_accounting 记账 ——
  诊断不是产品路径,记账会污染成本口径;
- Provider 为 mock 时显式告警:mock 向量下排名不具参考意义。
"""
import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.models.transcript import EmbeddingChunk  # noqa: E402


# ---- 结论代号(五支病因)--------------------------------------------------
# 命名与讲解口径一一对应:关①=没翻到,关②=翻到了但被门槛拦,关④=翻到没用对。
V_TEXT_ABSENT = "关①-1 原文不存在"
V_INDEX_MISSING = "关①-2 索引未建"
V_VERSION_MISMATCH = "关①-3a 不属于当前门牌号的版本"
V_INACTIVE = "关①-3b 当前版本的索引被关掉了"
V_PERMISSION = "关①-3c 权限或归属不匹配"
V_RANKED_OUT = "关①-4 排名掉队"
V_BELOW_THRESHOLD = "关②-1 差一点点"
V_PASSED = "已过线(问题不在关①关②,去看答案归属)"
V_RANK_UNKNOWN = "无法判定名次(未提供 --question)"


@dataclass
class Context:
    """录音侧上下文:门牌号与归属。"""

    recording_id: int
    meeting_id: int
    org_id: int
    meeting_title: str
    active_transcript_version_id: int | None
    active_search_version_id: int | None


@dataclass
class TextHit:
    """原文命中:一条 transcript_segment。"""

    segment_id: int
    version_id: int
    version_no: int
    source_type: str
    seq: int
    start_ms: int | None
    end_ms: int | None
    speaker: str | None
    text: str


@dataclass
class ChunkState:
    """这条片段在 embedding_chunk 里的行状态。"""

    chunk_id: int
    version_id: int
    org_id: int
    meeting_id: int
    is_active: bool
    has_embedding: bool


@dataclass
class Trace:
    """一条片段的完整行踪。"""

    hit: TextHit
    chunk: ChunkState | None = None
    rank: int | None = None
    similarity: float | None = None
    total_ranked: int | None = None
    in_query_log_candidates: bool | None = None


def like_pattern(keyword: str) -> str:
    """把关键词转成安全的包含匹配模式(转义 \\ % _ )。"""
    for ch in ("\\", "%", "_"):
        keyword = keyword.replace(ch, "\\" + ch)
    return f"%{keyword}%"


# ---- 查询层 --------------------------------------------------------------


def load_context(conn: Connection, recording_id: int) -> Context | None:
    row = conn.execute(
        text(
            "SELECT r.id AS recording_id, r.meeting_id, m.org_id, m.title,"
            " r.active_transcript_version_id, r.active_search_version_id"
            " FROM recording r JOIN meeting m ON m.id = r.meeting_id"
            " WHERE r.id = :rid"
        ),
        {"rid": recording_id},
    ).first()
    if row is None:
        return None
    return Context(
        recording_id=row.recording_id,
        meeting_id=row.meeting_id,
        org_id=row.org_id,
        meeting_title=row.title,
        active_transcript_version_id=row.active_transcript_version_id,
        active_search_version_id=row.active_search_version_id,
    )


def find_text_hits(
    conn: Connection, recording_id: int, keyword: str
) -> list[TextHit]:
    """按文本包含匹配找原文。不注入权限 —— 诊断要看到全貌。"""
    rows = conn.execute(
        text(
            "SELECT s.id, s.version_id, v.version_no, v.source_type, s.seq,"
            " s.start_ms, s.end_ms, s.speaker, s.text"
            " FROM transcript_segment s"
            " JOIN transcript_version v ON v.id = s.version_id"
            " WHERE v.recording_id = :rid"
            " AND s.text LIKE :kw ESCAPE '\\'"
            " ORDER BY v.version_no, s.seq"
        ),
        {"rid": recording_id, "kw": like_pattern(keyword)},
    ).all()
    return [
        TextHit(
            segment_id=r.id,
            version_id=r.version_id,
            version_no=r.version_no,
            source_type=str(r.source_type),
            seq=r.seq,
            start_ms=r.start_ms,
            end_ms=r.end_ms,
            speaker=r.speaker,
            text=r.text,
        )
        for r in rows
    ]


def load_hit_by_segment_id(conn: Connection, segment_id: int) -> TextHit | None:
    row = conn.execute(
        text(
            "SELECT s.id, s.version_id, v.version_no, v.source_type, s.seq,"
            " s.start_ms, s.end_ms, s.speaker, s.text"
            " FROM transcript_segment s"
            " JOIN transcript_version v ON v.id = s.version_id"
            " WHERE s.id = :sid"
        ),
        {"sid": segment_id},
    ).first()
    if row is None:
        return None
    return TextHit(
        segment_id=row.id,
        version_id=row.version_id,
        version_no=row.version_no,
        source_type=str(row.source_type),
        seq=row.seq,
        start_ms=row.start_ms,
        end_ms=row.end_ms,
        speaker=row.speaker,
        text=row.text,
    )


def load_chunk_states(
    conn: Connection, segment_ids: list[int]
) -> dict[int, ChunkState]:
    """取这些片段在 embedding_chunk 里的行(可能一条都没有)。"""
    if not segment_ids:
        return {}
    rows = conn.execute(
        select(
            EmbeddingChunk.id,
            EmbeddingChunk.segment_id,
            EmbeddingChunk.version_id,
            EmbeddingChunk.org_id,
            EmbeddingChunk.meeting_id,
            EmbeddingChunk.is_active,
            EmbeddingChunk.embedding.is_not(None),
        ).where(EmbeddingChunk.segment_id.in_(segment_ids))
    ).all()
    out: dict[int, ChunkState] = {}
    for r in rows:
        out[r.segment_id] = ChunkState(
            chunk_id=r.id,
            version_id=r.version_id,
            org_id=r.org_id,
            meeting_id=r.meeting_id,
            is_active=bool(r.is_active),
            has_embedding=bool(r[6]),
        )
    return out


def rank_segments(
    conn: Connection, version_id: int, question_vec: list[float]
) -> list[tuple[int, float]]:
    """按相似度给该版本全部 chunk 排名(不带权限)。

    返回 [(segment_id, 相似度)] 降序。名次 = 下标 + 1。
    不带权限是刻意的:权限与排名是两种不同的病,混在一起会看不清。
    """
    distance = EmbeddingChunk.embedding.cosine_distance(question_vec)
    rows = conn.execute(
        select(EmbeddingChunk.segment_id, distance.label("distance"))
        .where(EmbeddingChunk.version_id == version_id)
        .where(EmbeddingChunk.embedding.is_not(None))
        .order_by(distance)
    ).all()
    return [(r.segment_id, 1.0 - float(r.distance)) for r in rows]


def load_query_log(conn: Connection, log_id: int) -> dict | None:
    row = conn.execute(
        text(
            "SELECT id, question, retrieved_chunk_ids, relevant_candidate_count,"
            " cited_segment_ids, has_sufficient_evidence, is_mock"
            " FROM query_log WHERE id = :lid"
        ),
        {"lid": log_id},
    ).first()
    if row is None:
        return None
    return {
        "id": row.id,
        "question": row.question,
        "retrieved_chunk_ids": list(row.retrieved_chunk_ids or []),
        "relevant_candidate_count": row.relevant_candidate_count,
        "cited_segment_ids": list(row.cited_segment_ids or []),
        "has_sufficient_evidence": row.has_sufficient_evidence,
        "is_mock": row.is_mock,
    }


def embed_question(question: str) -> tuple[list[float], bool, str]:
    """把问题变成向量。返回 (向量, 是否 mock, 模型名)。

    延迟导入并在失败时抛出 —— 让主流程可以降级为"只做前两层"。
    """
    from app.ai.registry import build_providers

    _, _, embed = build_providers()
    vec = embed.embed_batch([question])[0]
    return vec, bool(getattr(embed, "is_mock", False)), str(
        getattr(embed, "model_name", "unknown")
    )


# ---- 判定层(纯函数,可单测)----------------------------------------------


def classify(trace: Trace, ctx: Context, *, threshold: float, top_k: int) -> tuple[str, str]:
    """把一个片段的行踪归到五支病因里的一支。

    返回 (结论代号, 补充说明)。纯函数:不碰数据库、不读全局配置,
    所有依赖都由参数传入 —— 这样判定口径固定,二十道题的归类不会漂。
    """
    if ctx.active_search_version_id is None:
        return V_INDEX_MISSING, (
            "这条录音还没有任何已发布的检索版本(检索门牌号为空)——"
            "索引阶段从未成功发布过,所以检索里一条都取不到。"
        )

    if trace.chunk is None:
        return V_INDEX_MISSING, (
            "原文在库里,但 embedding_chunk 里没有它的索引行 —— "
            "索引压根没建到这个片段(可能是建到一半中断,或版本未被纳入重建)。"
        )

    c = trace.chunk
    if not c.has_embedding:
        return V_INDEX_MISSING, "索引行在,但向量列为空 —— 这一行没算成功。"

    if c.version_id != ctx.active_search_version_id:
        return V_VERSION_MISMATCH, (
            f"索引行属于版本 {c.version_id},而当前检索门牌号是"
            f" {ctx.active_search_version_id} —— 它不属于当前生效版本"
            "(旧版本已被取代,或新版本尚未发布)。"
        )

    if not c.is_active:
        return V_INACTIVE, (
            "版本号对得上,但生效开关是关的 —— 这是异常状态:"
            "同门牌号版本的索引本应在发布时一并打开。"
        )

    if c.org_id != ctx.org_id or c.meeting_id != ctx.meeting_id:
        return V_PERMISSION, (
            f"索引行挂在 机构{c.org_id}/会谈{c.meeting_id},"
            f"而本录音属于 机构{ctx.org_id}/会谈{ctx.meeting_id} —— 权限或归属不匹配。"
        )

    # 门是开的。接下来只看排名。
    if trace.rank is None:
        return V_RANK_UNKNOWN, (
            "门是开的:原文在、索引行在、版本与权限都对。"
            "要判定是「排名掉队」还是「差一点点」,需要加 --question 算相似度。"
        )

    if trace.similarity is None:
        return V_RANK_UNKNOWN, "排名数据不完整。"

    gap = threshold - trace.similarity
    if trace.rank > top_k:
        return V_RANKED_OUT, (
            f"门是开的,但它排第 {trace.rank} 名(共 {trace.total_ranked} 条),"
            f"在取前 {top_k} 名之外 —— 压根没进候选。"
            f"相似度 {trace.similarity:.4f},距门槛 {gap:+.4f}。"
        )

    if trace.similarity < threshold:
        tail = (
            "差距很小,属于「差一点点」—— 值得看一眼门槛。"
            if gap <= 0.05
            else "差距不小,更像「表示不准」而不是「门槛太高」—— 调门槛未必救得回来。"
        )
        return V_BELOW_THRESHOLD, (
            f"它排第 {trace.rank} 名(共 {trace.total_ranked} 条),进了候选,"
            f"但相似度 {trace.similarity:.4f} 低于门槛 {threshold:.4f}"
            f"(差 {gap:.4f})。{tail}"
        )

    return V_PASSED, (
        f"它排第 {trace.rank} 名,相似度 {trace.similarity:.4f},"
        f"已过门槛 {threshold:.4f} —— 属于候选。"
        "如果答案仍然错,病不在关①关②,而在「翻到了没用对」(答案归属)。"
    )


# ---- 输出层 --------------------------------------------------------------


def _ms(ms: int | None) -> str:
    if ms is None:
        return "??:??"
    return f"{ms // 60000:02d}:{(ms % 60000) // 1000:02d}"


def _short(text_: str, width: int = 46) -> str:
    flat = " ".join(text_.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def render(
    ctx: Context,
    traces: list[Trace],
    *,
    keyword: str | None,
    threshold: float,
    top_k: int,
    ranked: list[tuple[int, float]] | None,
    embed_note: str | None,
    query_log: dict | None,
) -> str:
    lines: list[str] = []
    add = lines.append
    add("─" * 66)
    add(f"证据片段行踪 · 录音 {ctx.recording_id} · 会谈 {ctx.meeting_id}"
        f"「{ctx.meeting_title}」· 机构 {ctx.org_id}")
    tp = ctx.active_transcript_version_id
    sp = ctx.active_search_version_id
    flag = "一致" if tp == sp else "不一致(已发布但索引还没跟上)"
    add(f"当前门牌号: 展示 {tp} / 检索 {sp}  [{flag}]")
    if keyword:
        add(f"关键词「{keyword}」 · 门槛 {threshold:.4f} · top_k {top_k}")
    add("")

    if not traces:
        add("[1] 原文(transcript_segment)")
        add(f"    没搜到含「{keyword}」的片段。")
        add("")
        add(f"[结论] {V_TEXT_ABSENT}")
        add("    三种可能,按这个顺序排除:")
        add("      ① 你搜的词跟原文措辞不同(搜「注册证」,原文写「资质文件」)"
            " → 换个词再搜");
        add("      ② 正确信息横跨两个片段,被从中间切断了 → 看这版切片的边界;")
        add("      ③ 这一版压根没转写完 → 看 job_stage 状态。")
        add("─" * 66)
        return "\n".join(lines)

    for i, t in enumerate(traces, 1):
        add(f"[1] 原文(transcript_segment)  #{i}")
        add(f"    SEG-{t.hit.segment_id}  v{t.hit.version_no}"
            f"({t.hit.source_type})  第 {t.hit.seq} 条"
            f"  {_ms(t.hit.start_ms)}~{_ms(t.hit.end_ms)}")
        add(f"    「{_short(t.hit.text)}」")
        add("")

        c = t.chunk
        add("[2] 索引行(embedding_chunk)")
        if c is None:
            add("    没有这一行。")
        else:
            ok_v = "✓" if c.version_id == sp else "✗"
            ok_a = "✓" if c.is_active else "✗"
            ok_p = "✓" if (c.org_id == ctx.org_id and c.meeting_id == ctx.meeting_id) else "✗"
            ok_e = "✓" if c.has_embedding else "✗"
            add(f"    chunk#{c.chunk_id}  版本={c.version_id} 门牌号={sp} {ok_v}"
                f"  生效={c.is_active} {ok_a}  归属=机构{c.org_id}/会谈{c.meeting_id} {ok_p}"
                f"  向量={ok_e}")
        add("")

        add("[3] 排名(相似度)")
        if query_log is not None:
            cand = query_log["retrieved_chunk_ids"]
            inside = c is not None and c.chunk_id in cand
            add(f"    历史记录 query_log#{query_log['id']}: 阈值前候选 {len(cand)} 条,"
                f"过线 {query_log['relevant_candidate_count']} 条,"
                f"引用片段 {query_log['cited_segment_ids']}")
            add(f"    这一条的 chunk {'在' if inside else '不在'}当时的候选里"
                f" → {'检索到了' if inside else '压根没检索到'}")
        elif t.rank is not None:
            add(f"    第 {t.rank} 名 / 共 {t.total_ranked} 条,"
                f"相似度 {t.similarity:.4f}(门槛 {threshold:.4f})")
            if ranked:
                top = ranked[: max(top_k, 1)]
                parts = [
                    f"{sim:.3f}{'←目标' if sid == t.hit.segment_id else ''}"
                    for sid, sim in top
                ]
                add(f"    前 {len(top)} 名: " + " / ".join(parts))
        else:
            add("    未计算(加 --question 可算名次与分数)。")
        if embed_note:
            add(f"    注意: {embed_note}")
        add("")

        code, note = classify(t, ctx, threshold=threshold, top_k=top_k)
        add(f"[结论] {code}")
        for chunk in _wrap(note, 62):
            add(f"    {chunk}")
        add("")

    add("─" * 66)
    return "\n".join(lines)


def _wrap(s: str, width: int) -> list[str]:
    return [s[i : i + width] for i in range(0, len(s), width)] or [""]


# ---- 入口 ----------------------------------------------------------------


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="证据片段行踪追踪:一句原文为什么没成为证据(只读,不改数据)"
    )
    p.add_argument("--recording", type=int, help="录音 id")
    p.add_argument("--segment-id", type=int, help="直接指定转写片段 id(更精确)")
    p.add_argument("--keyword", help="按文本包含匹配找原文")
    p.add_argument("--question", help="顺便算相似度排名(会调用一次 Embedding)")
    p.add_argument("--query-log-id", type=int, help="复盘一次历史问答(query_log id)")
    p.add_argument("--threshold", type=float, help="覆盖相关性门槛")
    p.add_argument("--top-k", type=int, help="覆盖候选条数")
    return p.parse_args(argv)


def make_trace_engine():
    """诊断专用引擎:带连接超时。

    不复用 app.core.db.engine —— 它没有连接超时,库不可达时表现为长时间挂住,
    而诊断工具应当立刻告诉"连不上",而不是让你对着黑屏等。
    """
    return create_engine(
        get_settings().database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 5},
    )


def main(argv=None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    threshold = (
        args.threshold if args.threshold is not None else settings.qa_evidence_threshold
    )
    top_k = args.top_k if args.top_k is not None else settings.qa_top_k

    if args.segment_id is None and (args.recording is None or not args.keyword):
        print(
            "定位方式:--segment-id <片段id>,或 --recording <录音id> --keyword <关键词>。",
            file=sys.stderr,
        )
        return 2

    trace_engine = make_trace_engine()
    try:
        return _run(trace_engine, args, threshold=threshold, top_k=top_k)
    except OperationalError as exc:
        print(
            "连不上数据库:诊断脚本只读,但需要库在线。\n"
            f"  地址: {settings.database_url}\n"
            f"  错误: {exc}",
            file=sys.stderr,
        )
        return 1
    finally:
        trace_engine.dispose()


def _run(trace_engine, args, *, threshold: float, top_k: int) -> int:
    with trace_engine.connect() as conn:
        query_log = load_query_log(conn, args.query_log_id) if args.query_log_id else None

        # 定位:优先 segment-id,其次 keyword。
        if args.segment_id is not None:
            hit = load_hit_by_segment_id(conn, args.segment_id)
            if hit is None:
                print(f"没有 SEG-{args.segment_id} 这个片段。", file=sys.stderr)
                return 1
            row = conn.execute(
                text("SELECT recording_id FROM transcript_version WHERE id = :v"),
                {"v": hit.version_id},
            ).first()
            recording_id = row.recording_id if row else args.recording
            hits = [hit]
        else:
            recording_id = args.recording
            hits = find_text_hits(conn, recording_id, args.keyword) if args.keyword else []

        ctx = load_context(conn, recording_id) if recording_id else None
        if ctx is None:
            print(f"找不到录音 {recording_id}。", file=sys.stderr)
            return 1

        ranked = None
        embed_note = None
        if args.question:
            try:
                vec, is_mock, model_name = embed_question(args.question)
                ranked = rank_segments(conn, ctx.active_search_version_id, vec)
                if is_mock:
                    embed_note = (
                        f"当前 Embedding 是 mock({model_name}),"
                        "向量不表示真实语义 —— 排名与分数仅供参考。"
                    )
                else:
                    embed_note = None
            except Exception as exc:  # 模型不可用不该让诊断整体失败
                embed_note = f"无法计算相似度,已跳过第三层: {exc}"

        chunk_map = load_chunk_states(conn, [h.segment_id for h in hits])
        traces: list[Trace] = []
        for h in hits:
            t = Trace(hit=h, chunk=chunk_map.get(h.segment_id))
            if ranked is not None:
                t.total_ranked = len(ranked)
                for idx, (sid, sim) in enumerate(ranked, 1):
                    if sid == h.segment_id:
                        t.rank, t.similarity = idx, sim
                        break
            if query_log is not None and t.chunk is not None:
                t.in_query_log_candidates = t.chunk.chunk_id in query_log[
                    "retrieved_chunk_ids"
                ]
            traces.append(t)

        # 关键词模式下,只保留属于当前检索版本的命中,避免旧版本噪声淹没结论
        if args.keyword and len(traces) > 1:
            current = [
                t for t in traces
                if t.chunk is not None
                and t.chunk.version_id == ctx.active_search_version_id
            ]
            if current:
                traces = current

        print(
            render(
                ctx,
                traces,
                keyword=args.keyword,
                threshold=threshold,
                top_k=top_k,
                ranked=ranked,
                embed_note=embed_note,
                query_log=query_log,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
