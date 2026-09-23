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
    # 证据回放:这条片段属于哪个录音(界面据此请求受鉴权的音频接口)
    recording_id: int | None = None


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
    """版本摘要(完整内容仅最新版返回)。确认信息逐版本给出,不按会谈推断。"""

    model_config = {"from_attributes": True}

    id: int
    version_no: int
    source_type: str
    status: str
    created_at: datetime
    created_by: int | None = None
    confirmed_by: int | None = None
    confirmed_at: datetime | None = None


class MinutesDetailOut(BaseModel):
    """会谈纪要:最新版本完整内容 + 全部版本摘要。"""

    meeting_id: int
    id: int
    version_no: int
    source_type: str
    status: str
    created_at: datetime
    created_by: int | None = None
    confirmed_by: int | None = None
    confirmed_at: datetime | None = None
    content: MinutesContentOut
    versions: list[MinutesVersionBrief]


class MinutesEditIn(BaseModel):
    """人工修订:必须声明基于哪一版(并发保护),内容仍走服务端校验。

    校验口径与 AI 草稿完全一致(引用可解析、decision/todo 必须带证据、
    负责人/日期缺失须标 needs_confirmation)——不因"是人工编辑"就跳过规则。
    """

    base_version_id: int
    content: dict


class MinutesConfirmIn(BaseModel):
    """确认针对具体版本;界面必须把要确认的 version_id 传上来。"""

    version_id: int
