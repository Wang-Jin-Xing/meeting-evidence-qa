"""测试库隔离守卫:在任何建表/清表动作之前完成,禁止回退到应用库。

规则(缺失或不满足即退出,不执行 TRUNCATE):
1. 必须显式提供 TEST_DATABASE_URL(环境变量或 .env),不从 database_url 推导;
2. 测试库与应用库的 (host, port, database) 三元组不得相同,库名也不得相同;
3. 实际连接后以 current_database() 复核(不信 URL 字符串,信数据库自报);
4. 迁移只应用到测试库。

本模块只依赖 stdlib / sqlalchemy / alembic,且不得在模块级导入 app.core.db
(engine 在导入时即按当前配置创建,切换配置必须发生在该导入之前)。
"""
import os
import re
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

REPO_ROOT = Path(__file__).resolve().parents[1]
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# setup_test_database() 执行后填充:切换前解析到的应用库名,
# 供测试断言"当前连接确实不是应用库"(settings 已被改指向测试库)。
APP_DATABASE_NAME: str | None = None


class TestDatabaseConfigError(RuntimeError):
    """测试库配置缺失或不安全——调用方必须立即终止测试。"""


def _load_settings():
    """读取应用配置(app.core.config 无副作用,不创建引擎)。"""
    from app.core.config import get_settings

    return get_settings()


def _require_test_url(settings) -> str:
    raw = (os.environ.get("TEST_DATABASE_URL") or "").strip() or (
        settings.test_database_url or ""
    ).strip()
    if not raw:
        raise TestDatabaseConfigError(
            "缺少 TEST_DATABASE_URL,测试拒绝运行(避免误清应用数据库)。\n"
            "请在 .env 或环境变量中显式指定独立测试库,例如:\n"
            "  TEST_DATABASE_URL=postgresql+psycopg://<user>@localhost:5432"
            "/meeting_evidence_test\n"
            "模板见 .env.example;首次运行会自动建库并应用迁移。"
        )
    return raw


def _ensure_database_exists(test_url) -> None:
    """幂等建库(连接维护库 postgres);已存在则跳过。"""
    if not test_url.database or not _IDENT.match(test_url.database):
        raise TestDatabaseConfigError(
            f"测试库名不合法,无法安全建库: {test_url.database!r}"
        )
    admin = create_engine(
        test_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        with admin.connect() as conn:
            if not conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :db"),
                {"db": test_url.database},
            ).fetchone():
                conn.execute(text(f'CREATE DATABASE "{test_url.database}"'))
    finally:
        admin.dispose()


def _probe(test_url):
    """返回 (current_database, 编码)——以数据库自报为准。"""
    eng = create_engine(test_url, isolation_level="AUTOCOMMIT")
    try:
        with eng.connect() as conn:
            return (
                conn.execute(text("SELECT current_database()")).scalar(),
                conn.execute(
                    text(
                        "SELECT pg_encoding_to_char(encoding) FROM pg_database"
                        " WHERE datname = current_database()"
                    )
                ).scalar(),
            )
    finally:
        eng.dispose()


def setup_test_database() -> str:
    """校验并切换到独立测试库,返回已核实的 current_database() 名称。

    配置缺失或不安全时抛 TestDatabaseConfigError,由 conftest 立即终止会话。
    """
    global APP_DATABASE_NAME
    settings = _load_settings()
    app_url = make_url(settings.database_url)
    raw_test = _require_test_url(settings)
    test_url = make_url(raw_test)
    APP_DATABASE_NAME = app_url.database

    if not test_url.database:
        raise TestDatabaseConfigError("TEST_DATABASE_URL 未指定数据库名。")
    if (test_url.host, test_url.port, test_url.database) == (
        app_url.host,
        app_url.port,
        app_url.database,
    ):
        raise TestDatabaseConfigError(
            f"测试库与应用库指向同一数据库"
            f"({app_url.render_as_string(hide_password=True)}),拒绝运行测试。"
        )
    if test_url.database == app_url.database:
        raise TestDatabaseConfigError(
            f"测试库名与应用库名相同({test_url.database}),"
            "即便主机不同也拒绝运行(防止误指向生产库)。"
        )

    _ensure_database_exists(test_url)
    actual, encoding = _probe(test_url)
    if actual != test_url.database:
        raise TestDatabaseConfigError(
            f"实际连接的数据库为 {actual!r},与配置的 {test_url.database!r} 不符。"
        )
    if str(encoding).upper() != "UTF8":
        raise TestDatabaseConfigError(
            f"测试库编码为 {encoding},非 UTF8(中文语料与检索结果不可靠)。"
        )

    # 切换应用配置:此后(重新)导入的 app.core.db.engine 指向测试库,
    # worker 子进程经继承的 DATABASE_URL 走同一测试库。
    # 注意用原始字符串而非 str(URL)——后者会把密码渲染为 ***。
    os.environ["DATABASE_URL"] = raw_test
    from app.core.config import get_settings

    get_settings.cache_clear()
    _migrate_test_database()
    _acquire_exclusive_lock(test_url)
    return actual


_LOCK_KEY = 0x4D454554  # 'MEET':同一测试库同时只允许一个 pytest 会话

_HELD_LOCK = []  # 持有连接引用,进程结束自动释放


def _acquire_exclusive_lock(test_url) -> None:
    """测试库独占:并发跑两个 pytest 会话会互相 TRUNCATE,产生无法解释的
    外键违规等假故障。用会话级 advisory lock 让第二个会话直接拒绝启动。"""
    conn = create_engine(test_url, isolation_level="AUTOCOMMIT").connect()
    got = conn.execute(
        text("SELECT pg_try_advisory_lock(:k)"), {"k": _LOCK_KEY}
    ).scalar()
    if not got:
        conn.close()
        raise TestDatabaseConfigError(
            f"另一个 pytest 会话正持有测试库 {test_url.database} 的独占锁。"
            "测试库不允许并发运行(会互相清表),请等其结束或另配一个测试库。"
        )
    _HELD_LOCK.append(conn)


def _migrate_test_database() -> None:
    """只迁移测试库:env.py 的 URL 取自应用配置,此刻已指向测试库。"""
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    command.upgrade(cfg, "head")
