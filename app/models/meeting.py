"""会谈:机构、参与方、主题、时间与可见性。

注意:会谈不单独维护处理状态字段,处理进度由其录音的 processing_job 派生
(决策 D-01,见 docs/DECISIONS.md)。
"""
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.enums import MeetingVisibility


class Meeting(Base):
    __tablename__ = "meeting"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organization.id"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    vendor_name: Mapped[str] = mapped_column(String(200))
    topic: Mapped[str | None] = mapped_column(String(500), nullable=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime)
    visibility: Mapped[MeetingVisibility] = mapped_column(
        Enum(MeetingVisibility, native_enum=False, length=20),
        default=MeetingVisibility.org,
    )
    created_by: Mapped[int] = mapped_column(ForeignKey("user.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    recordings: Mapped[list["Recording"]] = relationship(back_populates="meeting")
