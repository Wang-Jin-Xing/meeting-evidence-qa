from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class MeetingCreate(BaseModel):
    """创建会谈。

    关键:extra="forbid" —— 请求体不允许携带 org_id/created_by 等任何多余字段,
    机构归属只能来自后端身份,前端传入机构 ID 直接 422。
    """
    model_config = ConfigDict(extra="forbid")

    title: str = Field(max_length=300)
    vendor_name: str = Field(max_length=200)
    topic: str | None = Field(default=None, max_length=500)
    scheduled_at: datetime
    visibility: Literal["org", "restricted"] = "org"
    # restricted 时可选白名单用户;服务端校验其必须属于本机构
    access_user_ids: list[int] = Field(default_factory=list)


class MeetingOut(BaseModel):
    id: int
    org_id: int
    title: str
    vendor_name: str
    topic: str | None
    scheduled_at: datetime
    visibility: str
    created_by: int
    created_at: datetime

    model_config = {"from_attributes": True}


class RecordingBrief(BaseModel):
    id: int
    original_filename: str
    checksum: str
    job_id: int | None
    job_status: str | None
    job_current_stage: str | None
    # 当前转写来源(asr / human_transcription / human_correction / re_asr):
    # 界面必须显示文本到底来自自动转写还是人工导入,不与 mock 转写混记
    transcript_version_no: int | None = None
    transcript_source: str | None = None
    # 实际产出该转写版本的模型名(经 model_run 关联),界面据此显示,不自行推断
    transcript_model: str | None = None

    model_config = {"from_attributes": True}


class MeetingDetail(MeetingOut):
    recordings: list[RecordingBrief] = []
