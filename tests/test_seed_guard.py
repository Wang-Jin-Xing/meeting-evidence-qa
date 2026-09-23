"""演示脚本的重置护栏:reset 只能清演示环境。

`--reset` 是对业务表做 TRUNCATE ... RESTART IDENTITY CASCADE,而真实录音链路、
入口验收与双链路对比那几条会谈和应用演示数据同库共存 —— 没有这道闸,
一条"重建演示数据"的命令就会把不可重造的验收数据一起清掉。
"""
import re

import pytest
from sqlalchemy import func, select

from app.core.db import SessionLocal
from app.models.enums import UserRole
from app.models.meeting import Meeting
from demo.seed_demo import DEMO_ORG_NAMES, reset_database
from tests.conftest import _uniq, make_meeting, make_org, make_user


def _one_non_demo_meeting():
    """造一条不属于演示白名单的会谈,返回 (会话, 会谈 id);会话由调用方关闭。"""
    db = SessionLocal()
    org = make_org(db, _uniq("真实录音会谈"))
    admin = make_user(db, org, UserRole.org_admin)
    return db, make_meeting(db, org, admin).id


def test_reset_refuses_when_nondemo_data_present():
    db, mid = _one_non_demo_meeting()
    try:
        with pytest.raises(SystemExit) as got:
            reset_database(db_name_check=False)
        msg = str(got.value)
        assert "拒绝执行" in msg and "非演示" in msg
        # 提示要给出会被清掉的数量,让人判断要不要加 --force
        listed = int(re.search(r"有 (\d+) 条", msg).group(1))
        assert listed >= 1
        # 被拒绝时不能已经清了
        db.expire_all()
        assert db.scalar(select(func.count(Meeting.id)).where(Meeting.id == mid)) == 1
    finally:
        db.close()


def test_force_proceeds_and_demo_only_needs_no_force():
    # 纯演示数据:不加 --force 也应放行
    db = SessionLocal()
    try:
        reset_database(db_name_check=False, force=True)     # 确定性起点(清场)
    finally:
        db.close()

    db = SessionLocal()
    try:
        org = make_org(db, sorted(DEMO_ORG_NAMES)[0])
        admin = make_user(db, org, UserRole.org_admin)
        make_meeting(db, org, admin)
        reset_database(db_name_check=False)                  # 不应抛错
        db.expire_all()
        assert db.scalar(select(func.count(Meeting.id))) == 0
    finally:
        db.close()

    # 脏数据 + --force:显式确认后放行
    db, mid = _one_non_demo_meeting()
    try:
        reset_database(db_name_check=False, force=True)
        db.expire_all()
        assert db.scalar(select(func.count(Meeting.id)).where(Meeting.id == mid)) == 0
    finally:
        db.close()
