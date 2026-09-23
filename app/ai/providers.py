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
class ProviderUsage:
    """一次调用的用量与费用(三态口径,详见 docs/DECISIONS.md D-42)。

    basis:
    - provider_reported:服务商返回的用量/账单口径,最可信;
    - estimated:按单价表估算(实际可能小幅偏差);
    - unknown:无任何成本信息。**cost_cents 必须为 None,不得按 0 记账**。
    """

    basis: str = "unknown"
    cost_cents: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None


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
    # 时间信息来源:切片边界 ≠ 精确语音位置,界面与引用必须带精度口径
    timestamp_precision: str | None = None


@dataclass
class QAAnswer:
    """text=None 表示回答方判定证据不足以回答(充分性判断属回答方职责)。"""

    text: str | None
    cited_segment_ids: list[int] = field(default_factory=list)
    is_mock: bool = True


class _ProviderBase:
    """Provider 公共契约:身份声明 + 成本核算钩子。

    is_mock 默认 False:真实适配器若忘了声明,会被记为真实调用,
    从而在预算核算上保守(缺成本信息即阻断),而不是静默按 mock 放过。
    """

    name = "provider"
    model_name = "unknown"
    is_mock = False
    # 推理发生在哪里:"cpu" / "cuda" / "remote:<厂商>" 等,由实现自报。
    # None = 未声明(记账写 NULL,不猜设备)。见 D-52。
    runtime_device: str | None = None

    def estimate_cost(self, call: str, units: dict) -> float | None:
        """发出调用前的费用预估(分)。None = 该 Provider 无价格口径。

        预算执行依赖它:真实 Provider 返回 None 且机构设了预算时,
        调用会被拒绝(预算不可核算),而不是当作 0 放行。
        """
        return None

    def usage_for(self, call: str, units: dict, result) -> ProviderUsage | None:
        """调用成功后从结果(或服务商响应)读取实际用量。None = 无用量信息。"""
        return None


class ASRProvider(_ProviderBase):
    name = "asr"

    def transcribe_slice(self, audio: bytes, slice_index: int) -> ASRSlice:
        raise NotImplementedError


class LLMProvider(_ProviderBase):
    name = "llm"
    prompt_version = "unknown"

    def generate_minutes(self, segments: list[SegmentForLLM]) -> MinutesDraft:
        raise NotImplementedError

    def answer_question(self, question: str, candidates: list[QACandidate]) -> QAAnswer:
        """基于候选片段生成回答。候选已由服务层做权限过滤与相关性阈值筛选;
        若证据不足以回答所问(候选相关但焦点信息缺失),返回 text=None;
        引用必须且只能来自候选(服务层校验,越权引用即协议违规)。"""
        raise NotImplementedError


class EmbedProvider(_ProviderBase):
    name = "embed"

    # 真实适配器必须声明其输出维度;工厂据此与向量列维度核对(D-03)。
    embedding_dim: int | None = None

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError
