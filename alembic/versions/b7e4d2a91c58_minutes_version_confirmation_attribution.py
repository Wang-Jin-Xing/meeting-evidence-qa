"""P3 纪要人工流转:确认必须归属于具体版本与具体人

Revision ID: b7e4d2a91c58
Revises: 5c1a8f3b0d47
Create Date: 2026-09-23

纪要状态只有 pending_review / confirmed 两态,但"已确认"这个事实此前**无处可查**:
谁在什么时候确认了哪一版。没有这两列就只能把确认挂在会谈上,而一旦有人编辑出新版本,
旧版本的确认状态就会错误地"跟随"过去 —— 那等于替说话人承认他没承认过的内容。

因此确认信息落在版本行上:
- confirmed_by / confirmed_at 都可空且**不回填**:历史版本没有被确认过这件事
  是事实,补任何默认值都是造假;
- 编辑产生的新版本 status 一律从 pending_review 起(见 D-62),要人再确认一次。
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "b7e4d2a91c58"
down_revision = "5c1a8f3b0d47"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "minutes_version",
        sa.Column("confirmed_by", sa.Integer(), nullable=True),
    )
    op.add_column(
        "minutes_version",
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
    )
    op.create_foreign_key(
        "fk_minutes_version_confirmed_by",
        "minutes_version", "user",
        ["confirmed_by"], ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_minutes_version_confirmed_by", "minutes_version",
                       type_="foreignkey")
    op.drop_column("minutes_version", "confirmed_at")
    op.drop_column("minutes_version", "confirmed_by")
