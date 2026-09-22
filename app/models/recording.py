"""录音与处理任务。

- recording 不可变:重新处理 = 新 recording(设计约定);
- 双版本指针:active_transcript_version=当前转写,active_search_version=已建好索引的版本,
  检索只依据后者(设计约定,消除发布/索引窗口不一致);
- 一个 recording 只有一个 processing_job(UNIQUE),手动 requeue 复用同一 job。
"""
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.enums import JobStatus, SliceStatus, StageName, StageStatus


class Recording(Base):
    __tablename__ = "recording"

    id: Mapped[int] = mapped_column(primary_key=True)
    meeting_id: Mapped[int] = mapped_column(ForeignKey("meeting.id"), index=True)
    file_path: Mapped[str] = mapped_column(String(1000))
    original_filename: Mapped[str] = mapped_column(String(500))
    checksum: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    mime: Mapped[str | None] = mapped_column(String(100), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("user.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # 双版本指针(指向 transcript_version,P1.2 起写入)
    active_transcript_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("transcript_version.id", use_alter=True, name="fk_rec_active_transcript"),
        nullable=True,
    )
    active_search_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("transcript_version.id", use_alter=True, name="fk_rec_active_search"),
        nullable=True,
    )

    __table_args__ = (UniqueConstraint("meeting_id", "checksum"),)  # 重复提交检测

    meeting = relationship("Meeting", back_populates="recordings")
    job: Mapped["ProcessingJob"] = relationship(
        back_populates="recording", uselist=False
    )


class ProcessingJob(Base):
    __tablename__ = "processing_job"

    id: Mapped[int] = mapped_column(primary_key=True)
    recording_id: Mapped[int] = mapped_column(
        ForeignKey("recording.id"), unique=True, index=True
    )
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, native_enum=False, length=20), default=JobStatus.queued
    )
    current_stage: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # 租约:epoch 每次领取 +1;所有提交须校验 epoch(设计约定)
    lease_epoch: Mapped[int] = mapped_column(Integer, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    # 重试退避最早可领取时刻(失败后按 attempt 递增写入;NULL=立即可领)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    recording = relationship("Recording", back_populates="job")
    stages: Mapped[list["JobStage"]] = relationship(
        back_populates="job", order_by="JobStage.id"
    )


class JobStage(Base):
    __tablename__ = "job_stage"
    __table_args__ = (UniqueConstraint("job_id", "stage_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("processing_job.id"), index=True)
    stage_name: Mapped[StageName] = mapped_column(
        Enum(StageName, native_enum=False, length=20)
    )
    status: Mapped[StageStatus] = mapped_column(
        Enum(StageStatus, native_enum=False, length=20), default=StageStatus.pending
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 阶段产物引用(如 prepare 的音频元信息);JSON 文件路径也记录于此
    artifact_ref: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    job = relationship("ProcessingJob", back_populates="stages")


class TranscribeSlice(Base):
    """转写分片:进度以数据库为准(设计约定)。

    - prepare 阶段按切片边界建行(pending);
    - transcribe 逐片转写,成功片写回 output_ref 且可复用,失败重试只处理未成功片;
    - 分片内容存 DB,不依赖共享文件追加与计数。
    """
    __tablename__ = "transcribe_slice"
    __table_args__ = (UniqueConstraint("job_id", "slice_index"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("processing_job.id"), index=True)
    slice_index: Mapped[int] = mapped_column(Integer)
    start_ms: Mapped[int] = mapped_column(Integer)
    end_ms: Mapped[int] = mapped_column(Integer)
    status: Mapped[SliceStatus] = mapped_column(
        Enum(SliceStatus, native_enum=False, length=20), default=SliceStatus.pending
    )
    input_hash: Mapped[str] = mapped_column(String(64))  # 切片字节+配置的哈希
    output_ref: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {speaker,text}
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
