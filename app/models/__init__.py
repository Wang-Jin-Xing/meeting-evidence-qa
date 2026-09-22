"""导入全部模型,保证 Alembic autogenerate 元数据完整。"""
from app.models.enums import (
    STAGE_ORDER,
    JobStatus,
    MappingChangeType,
    MeetingVisibility,
    MinutesSourceType,
    MinutesStatus,
    ModelRunStatus,
    StageName,
    StageStatus,
    TimestampPrecision,
    TranscriptSourceType,
    UserRole,
)
from app.models.meeting import Meeting
from app.models.minutes import MinutesVersion, QueryLog
from app.models.model_run import ModelRun
from app.models.org import MeetingAccess, Organization, User
from app.models.recording import JobStage, ProcessingJob, Recording
from app.models.transcript import (
    EMBEDDING_DIM,
    EmbeddingChunk,
    SegmentMapping,
    TranscriptSegment,
    TranscriptVersion,
)

__all__ = [
    "STAGE_ORDER", "JobStatus", "MappingChangeType", "MeetingVisibility",
    "MinutesSourceType", "MinutesStatus", "ModelRunStatus", "StageName",
    "StageStatus", "TimestampPrecision", "TranscriptSourceType", "UserRole",
    "Meeting", "MinutesVersion", "QueryLog", "ModelRun",
    "MeetingAccess", "Organization", "User",
    "JobStage", "ProcessingJob", "Recording",
    "EMBEDDING_DIM", "EmbeddingChunk", "SegmentMapping",
    "TranscriptSegment", "TranscriptVersion",
]
