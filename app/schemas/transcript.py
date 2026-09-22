"""人工转写/校对导入的请求与响应模型。"""
from pydantic import BaseModel, Field


class TranscriptLineIn(BaseModel):
    speaker: str | None = None
    text: str = Field(min_length=1, max_length=2000)


class TranscriptIn(BaseModel):
    """一行对应一个音频切片(时间位置来自切片边界,不由文本推断)。"""

    recording_id: int | None = None
    lines: list[TranscriptLineIn] = Field(min_length=1, max_length=500)
    generate_minutes: bool = True


class TranscriptImportOut(BaseModel):
    meeting_id: int
    recording_id: int
    transcript_version_id: int
    source_type: str          # human_transcription | human_correction
    segments: int
    edited: int
    minutes: dict | None
    index: dict
    # 本次实际生效的模型(真实/mock)——界面据此如实标注输入与模型来源
    providers: dict
