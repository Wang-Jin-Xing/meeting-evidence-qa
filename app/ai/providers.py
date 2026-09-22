"""模型服务 Provider 协议。

worker 只依赖这些接口;mock 与真实适配器(P3)分别实现。
所有 mock 输出必须确定性且带 is_mock 标识,仅用于链路测试。
"""
from dataclasses import dataclass, field


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


class EmbedProvider:
    name = "embed"
    model_name = "unknown"

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError
