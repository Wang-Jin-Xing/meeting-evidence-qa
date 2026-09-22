"""机构与用户。"""
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.enums import UserRole


class Organization(Base):
    __tablename__ = "organization"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    # 费用预算(货币分,NULL=不限):达预算后 claim 不再领取该机构任务,
    # 且每次外部调用前需通过预留检查(见 app/services/budget.py)
    budget_cents: Mapped[float | None] = mapped_column(Numeric(14, 4), nullable=True)
    # 在途预留(货币分):调用发出前 +est,结算后 -est。
    # 与 SUM(model_run.cost_estimate) 相加才是"该机构已占用额度"。
    reserved_cents: Mapped[float] = mapped_column(
        Numeric(14, 4), default=0, server_default="0", nullable=False
    )

    users: Mapped[list["User"]] = relationship(back_populates="organization")


class User(Base):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organization.id"), index=True)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(String(500))
    display_name: Mapped[str] = mapped_column(String(200))
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, native_enum=False, length=20), default=UserRole.staff
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    organization: Mapped[Organization] = relationship(back_populates="users")


class MeetingAccess(Base):
    """restricted 会谈的白名单。org 可见性会谈不写此表。"""
    __tablename__ = "meeting_access"
    __table_args__ = (UniqueConstraint("user_id", "meeting_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    meeting_id: Mapped[int] = mapped_column(ForeignKey("meeting.id"), index=True)
