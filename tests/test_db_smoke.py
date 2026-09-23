"""数据库冒烟测试(集成):真实连接 PostgreSQL 并验证 pgvector 可用。"""
import os
import subprocess
import sys

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.core.db import engine
from tests.conftest import TEST_DB
from tests.db_guard import APP_DATABASE_NAME, REPO_ROOT


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


# ---------- 测试库隔离(决策 D-06 修订) ----------

def test_session_runs_on_isolated_test_database():
    """本会话连接的库必须是守卫核实过的测试库,且不是应用库。"""
    with engine.connect() as conn:
        actual = conn.execute(text("SELECT current_database()")).scalar()
    assert actual == TEST_DB
    assert APP_DATABASE_NAME, "守卫未记录应用库名,无法证明隔离"
    assert actual != APP_DATABASE_NAME, (
        f"测试库与应用库同名({actual}),隔离失效"
    )
    assert make_url(os.environ["DATABASE_URL"]).database == actual


def test_worker_subprocess_inherits_test_database():
    """子进程(独立 worker 进程的等价路径)必须同样连到测试库。

    worker/e2e/恢复类测试用 subprocess 起真实 worker,它们经 os.environ 的
    DATABASE_URL 建引擎;若此处泄漏到应用库,故障注入会写脏真实库。
    """
    code = (
        "from sqlalchemy import text;"
        "from app.core.db import engine;"
        "c=engine.connect();"
        "print(c.execute(text('SELECT current_database()')).scalar())"
    )
    env = {**os.environ, "PYTHONUTF8": "1"}
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_ROOT, env=env,
        capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == TEST_DB, (
        f"子进程连到了 {out.stdout.strip()!r},应为 {TEST_DB!r}"
    )


def test_tests_do_not_inherit_real_providers_from_env():
    """防回归:应用 .env 里配了真实 Provider 时,测试仍必须跑在 mock 上。

    否则一次 .env 改动就会让集成测试去加载本地大模型、甚至调用付费服务,
    并把为该模型校准的检索阈值一起带进来(表现为无法解释的成片失败)。
    """
    from app.core.config import get_settings
    from tests.conftest import REAL_PROVIDER_TESTS

    s = get_settings()
    if REAL_PROVIDER_TESTS:
        pytest.skip("显式要求真实 Provider 集成测试")
    assert (s.asr_provider, s.llm_provider, s.embed_provider) == ("mock",)*3
    assert s.qa_evidence_threshold == 0.15
