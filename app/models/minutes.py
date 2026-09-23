"""纪要版本链与问答日志。"""
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.enums import MinutesSourceType, MinutesStatus


class MinutesVersion(Base):
    """纪要版本:ai_draft 为 v1,人工修订为 human_edit 新版本(parent 指向前版)。"""
    __tablename__ = "minutes_version"
    __table_args__ = (
        UniqueConstraint("job_id"),  # 一个 job 只产出一个 AI 草稿
        UniqueConstraint("meeting_id", "version_no"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    meeting_id: Mapped[int] = mapped_column(ForeignKey("meeting.id"), index=True)
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("processing_job.id"), nullable=True
    )
    version_no: Mapped[int] = mapped_column(Integer)
    source_type: Mapped[MinutesSourceType] = mapped_column(
        Enum(MinutesSourceType, native_enum=False, length=20)
    )
    parent_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("minutes_version.id"), nullable=True
    )
    status: Mapped[MinutesStatus] = mapped_column(
        Enum(MinutesStatus, native_enum=False, length=20),
        default=MinutesStatus.pending_review,
    )
    # 结构化内容;每条目含 type/text/owner/due_date/certainty/
    # evidence_segment_ids/needs_confirmation,由 Pydantic 校验
    content: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    model_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("model_run.id"), nullable=True
    )
    created_by: Mapped[int | None] = mapped_column(ForeignKey("user.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # 确认落在**版本行**上:谁确认了哪一版。新编辑出的版本从 pending_review 重新起算,
    # 旧版本的确认不会跟着走(D-62)。未确认 = 两列都为 NULL,不填默认值。
    confirmed_by: Mapped[int | None] = mapped_column(ForeignKey("user.id"), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class QueryLog(Base):
    """问答审计:问题、检索候选、回答与引用,含 mock 标识。

    计数口径约定(P2):retrieved_chunk_ids 为阈值前原始检索候选(完整
    检索行为),relevant_candidate_count 为过相关性阈值的候选数(真正
    进入回答流程的证据候选),两者分离,避免数量与 ID 列表口径混淆。
    """

    __tablename__ = "query_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    question: Mapped[str] = mapped_column(Text)
    retrieved_chunk_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    relevant_candidate_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    cited_segment_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    has_sufficient_evidence: Mapped[bool | None] = mapped_column(nullable=True)
    is_mock: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
