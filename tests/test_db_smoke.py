"""数据库冒烟测试(集成):真实连接 PostgreSQL 并验证 pgvector 可用。"""
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from app.core.db import engine


def _alembic_head() -> str:
    """动态读取迁移目录当前 head,避免硬编码版本号。"""
    cfg = Config("alembic.ini")
    return ScriptDirectory.from_config(cfg).get_current_head()


def test_select_one():
    with engine.connect() as conn:
        assert conn.execute(text("SELECT 1")).scalar() == 1


def test_alembic_revision_applied():
    with engine.connect() as conn:
        rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    expected = _alembic_head()
    assert rev == expected, f"期望迁移版本 {expected},实际 {rev}"


def test_pgvector_extension_and_type():
    with engine.connect() as conn:
        ext = conn.execute(
            text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
        ).scalar()
        assert ext == "vector", "pgvector 扩展未安装,请检查初始迁移"
        dim = conn.execute(text("SELECT vector_dims('[1,2,3]'::vector)")).scalar()
    assert dim == 3
