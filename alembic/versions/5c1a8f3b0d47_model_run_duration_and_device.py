"""P3 真实音频验证前置:model_run 记录单次调用耗时与运行设备

Revision ID: 5c1a8f3b0d47
Revises: a7f3d0c58e21
Create Date: 2026-09-23

为什么要加这两列(见 docs/DECISIONS.md D-52):
本地模型的外部 API 费用是 0,但**算力不是 0**。只记 cost=0 会让"零成本"被误读,
因此每次调用额外记录:
- duration_ms:本次调用的实际墙钟耗时(本地推理含模型加载后的净推理时间);
- device:推理发生在哪里(cpu / cuda / 云端服务商标识),由 Provider 自报,
  未声明则为 NULL(不猜测、不填默认值)。
两列都可空,存量行不回填(历史数据没有测量过,补值就是造假)。
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "5c1a8f3b0d47"
down_revision = "a7f3d0c58e21"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("model_run", sa.Column("duration_ms", sa.Integer(), nullable=True))
    op.add_column("model_run", sa.Column("device", sa.String(length=50), nullable=True))


def downgrade() -> None:
    op.drop_column("model_run", "device")
    op.drop_column("model_run", "duration_ms")
