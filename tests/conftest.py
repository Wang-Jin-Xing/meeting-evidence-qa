"""集成测试夹具:连接独立测试 PostgreSQL(仅模型服务为 mock)。

会话开始前 TRUNCATE 全部业务表保证可重复执行。
决策 D-06(修订):TRUNCATE 只允许发生在独立测试库上——tests/db_guard.py
要求显式 TEST_DATABASE_URL 并以 current_database() 复核,校验不通过则
整个会话立即退出,不执行任何清表动作。
"""
import io
import itertools
import wave
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

# 必须先于 app.core.db 导入:engine 在该模块导入时按当时配置创建
from tests.db_guard import TestDatabaseConfigError, setup_test_database

try:
    TEST_DB = setup_test_database()
except TestDatabaseConfigError as e:
    pytest.exit(reason=f"测试库隔离校验未通过:\n{e}", returncode=1)

from fastapi.testclient import TestClient  # noqa: E402

from app.core.db import SessionLocal, engine  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models.enums import MeetingVisibility, UserRole  # noqa: E402
from app.models.meeting import Meeting  # noqa: E402
from app.models.org import MeetingAccess, Organization, User  # noqa: E402

ALL_TABLES = [
    "query_log", "embedding_chunk", "segment_mapping", "transcript_segment",
    "transcript_version", "minutes_version", "model_run", "job_stage",
    "processing_job", "recording", "meeting_access", "meeting",
    "user", "organization",
]

_counter = itertools.count(1)


def _uniq(prefix: str) -> str:
    return f"{prefix}-{next(_counter)}"


def assert_isolated_database() -> str:
    """清表前置守卫:实际连接的库必须是守卫已核实的测试库。"""
    with engine.connect() as conn:
        actual = conn.execute(text("SELECT current_database()")).scalar()
    if actual != TEST_DB:
        pytest.exit(
            reason=f"拒绝清表:当前连接库 {actual!r} != 已核实测试库 {TEST_DB!r}"
        )
    return actual


@pytest.fixture(scope="session", autouse=True)
def clean_database():
    # user 是 PG 保留字,手写 SQL 需引号(ORM 自动处理)
    assert_isolated_database()
    quoted = ", ".join(f'"{t}"' for t in ALL_TABLES)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {quoted} RESTART IDENTITY CASCADE"))


@pytest.fixture(autouse=True)
def no_stale_pending_jobs():
    """每个测试前把遗留 queued/running 任务置终态(全局,消除跨模块顺序依赖)。

    防护:恢复/故障类测试中断后会留下可领取任务,后续测试模块的 worker
    按 created_at 抢走最老残留,导致故障注入错位。放在 conftest 而非各模块,
    使任意执行顺序(含单文件运行)下行为一致。succeeded/failed 不在领取候选内。
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE processing_job SET status = 'failed',"
                " last_error = '测试间清理:残留任务置终态'"
                " WHERE status IN ('queued', 'running')"
            )
        )
    yield


@pytest.fixture
def session():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


# ---- 数据工厂(直接经 ORM 写库并提交) ----

def make_org(session, name=None) -> Organization:
    org = Organization(name=name or _uniq("org"))
    session.add(org)
    session.commit()
    return org


def make_user(session, org, role=UserRole.staff, username=None) -> User:
    user = User(
        org_id=org.id,
        username=username or _uniq("user"),
        password_hash=hash_password("pass1234"),
        display_name=username or _uniq("用户"),
        role=role,
    )
    session.add(user)
    session.commit()
    return user


def make_meeting(session, org, created_by, visibility=MeetingVisibility.org,
                 access_user_ids=()) -> Meeting:
    meeting = Meeting(
        org_id=org.id,
        title=_uniq("会谈"),
        vendor_name="虚构厂商",
        topic="设备采购",
        scheduled_at=datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc).replace(tzinfo=None),
        visibility=visibility,
        created_by=created_by.id,
    )
    session.add(meeting)
    session.flush()
    for uid in access_user_ids:
        session.add(MeetingAccess(user_id=uid, meeting_id=meeting.id))
    session.commit()
    return meeting


# ---- 测试辅助 ----

def auth_headers(client, username, password="pass1234") -> dict:
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, f"登录失败: {resp.text}"
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def make_wav_bytes(seconds: float = 1.0, sample_rate: int = 8000) -> bytes:
    """生成真实 WAV(静音),供上传测试使用。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\x00\x00" * int(sample_rate * seconds))
    return buf.getvalue()


# ---- 进程内驱动 worker 阶段的公共辅助(worker 子进程路径另有 e2e/恢复测试) ----

def upload_recording(client, session, org, user, seconds: float = 25):
    """建会谈 + 上传录音,返回 (meeting, job_id)。25s → 3 个分片。"""
    meeting = make_meeting(session, org, user)
    resp = client.post(
        f"/meetings/{meeting.id}/recordings",
        files={"file": (f"rec-{meeting.id}.wav",
                        make_wav_bytes(seconds=seconds), "audio/wav")},
        headers=auth_headers(client, user.username),
    )
    assert resp.status_code == 202, resp.text
    return meeting, resp.json()["job_id"]


def claim_and_load(job_id: int, ttl: int = 120):
    """领取任务并装载阶段上下文,返回 (epoch, JobContext)。"""
    from app.core.db import engine
    from app.worker.lease import claim_job, read_lease, worker_id
    from app.worker.stages import load_context

    wid = worker_id()
    claimed = claim_job(engine, wid, ttl_seconds=ttl)
    assert claimed == job_id, f"应领取到 job {job_id},实际 {claimed}"
    epoch, _ = read_lease(engine, job_id)
    return epoch, load_context(engine, job_id)


@pytest.fixture
def two_orgs(session):
    """两个机构 × 各一管理员一普通用户,用于权限隔离测试。"""
    org_a = make_org(session, _uniq("机构A"))
    org_b = make_org(session, _uniq("机构B"))
    a_admin = make_user(session, org_a, UserRole.org_admin)
    a_staff = make_user(session, org_a, UserRole.staff)
    b_admin = make_user(session, org_b, UserRole.org_admin)
    b_staff = make_user(session, org_b, UserRole.staff)
    return {
        "org_a": org_a, "org_b": org_b,
        "a_admin": a_admin, "a_staff": a_staff,
        "b_admin": b_admin, "b_staff": b_staff,
    }
