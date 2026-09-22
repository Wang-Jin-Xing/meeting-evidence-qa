from pydantic import BaseModel


class UploadAccepted(BaseModel):
    """录音上传受理结果:任务已持久化排队,请求内不做任何模型调用。"""
    recording_id: int
    job_id: int
    job_status: str
    duplicate: bool = False
    existing_recording_id: int | None = None


class StageOut(BaseModel):
    stage_name: str
    status: str
    attempt_count: int
    started_at: str | None
    finished_at: str | None
    error: str | None


class JobOut(BaseModel):
    id: int
    recording_id: int
    meeting_id: int
    status: str
    current_stage: str | None
    attempt_count: int
    last_error: str | None
    stages: list[StageOut]
