"""转写版本、片段、映射与向量索引。

证据链:recording → transcript_version → segment → 纪要/问答引用。
- segment 落库后不可变(应用层无 UPDATE/DELETE 路径);
- 人工纠错 = 新版本 + segment_mapping 逐条映射(仅 unchanged/text_edited);
- re_asr 版本不生成映射,旧引用指向原版本依然可解析;
- embedding_chunk 仅索引各 recording 的 active_search_version,切换原子进行。
"""
from datetime import datetime

from sqlalchemy import (
    Boolean,
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
from pgvector.sqlalchemy import Vector

from app.core.db import Base
from app.models.enums import (
    MappingChangeType,
    TimestampPrecision,
    TranscriptSourceType,
)

# 向量维度固定 1024(决策 D-03);真实 Provider 若维度不同需新迁移
EMBEDDING_DIM = 1024


class TranscriptVersion(Base):
    __tablename__ = "transcript_version"
    __table_args__ = (UniqueConstraint("recording_id", "version_no"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    recording_id: Mapped[int] = mapped_column(ForeignKey("recording.id"), index=True)
    version_no: Mapped[int] = mapped_column(Integer)
    source_type: Mapped[TranscriptSourceType] = mapped_column(
        Enum(TranscriptSourceType, native_enum=False, length=30)
    )
    correction_of_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("transcript_version.id"), nullable=True
    )
    model_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("model_run.id"), nullable=True
    )
    created_by: Mapped[int | None] = mapped_column(ForeignKey("user.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    segments: Mapped[list["TranscriptSegment"]] = relationship(
        back_populates="version", order_by="TranscriptSegment.seq"
    )


class TranscriptSegment(Base):
    """证据原子:落库后不可变,id 即证据 ID(展示为 SEG-{id})。"""
    __tablename__ = "transcript_segment"
    __table_args__ = (UniqueConstraint("version_id", "seq"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("transcript_version.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    start_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    speaker: Mapped[str | None] = mapped_column(String(100), nullable=True)
    text: Mapped[str] = mapped_column(Text)
    timestamp_precision: Mapped[str] = mapped_column(
        Enum(TimestampPrecision, native_enum=False, length=50),
        default=TimestampPrecision.segment_level_from_slice,
    )

    version = relationship("TranscriptVersion", back_populates="segments")


class SegmentMapping(Base):
    """human_correction 版本与被纠错版本的逐条映射。"""
    __tablename__ = "segment_mapping"
    __table_args__ = (
        UniqueConstraint("old_version_id", "old_seq", "new_version_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    old_version_id: Mapped[int] = mapped_column(ForeignKey("transcript_version.id"))
    old_seq: Mapped[int] = mapped_column(Integer)
    new_version_id: Mapped[int] = mapped_column(ForeignKey("transcript_version.id"))
    new_seq: Mapped[int] = mapped_column(Integer)
    change_type: Mapped[MappingChangeType] = mapped_column(
        Enum(MappingChangeType, native_enum=False, length=20)
    )


class EmbeddingChunk(Base):
    """检索单元 = 转写片段;org_id/meeting_id 冗余作为权限过滤键。"""
    __tablename__ = "embedding_chunk"
    __table_args__ = (UniqueConstraint("version_id", "segment_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    segment_id: Mapped[int] = mapped_column(ForeignKey("transcript_segment.id"))
    version_id: Mapped[int] = mapped_column(ForeignKey("transcript_version.id"), index=True)
    org_id: Mapped[int] = mapped_column(Integer, index=True)
    meeting_id: Mapped[int] = mapped_column(Integer, index=True)
    # is_active=true ⇔ 版本为其 recording 的 active_search_version;
    # 翻转与指针切换同事务,检索永不看到半成品
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    text: Mapped[str] = mapped_column(Text)  # 冗余片段文本,检索回显用
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
