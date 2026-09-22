"""集成测试夹具:直连本地 PostgreSQL(真实库,mock 仅替换模型服务)。

会话开始前 TRUNCATE 全部业务表保证可重复执行(本地开发库专用,决策 D-06)。
"""
import io
import itertools
import wave
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.db import SessionLocal, engine
from app.core.security import hash_password
from app.main import app
from app.models.enums import MeetingVisibility, UserRole
from app.models.meeting import Meeting
from app.models.org import MeetingAccess, Organization, User

ALL_TABLES = [
    "query_log", "embedding_chunk", "segment_mapping", "transcript_segment",
    "transcript_version", "minutes_version", "model_run", "job_stage",
    "processing_job", "recording", "meeting_access", "meeting",
    "user", "organization",
]

_counter = itertools.count(1)


def _uniq(prefix: str) -> str:
    return f"{prefix}-{next(_counter)}"


@pytest.fixture(scope="session", autouse=True)
def clean_database():
    # user 是 PG 保留字,手写 SQL 需引号(ORM 自动处理)
    quoted = ", ".join(f'"{t}"' for t in ALL_TABLES)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {quoted} RESTART IDENTITY CASCADE"))


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
