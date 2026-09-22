"""会谈访问范围判定。

权限由后端身份(User.org_id / role / meeting_access)决定;
所有会谈/录音/任务接口复用这里的条件,不信任前端传入的机构 ID。
"""
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.models.meeting import Meeting
from app.models.org import MeetingAccess, User


def visible_meeting_condition(user: User):
    """返回当前用户可见会谈的 WHERE 条件(用于列表查询与单条访问)。"""
    from sqlalchemy import and_, or_

    return and_(
        Meeting.org_id == user.org_id,
        or_(
            Meeting.visibility == "org",
            Meeting.created_by == user.id,
            Meeting.id.in_(
                select(MeetingAccess.meeting_id).where(
                    MeetingAccess.user_id == user.id
                )
            ),
            # org_admin 恒可见本机构全部(含 restricted)
            user.role == "org_admin",
        ),
    )


def get_accessible_meeting(db: Session, user: User, meeting_id: int) -> Meeting | None:
    """按访问范围取会谈;无权或不存在统一返回 None(不区分,防探测)。"""
    stmt = (
        select(Meeting)
        .where(Meeting.id == meeting_id)
        .where(visible_meeting_condition(user))
    )
    return db.scalar(stmt)


def visible_meeting_ids_subquery(user: User) -> Select:
    """当前用户可见会谈 ID 子查询(录音/任务等关联查询复用)。"""
    return select(Meeting.id).where(visible_meeting_condition(user))
