"""P2 收尾: processing_job.next_run_at(重试退避)与 organization.budget_cents(费用预算)

Revision ID: e4b2c19d7a53
Revises: c8a1f27d90b4
Create Date: 2026-09-23

变更说明:
1. processing_job.next_run_at:重试退避最早可领取时刻。claim 候选增加
   `next_run_at IS NULL OR next_run_at <= clock_timestamp()` 条件;
   requeue_or_fail 按 attempt 递增写入退避(封顶),防止失败后立即重领。
2. organization.budget_cents:组织费用预算(NULL=不限)。claim 候选排除
   累计模型费用(SUM(model_run.cost_estimate))已达预算的组织任务,
   使费用预算切实参与调度。mock 调用 cost_estimate 为空(0),不影响
   既有测试;真实 Provider 接入后由适配器写入费用。
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "e4b2c19d7a53"
down_revision = "c8a1f27d90b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "processing_job",
        sa.Column("next_run_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "organization",
        sa.Column("budget_cents", sa.Numeric(14, 4), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("organization", "budget_cents")
    op.drop_column("processing_job", "next_run_at")
