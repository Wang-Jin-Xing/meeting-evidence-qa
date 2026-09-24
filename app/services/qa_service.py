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
import hashlib
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.providers import QACandidate
from app.ai.registry import build_providers
from app.services import call_accounting
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

    返回 (chunk_id, segment_id, distance, seq, speaker, text, start_ms, end_ms,
    timestamp_precision, recording_id),
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
            TranscriptSegment.timestamp_precision,
            TranscriptVersion.recording_id,
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


_MODEL_CODE_RE = re.compile(r"[A-Za-z]{1,4}\d{2,4}[A-Za-z]?")


def model_codes(text: str) -> set[str]:
    """文本里出现的设备型号代号(统一大写、去分隔符)。"""
    return {m.group(0).upper() for m in _MODEL_CODE_RE.finditer(text.replace("-", "")
                                                                .replace("_", "").replace(" ", ""))}


_CLUE_UNIT = r"(年|个月|天|周|工作日|小时|分钟|人份|度|摄氏度)"


def _clue_asked(question: str):
    """问题在问哪类线索(与 mock 的焦点线索规则同一分类,便于两条路径同口径)。

    返回 None = 不属任何可核验线索类,此时只要求型号同句出现,不做线索判断。
    """
    if "谁" in question or "负责" in question:
        return lambda t: "负责" in t
    if any(k in question for k in ("什么时候", "几号", "哪天", "日期", "时间")):
        return lambda t: bool(re.search(r"\d{1,2}月\d{1,2}[日号]|\d{4}年\d{1,2}月", t))
    if any(k in question for k in ("多久", "多长", "几天", "几年", "期限")):
        return lambda t: bool(re.search(rf"\d+\s*{_CLUE_UNIT}", t))
    if any(k in question for k in ("多少", "几盒", "价格", "报价", "单价", "费用", "钱")):
        return lambda t: bool(re.search(r"\d", t))
    return None


def focus_mismatch(question: str, cited_texts: list[str]) -> bool:
    """型号归属不成立:没有任何一段被引用的证据**同时**满足
    ①含问题点名的型号 ②含问题所要的那类线索。

    为什么必须两层都查:只查"型号在不在证据里"会被同一会议里的其他句子蒙过去 ——
    实测 D-32 反例("X300 的注册证什么时候补齐?")就通过了弱版闸,
    因为语料里另有一句"X300 型号的保修期是多久?"确实提到了 X300,
    但它回答的不是注册证日期。真正的问题是**把 X200 的日期安到 X300 头上**,
    所以必须要求型号与线索在同一句里共现。

    为什么必须是确定性的一道闸,而不是交给模型自觉:
    - mock 的焦点线索规则只看"负责人/日期/时长"有没有线索,**不看型号**;
    - 真实 LLM 是否会拒取决于提示词与采样,换模型/换版本就可能翻转 ——
      而"把 A 型号的承诺安到 B 型号头上"是本业务里代价最高的一类错。
    问题没点名型号时本闸不介入(避免把"谁负责培训"这类可答题误判为不足)。
    """
    asked = model_codes(question)
    if not asked:
        return False
    clue = _clue_asked(question)
    for t in cited_texts:
        if not (asked & model_codes(t)):
            continue
        if clue is None or clue(t):
            return False
    return True


def ask_question(db: Session, user: User, meeting: Meeting, question: str) -> AnswerOut:
    """完整问答流程(问题向量化与 LLM 为同步调用,见 config 注释)。

    这两次调用同样经 call_accounting:问答路径不记账的话,真实 Provider 的
    费用与预算就只覆盖了后台流水线,用户侧调用会成了免费额度。
    """
    settings = get_settings()
    _, llm, embed = build_providers()
    eng = db.get_bind()

    def _call(provider, stage, call, units, input_hash, invoke, to_ref=None):
        return call_accounting.billed_call(
            eng, provider=provider, org_id=user.org_id, job_id=None,
            stage=stage, call=call, units=units, input_hash=input_hash,
            invoke=invoke, to_ref=to_ref,
            prompt_version=getattr(provider, "prompt_version", None),
        )

    question_hash = _sha(f"embed|{embed.model_name}|{question}")
    billed_vec = _call(
        embed, "qa", "embed_batch",
        {"texts": 1, "chars": len(question)}, question_hash,
        lambda: embed.embed_batch([question]),
    )
    question_vec = billed_vec.result[0]
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
        # raw=阈值前检索数(检索了什么);relevant=过阈值候选数
        # (0=无相关候选:无索引/不相关/未发布版本被指针排除;
        #  >0=相关但焦点线索缺失)
        _log_query(
            db, user, question, rows, relevant=len(candidates),
            answer=None, cited=[], sufficient=False, is_mock=llm.is_mock,
        )
        return AnswerOut(
            meeting_id=meeting.id,
            question=question,
            answer=None,
            has_sufficient_evidence=False,
            is_mock=llm.is_mock,
            raw_retrieved_count=len(rows),
            relevant_candidate_count=len(candidates),
            citations=[],
        )

    if not candidates:
        return insufficient()

    qc = [
        QACandidate(
            segment_id=r.segment_id, seq=r.seq, speaker=r.speaker, text=r.text,
            start_ms=r.start_ms, end_ms=r.end_ms, similarity=round(sim, 6),
            timestamp_precision=(r.timestamp_precision.value
                                 if r.timestamp_precision else None),
            recording_id=r.recording_id,
        )
        for r, sim in candidates
    ]
    # 每次问答都是真实外部调用:不做跨请求结果缓存(缓存按 job 隔离的约定
    # 不适用于无 job 的问答路径,跨用户复用回答会绕过审计与计费口径)
    answer_hash = _sha(
        f"{llm.prompt_version}|{question}|"
        + "|".join(f"{c.segment_id}:{c.text}" for c in qc)
    )
    billed_ans = _call(
        llm, "qa", "answer_question",
        {"candidates": len(qc), "question_chars": len(question),
         "chars": len(question) + sum(len(c.text) for c in qc)}, answer_hash,
        lambda: llm.answer_question(question, qc),
        to_ref=lambda r: {"text": r.text, "cited": r.cited_segment_ids},
    )
    result = billed_ans.result

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

    # 充分性第三层(与 Provider 无关):型号归属闸。
    # 问题点名的型号在证据里根本没出现 → 这段证据说的不是它,不能答。
    if focus_mismatch(question, [c.text for c in citations]):
        return insufficient()

    _log_query(
        db, user, question, rows, relevant=len(candidates),
        answer=result.text, cited=result.cited_segment_ids, sufficient=True,
        is_mock=result.is_mock,
    )
    return AnswerOut(
        meeting_id=meeting.id,
        question=question,
        answer=result.text,
        has_sufficient_evidence=True,
        is_mock=result.is_mock,
        raw_retrieved_count=len(rows),
        relevant_candidate_count=len(candidates),
        citations=[
            CitationOut(
                segment_id=c.segment_id, seq=c.seq, speaker=c.speaker, text=c.text,
                start_ms=c.start_ms, end_ms=c.end_ms, similarity=c.similarity,
                timestamp_precision=c.timestamp_precision,
                recording_id=c.recording_id,
            )
            for c in citations
        ],
    )


def _sha(text_: str) -> str:
    return hashlib.sha256(text_.encode("utf-8")).hexdigest()


def _log_query(db, user, question, rows, *, relevant, answer, cited, sufficient,
               is_mock):
    db.add(
        QueryLog(
            user_id=user.id,
            question=question,
            retrieved_chunk_ids=[r[0] for r in rows],  # 阈值前原始候选
            relevant_candidate_count=relevant,          # 过阈值候选数
            answer=answer,
            cited_segment_ids=list(cited),
            has_sufficient_evidence=sufficient,
            is_mock=is_mock,
        )
    )
    db.commit()
