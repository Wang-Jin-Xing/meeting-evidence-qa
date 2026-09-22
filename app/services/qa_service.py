"""检索问答服务:权限注入候选 SQL → 阈值过滤 → LLM 抽取式回答 → 审计落库。

设计约定:
- 权限作用于检索候选本身(SQL 同时注入 org_id 等值条件与可见会谈子查询,
  双条件纵深防御,不信任单一冗余键);前置的会谈访问检查不替代候选过滤;
- 引用必须来自权限过滤后的候选,LLM 越权引用即协议违规(500,不静默剔除——
  剔除会导致回答文本与引用集不一致);
- 无证据(无可用索引或候选全部低于相似度阈值)明确返回证据不足,
  不生成回答、不带引用;
- 每次问答(无论是否充分)写 query_log 审计。
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.mock_providers import get_providers
from app.ai.providers import QACandidate
from app.core.config import get_settings
from app.models.meeting import Meeting
from app.models.minutes import QueryLog
from app.models.org import User
from app.models.recording import Recording
from app.models.transcript import (
    EmbeddingChunk,
    TranscriptSegment,
    TranscriptVersion,
)
from app.schemas.qa import AnswerOut, CitationOut
from app.services.access import visible_meeting_ids_subquery


class QACitationViolationError(Exception):
    """LLM 引用了候选之外的片段:协议违规,拒绝返回该回答。"""


def retrieve_candidates(
    db: Session, user: User, meeting_id: int, question_vec: list[float], top_k: int
) -> list[tuple]:
    """相似度检索,权限与发布状态注入候选 SQL。

    返回 (chunk_id, segment_id, distance, seq, speaker, text, start_ms, end_ms),
    按余弦距离升序(最相似在前)。条件:
    - 显式限定 meeting_id = 当前会谈(URL 参数),"属于可见集合"不替代该条件;
    - 权限双注入:org_id 等值 + meeting_id ∈ 可见会谈子查询(不信任单一冗余键);
    - 发布状态双保险:is_active=true 且 chunk.version_id = 所属录音的
      active_search_version_id(版本指针为权威,冗余标志误标不可见)。
    """
    distance = EmbeddingChunk.embedding.cosine_distance(question_vec)
    stmt = (
        select(
            EmbeddingChunk.id,
            EmbeddingChunk.segment_id,
            distance.label("distance"),
            TranscriptSegment.seq,
            TranscriptSegment.speaker,
            TranscriptSegment.text,
            TranscriptSegment.start_ms,
            TranscriptSegment.end_ms,
        )
        .join(TranscriptSegment, TranscriptSegment.id == EmbeddingChunk.segment_id)
        .join(
            TranscriptVersion, TranscriptVersion.id == EmbeddingChunk.version_id
        )
        .join(Recording, Recording.id == TranscriptVersion.recording_id)
        .where(EmbeddingChunk.is_active.is_(True))
        .where(EmbeddingChunk.embedding.is_not(None))
        # 版本指针匹配:未发布/旧版本即使误标 is_active 也不可检索
        .where(Recording.active_search_version_id == EmbeddingChunk.version_id)
        # 显式会谈限定 + 权限注入候选 SQL(机构等值 + 可见会谈子查询)
        .where(EmbeddingChunk.org_id == user.org_id)
        .where(EmbeddingChunk.meeting_id == meeting_id)
        .where(EmbeddingChunk.meeting_id.in_(visible_meeting_ids_subquery(user)))
        .order_by(distance)
        .limit(top_k)
    )
    return db.execute(stmt).all()


def ask_question(db: Session, user: User, meeting: Meeting, question: str) -> AnswerOut:
    """完整问答流程(问题向量化与 LLM 为同步调用,见 config 注释)。"""
    settings = get_settings()
    _, llm, embed = get_providers()

    question_vec = embed.embed_batch([question])[0]
    rows = retrieve_candidates(
        db, user, meeting.id, question_vec, settings.qa_top_k
    )

    # 充分性第一层(相关性筛选):相似度 >= 阈值的候选才可作为证据来源
    candidates = [
        (row, 1.0 - float(row.distance))
        for row in rows
        if 1.0 - float(row.distance) >= settings.qa_evidence_threshold
    ]

    def insufficient() -> AnswerOut:
        # 明确返回证据不足:无回答、无引用(不拼接候选冒充答案)。
        # retrieved_count=过阈值候选数:0 表示无相关候选(无索引/不相关/
        # 未发布版本被指针排除),>0 表示相关但焦点线索缺失
        _log_query(db, user, question, rows, answer=None, cited=[], sufficient=False)
        return AnswerOut(
            meeting_id=meeting.id,
            question=question,
            answer=None,
            has_sufficient_evidence=False,
            is_mock=True,
            retrieved_count=len(candidates),
            citations=[],
        )

    if not candidates:
        return insufficient()

    qc = [
        QACandidate(
            segment_id=r.segment_id, seq=r.seq, speaker=r.speaker, text=r.text,
            start_ms=r.start_ms, end_ms=r.end_ms, similarity=round(sim, 6),
        )
        for r, sim in candidates
    ]
    result = llm.answer_question(question, qc)

    # 充分性第二层(焦点线索启发式,mock):候选相关但问题询问的
    # 日期/负责人/时长线索缺失时,LLM 返回 text=None → 证据不足
    if result.text is None:
        return insufficient()

    # 引用校验:必须全部来自权限过滤后的候选(防越权引用)
    candidate_ids = {c.segment_id for c in qc}
    if not set(result.cited_segment_ids) <= candidate_ids:
        raise QACitationViolationError(
            f"LLM 引用了候选之外的片段: {sorted(set(result.cited_segment_ids) - candidate_ids)}"
        )

    cited_map = {c.segment_id: c for c in qc}
    citations = [cited_map[sid] for sid in result.cited_segment_ids]

    _log_query(
        db, user, question, rows,
        answer=result.text, cited=result.cited_segment_ids, sufficient=True,
    )
    return AnswerOut(
        meeting_id=meeting.id,
        question=question,
        answer=result.text,
        has_sufficient_evidence=True,
        is_mock=result.is_mock,
        retrieved_count=len(candidates),
        citations=[
            CitationOut(
                segment_id=c.segment_id, seq=c.seq, speaker=c.speaker, text=c.text,
                start_ms=c.start_ms, end_ms=c.end_ms, similarity=c.similarity,
            )
            for c in citations
        ],
    )


def _log_query(db, user, question, rows, *, answer, cited, sufficient):
    db.add(
        QueryLog(
            user_id=user.id,
            question=question,
            retrieved_chunk_ids=[r[0] for r in rows],
            answer=answer,
            cited_segment_ids=list(cited),
            has_sufficient_evidence=sufficient,
            is_mock=True,
        )
    )
    db.commit()
