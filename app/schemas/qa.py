"""检索问答的请求/响应模型。"""
from pydantic import BaseModel, ConfigDict, Field


class QuestionIn(BaseModel):
    """提问请求。extra=forbid:不接受任何多余字段。"""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)


class CitationOut(BaseModel):
    """引用片段:segment_id 全局唯一,原文与时间位置直接内嵌,引用即证据。"""

    segment_id: int
    seq: int
    speaker: str | None
    text: str
    start_ms: int | None
    end_ms: int | None
    similarity: float
    timestamp_precision: str | None = None  # 与问题的余弦相似度(1 - 余弦距离)
    recording_id: int | None = None  # 证据回放按此请求受鉴权的音频接口


class AnswerOut(BaseModel):
    """问答结果。

    has_sufficient_evidence 为 mock 启发式判断(相关性阈值 + 焦点线索规则,
    见 docs/DECISIONS.md D-25),不宣称完成真实充分性校验。
    false 时 answer 为 None 且无引用——无论原因是候选全低于相关性阈值、
    无可用索引,还是候选相关但问题询问的日期/负责人/时长线索缺失,
    均明确返回证据不足,不拼接候选冒充答案。

    计数口径约定(与 query_log 一致):
    - raw_retrieved_count:阈值前原始检索候选数(检索了什么);
    - relevant_candidate_count:过相关性阈值的候选数(可作证据的候选)。
    两者分离,避免数量与 ID 列表口径混淆。
    """

    meeting_id: int
    question: str
    answer: str | None
    has_sufficient_evidence: bool
    is_mock: bool
    raw_retrieved_count: int
    relevant_candidate_count: int
    citations: list[CitationOut]
