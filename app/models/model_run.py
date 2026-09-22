"""模型调用记录(可复现与成本核算)。

- input_hash:规范化输入+prompt 版本的哈希,用于"结果缓存"(相同内容复用输出);
- idempotency_key:含机构与逻辑任务的稳定键(不含 attempt),用于"请求幂等",
  仅在服务商支持请求级幂等时实际生效,是否已发送由 idempotency_key_sent 记录;
- 两者是分开的机制,不混用(设计约定);
- status=unknown:超时/断连等结果未知调用,预留估算费用计入费用上限。
"""
from datetime import datetime

from sqlalchemy import DateTime, Enum, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.enums import ModelRunStatus


class ModelRun(Base):
    __tablename__ = "model_run"
    __table_args__ = (
        UniqueConstraint("idempotency_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # 调用归属(机构+逻辑任务),保证不同业务请求即使内容相同也不合并
    org_id: Mapped[int] = mapped_column(Integer, index=True)
    job_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    stage: Mapped[str] = mapped_column(String(20))
    provider: Mapped[str] = mapped_column(String(50))
    model_name: Mapped[str] = mapped_column(String(100))
    prompt_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    input_hash: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[ModelRunStatus] = mapped_column(
        Enum(ModelRunStatus, native_enum=False, length=20)
    )
    output_ref: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    is_mock: Mapped[bool] = mapped_column(default=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    idempotency_key_sent: Mapped[bool] = mapped_column(default=False)
    cost_estimate: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    tokens_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
