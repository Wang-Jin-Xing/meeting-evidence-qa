"""纪要查看的响应模型。"""
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class SegmentEvidenceOut(BaseModel):
    """纪要条目证据片段的解析结果(segment_id → 原文与时间位置)。

    片段落库后不可变,re_asr 也不删旧版本,故历史纪要的引用始终可解析。
    """

    model_config = {"from_attributes": True}

    segment_id: int
    seq: int
    speaker: str | None
    text: str
    start_ms: int | None
    end_ms: int | None
    # 时间信息来源(如 segment_level_from_slice):界面必须显示精度,
    # 不得把切片边界当成精确语音位置(D-53)
    timestamp_precision: str | None = None


class MinutesItemOut(BaseModel):
    type: str
    text: str
    certainty: str
    owner: str | None
    due_date: str | None
    evidence_segment_ids: list[int]
    needs_confirmation: bool
    # 服务端按 segment_id 解析并内嵌的片段详情(限本会谈的录音)
    evidence: list[SegmentEvidenceOut]


class MinutesContentOut(BaseModel):
    items: list[MinutesItemOut]


class MinutesVersionBrief(BaseModel):
    """版本摘要(完整内容仅最新版返回)。"""

    model_config = {"from_attributes": True}

    id: int
    version_no: int
    source_type: str
    status: str
    created_at: datetime


class MinutesDetailOut(BaseModel):
    """会谈纪要:最新版本完整内容 + 全部版本摘要。"""

    meeting_id: int
    id: int
    version_no: int
    source_type: str
    status: str
    created_at: datetime
    content: MinutesContentOut
    versions: list[MinutesVersionBrief]
