"""模型服务 Provider 协议。

worker 只依赖这些接口;mock 与真实适配器(P3)分别实现。
所有 mock 输出必须确定性且带 is_mock 标识,仅用于链路测试。
"""
from dataclasses import dataclass, field


class ProviderResultUnknownError(Exception):
    """调用已发出但无法确认远端执行结果(超时/连接中断)。

    该类异常不得把 model_run 记为 failed——结果未知即保留 unknown,
    重发时同键留痕(不宣称避免重复调用/计费)。
    """


@dataclass
class ASRSlice:
    """一个音频分片的转写结果。时间信息由调用方(切片边界)提供,ASR 不产生时间。"""
    speaker: str | None
    text: str
    is_mock: bool = True


@dataclass
class MinutesItemDraft:
    type: str  # topic|discussion|decision|todo|needs_confirmation
    text: str
    certainty: str  # suggested|discussed|decided
    owner: str | None = None
    due_date: str | None = None
    evidence_segment_ids: list[int] = field(default_factory=list)
    needs_confirmation: bool = False


@dataclass
class MinutesDraft:
    items: list[MinutesItemDraft]
    is_mock: bool = True


@dataclass
class SegmentForLLM:
    """喂给纪要生成器的片段(带稳定证据 ID)。"""
    segment_id: int
    seq: int
    speaker: str | None
    text: str


@dataclass
class QACandidate:
    """问答候选片段:服务层已按权限过滤与相似度阈值筛选。"""
    segment_id: int
    seq: int
    speaker: str | None
    text: str
    start_ms: int | None
    end_ms: int | None
    similarity: float  # 余弦相似度(1 - 余弦距离)


@dataclass
class QAAnswer:
    """text=None 表示回答方判定证据不足以回答(充分性判断属回答方职责)。"""

    text: str | None
    cited_segment_ids: list[int] = field(default_factory=list)
    is_mock: bool = True


class ASRProvider:
    name = "asr"
    model_name = "unknown"

    def transcribe_slice(self, audio: bytes, slice_index: int) -> ASRSlice:
        raise NotImplementedError


class LLMProvider:
    name = "llm"
    model_name = "unknown"
    prompt_version = "unknown"

    def generate_minutes(self, segments: list[SegmentForLLM]) -> MinutesDraft:
        raise NotImplementedError

    def answer_question(self, question: str, candidates: list[QACandidate]) -> QAAnswer:
        """基于候选片段生成回答。候选已由服务层做权限过滤与相关性阈值筛选;
        若证据不足以回答所问(候选相关但焦点信息缺失),返回 text=None;
        引用必须且只能来自候选(服务层校验,越权引用即协议违规)。"""
        raise NotImplementedError


class EmbedProvider:
    name = "embed"
    model_name = "unknown"

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError
