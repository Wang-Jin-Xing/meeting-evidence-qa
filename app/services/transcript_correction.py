"""人工校对转写导入:新增 human_correction 版本,不覆盖原始转写。

用途边界(如实声明):
- 本模块是**数据通路**(评测与后续人工纠错 API 可复用),
  不是人工确认工作流本身;纪要的人工确认/编辑流转仍未实现;
- 纠错只产生新版本 + 逐条 segment_mapping,原版本与其片段保持不可变,
  因此指向旧片段的历史纪要/回答引用仍可解析;
- 索引重建沿用 worker 的发布不变量:分批以 is_active=false 写入,
  全部完成后在**同一事务**里切指针并翻转旧版本;检索永远看不到半成品。
  (worker 侧的租约守卫在这里不适用:离线导入没有并发 worker,
  但仍受费用与预算记账约束。)
"""
import hashlib

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.ai.providers import EmbedProvider
from app.services import call_accounting


class CorrectionError(Exception):
    """人工校对导入前置条件不满足(录音未转写、校对条数不符等)。"""


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def import_corrected_transcript(
    eng: Engine, *, recording_id: int, lines: list[tuple[str | None, str]],
    created_by: int | None = None, source_note: str = "manual_correction",
) -> dict:
    """按校对文本新建 human_correction 版本并发布为当前转写版本。

    lines: [(speaker, text), ...],顺序即 seq(1 起),须与原版片段数一致
    ——校对是修文字,不是重切时间轴;条数不符即报错,避免时间戳错位。
    返回 {version_id, mapped, edited}。
    """
    with eng.connect() as conn:
        base = conn.execute(
            text(
                "SELECT r.active_transcript_version_id, r.meeting_id, m.org_id"
                " FROM recording r JOIN meeting m ON m.id = r.meeting_id"
                " WHERE r.id = :r"
            ),
            {"r": recording_id},
        ).fetchone()
    if base is None:
        raise CorrectionError(f"录音不存在: {recording_id}")
    base_version, meeting_id, org_id = base
    if base_version is None:
        raise CorrectionError("该录音尚无转写版本,无法做人工校对(请先跑完流水线)")

    with eng.connect() as conn:
        old_segments = conn.execute(
            text(
                "SELECT seq, speaker, text, start_ms, end_ms FROM transcript_segment"
                " WHERE version_id = :v ORDER BY seq"
            ),
            {"v": base_version},
        ).fetchall()
    if len(old_segments) != len(lines):
        raise CorrectionError(
            f"校对条数({len(lines)})与原版片段数({len(old_segments)})不符:"
            "人工校对不得改变分段与时轴(改时轴属重新转写 re_asr)。"
        )

    with eng.begin() as conn:
        version_no = conn.execute(
            text("SELECT count(*) + 1 FROM transcript_version WHERE recording_id = :r"),
            {"r": recording_id},
        ).scalar()
        new_version = conn.execute(
            text(
                "INSERT INTO transcript_version"
                " (recording_id, version_no, source_type, correction_of_version_id,"
                "  created_by)"
                " VALUES (:r, :no, 'human_correction', :base, :by) RETURNING id"
            ),
            {"r": recording_id, "no": version_no, "base": base_version,
             "by": created_by},
        ).scalar()

        edited = 0
        for (seq, speaker, old_text, start_ms, end_ms), (new_speaker, new_text) in zip(
            old_segments, lines
        ):
            conn.execute(
                text(
                    "INSERT INTO transcript_segment"
                    " (version_id, seq, start_ms, end_ms, speaker, text,"
                    "  timestamp_precision)"
                    " VALUES (:v, :seq, :sms, :ems, :sp, :tx,"
                    "  'segment_level_from_slice') RETURNING id"
                ),
                # 时间信息来源不变:仍是音频切片边界,校对文本不产生新时间
                {"v": new_version, "seq": seq, "sms": start_ms, "ems": end_ms,
                 "sp": (new_speaker or speaker), "tx": new_text},
            ).scalar()
            conn.execute(
                text(
                    "INSERT INTO segment_mapping"
                    " (old_version_id, old_seq, new_version_id, new_seq, change_type)"
                    " VALUES (:ov, :os, :nv, :ns, :ct)"
                ),
                {"ov": base_version, "os": seq, "nv": new_version, "ns": seq,
                 "ct": "unchanged" if new_text == old_text else "text_edited"},
            )
            if new_text != old_text:
                edited += 1

        # 转写发布指针切换(检索指针稍后随索引完成再切)
        conn.execute(
            text("UPDATE recording SET active_transcript_version_id = :v WHERE id = :r"),
            {"v": new_version, "r": recording_id},
        )

    return {
        "version_id": new_version,
        "meeting_id": meeting_id,
        "org_id": org_id,
        "segments": len(lines),
        "edited": edited,
        "unchanged": len(lines) - edited,
        "source_note": source_note,
    }


def rebuild_search_index(
    eng: Engine, *, recording_id: int, version_id: int, embed: EmbedProvider,
    batch_size: int = 50,
) -> dict:
    """为校对版本重建向量索引并原子发布(沿用 worker 的发布不变量)。"""
    with eng.connect() as conn:
        row = conn.execute(
            text(
                "SELECT r.meeting_id, m.org_id FROM recording r"
                " JOIN meeting m ON m.id = r.meeting_id WHERE r.id = :r"
            ),
            {"r": recording_id},
        ).fetchone()
    if row is None:
        raise CorrectionError(f"录音不存在: {recording_id}")
    meeting_id, org_id = row

    with eng.connect() as conn:
        segments = conn.execute(
            text(
                "SELECT id, seq, text FROM transcript_segment"
                " WHERE version_id = :v ORDER BY seq"
            ),
            {"v": version_id},
        ).fetchall()
    if not segments:
        raise CorrectionError("校对版本无片段,无法建索引")

    with eng.begin() as conn:
        conn.execute(
            text("DELETE FROM embedding_chunk WHERE version_id = :v"),
            {"v": version_id},
        )
    built = 0
    for i in range(0, len(segments), batch_size):
        batch = segments[i : i + batch_size]
        texts = [s[2] for s in batch]
        input_hash = _sha("|".join(str(s[0]) for s in batch))
        billed = call_accounting.billed_call(
            eng, provider=embed, org_id=org_id, job_id=None,
            stage="index", call="embed_batch",
            units={"texts": len(texts), "chars": sum(len(t) for t in texts)},
            input_hash=input_hash,
            invoke=lambda: embed.embed_batch(texts),
            to_ref=lambda vecs: {"vectors": vecs},
        )
        vectors = billed.result
        with eng.begin() as conn:
            for seg, vec in zip(batch, vectors):
                conn.execute(
                    text(
                        "INSERT INTO embedding_chunk"
                        " (segment_id, version_id, org_id, meeting_id, is_active,"
                        "  embedding, text)"
                        " VALUES (:sid, :vid, :org, :mid, false,"
                        "  CAST(:vec AS vector), :text)"
                        " ON CONFLICT (version_id, segment_id) DO NOTHING"
                    ),
                    {"sid": seg[0], "vid": version_id, "org": org_id,
                     "mid": meeting_id,
                     "vec": "[" + ",".join(str(x) for x in vec) + "]",
                     "text": seg[2]},
                )
        built += len(batch)

    # 原子发布:指针 + is_active 翻转同事务;翻转范围限本录音的旧版本
    with eng.begin() as conn:
        conn.execute(
            text(
                "UPDATE embedding_chunk SET is_active = false"
                " WHERE is_active = true AND version_id != :vid"
                " AND version_id IN (SELECT id FROM transcript_version"
                "                    WHERE recording_id = :rid)"
            ),
            {"vid": version_id, "rid": recording_id},
        )
        conn.execute(
            text(
                "UPDATE embedding_chunk SET is_active = true"
                " WHERE version_id = :vid"
            ),
            {"vid": version_id},
        )
        conn.execute(
            text("UPDATE recording SET active_search_version_id = :vid WHERE id = :rid"),
            {"vid": version_id, "rid": recording_id},
        )
    return {"chunks": built, "version_id": version_id}
