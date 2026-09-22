"""P2: model_run idempotency_key 唯一约束改为普通索引 + query_log 增列

Revision ID: c8a1f27d90b4
Revises: bd57398cd833
Create Date: 2026-09-23

变更说明:
1. model_run.idempotency_key 由唯一约束改为普通索引。
   语义修订(P2):idempotency_key 是"发送给服务商的请求幂等键"(服务商
   支持时重发同键可去重),不是审计主键;model_run 行 = 一次物理调用的
   审计事实。崩溃恢复中"结果未知的调用 + 重发调用"需要各自留痕
   (同键多行),唯一约束会吞掉重发记录,故移除。
2. query_log 增列 relevant_candidate_count:过相关性阈值的候选数,
   与 retrieved_chunk_ids(阈值前原始检索候选)口径分离,避免
   数量与 ID 列表混淆(检索日志约定)。
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "c8a1f27d90b4"
down_revision = "bd57398cd833"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("model_run_idempotency_key_key", "model_run", type_="unique")
    op.create_index(
        op.f("ix_model_run_idempotency_key"), "model_run", ["idempotency_key"]
    )
    op.add_column(
        "query_log",
        sa.Column("relevant_candidate_count", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("query_log", "relevant_candidate_count")
    op.drop_index(op.f("ix_model_run_idempotency_key"), table_name="model_run")
    # 降级前提:不存在同键多行,否则唯一约束重建失败
    op.create_unique_constraint("model_run_idempotency_key_key", "model_run", ["idempotency_key"])
