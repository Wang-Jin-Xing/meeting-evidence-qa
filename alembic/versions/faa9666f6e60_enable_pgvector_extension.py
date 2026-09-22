"""enable pgvector extension

初始迁移:仅启用 pgvector 扩展作为基础设施验证。
业务表(机构/用户/会谈/录音/任务/转写版本/纪要等)自 Phase 1 起逐阶段添加,
不在此提前创建。

Revision ID: faa9666f6e60
Revises:
Create Date: 2026-09-23 02:07:11.958971
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'faa9666f6e60'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # CREATE EXTENSION IF NOT EXISTS:幂等,重复执行无害
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def downgrade() -> None:
    # vector 扩展被依赖表使用前才可删除;Phase 0 阶段无依赖,可安全降级
    op.execute("DROP EXTENSION IF EXISTS vector")
