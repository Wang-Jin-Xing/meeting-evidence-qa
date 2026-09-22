"""数据库冒烟测试(集成):真实连接 PostgreSQL 并验证 pgvector 可用。

覆盖三点:
1. 连接与查询(SELECT 1);
2. 初始迁移已执行(alembic_version 为期望版本,pgvector 扩展存在);
3. vector 类型实际可用(能构造向量并计算维度)。
"""
import pytest
from sqlalchemy import text

from app.core.db import engine

EXPECTED_REVISION = "faa9666f6e60"


def test_select_one():
    with engine.connect() as conn:
        assert conn.execute(text("SELECT 1")).scalar() == 1


def test_alembic_revision_applied():
    with engine.connect() as conn:
        rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert rev == EXPECTED_REVISION, f"期望迁移版本 {EXPECTED_REVISION},实际 {rev}"


def test_pgvector_extension_and_type():
    with engine.connect() as conn:
        ext = conn.execute(
            text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
        ).scalar()
        assert ext == "vector", "pgvector 扩展未安装,请检查初始迁移"
        dim = conn.execute(text("SELECT vector_dims('[1,2,3]'::vector)")).scalar()
    assert dim == 3
