"""P3 费用与预算:model_run.cost_basis(费用三态)与 organization.reserved_cents(调用级预留)

Revision ID: a7f3d0c58e21
Revises: e4b2c19d7a53
Create Date: 2026-09-23

变更说明(见 docs/DECISIONS.md D-42/D-38):
1. model_run.cost_basis:费用口径三态 estimated / provider_reported / unknown。
   口径约定:cost_estimate 为 NULL 表示"成本未知",**不是 0**;mock 调用恒为
   unknown(本地计算不产生真实费用,也不得冒充真实成本)。存量行回填 unknown。
2. organization.reserved_cents:在途预留金额(分)。预算执行从"领取时比较历史
   费用"改为"每次外部调用前原子预留、返回后结算",否则单个长任务(数百分片)
   可在领取后无限消耗。机构已占用额度 = SUM(model_run.cost_estimate)
   + reserved_cents。
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "a7f3d0c58e21"
down_revision = "e4b2c19d7a53"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "model_run",
        sa.Column(
            "cost_basis",
            sa.Enum(
                "estimated", "provider_reported", "unknown",
                name="costbasis", native_enum=False, length=30,
            ),
            nullable=False,
            server_default="unknown",
        ),
    )
    op.add_column(
        "organization",
        sa.Column(
            "reserved_cents", sa.Numeric(14, 4), nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("organization", "reserved_cents")
    op.drop_column("model_run", "cost_basis")
