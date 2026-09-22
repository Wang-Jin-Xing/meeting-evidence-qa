"""模型调用记录(可复现与成本核算)。

- input_hash:规范化输入+prompt 版本的哈希,用于"结果缓存"(相同内容复用输出);
- idempotency_key:发送给服务商的请求幂等键(服务商支持时重发同键可去重),
  不是审计主键——model_run 行 = 一次物理调用的事实记录,同键可多行
  (P2 修订:崩溃恢复中"结果未知的调用 + 重发调用"各自留痕);
- status=unknown:调用已发出但结果未知(进程死亡/超时/断连),
  该行即"可能重复调用/计费"的审计证据,不宣称避免。
- 费用三态(cost_basis):provider_reported(服务商返回用量)/ estimated
  (单价表估算)/ unknown(无成本信息)。cost_estimate 为 NULL 表示**未知**,
  不是 0:预算核算遇到 unknown 的真实调用即视为不可核算(见 D-42)。
  mock 调用 is_mock=true 且恒为 unknown——本地计算不产生真实费用,
  也不得被当作真实成本参与对比。
"""
from datetime import datetime

from sqlalchemy import DateTime, Enum, Index, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.enums import CostBasis, ModelRunStatus


class ModelRun(Base):
    __tablename__ = "model_run"
    __table_args__ = (
        Index("ix_model_run_idempotency_key", "idempotency_key"),
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
    # 费用口径(三态):unknown 时 cost_estimate 必须为 NULL,不得按 0 记账。
    # mock 调用恒为 unknown——本地计算无真实费用,也不得冒充真实成本。
    cost_basis: Mapped[CostBasis] = mapped_column(
        Enum(CostBasis, native_enum=False, length=30),
        default=CostBasis.unknown,
        server_default=CostBasis.unknown.value,
        nullable=False,
    )
    tokens_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
