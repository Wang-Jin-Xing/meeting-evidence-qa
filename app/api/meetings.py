from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.meeting import Meeting
from app.models.org import MeetingAccess, User
from app.schemas.meeting import MeetingCreate, MeetingDetail, MeetingOut, RecordingBrief
from app.services.access import get_accessible_meeting, visible_meeting_condition

router = APIRouter(prefix="/meetings", tags=["meetings"])


@router.post("", response_model=MeetingOut, status_code=status.HTTP_201_CREATED)
def create_meeting(
    body: MeetingCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """创建会谈。org_id/created_by 一律取自后端身份,不读请求体。"""
    meeting = Meeting(
        org_id=user.org_id,  # 后端身份决定机构
        title=body.title,
        vendor_name=body.vendor_name,
        topic=body.topic,
        scheduled_at=body.scheduled_at,
        visibility=body.visibility,
        created_by=user.id,
    )
    db.add(meeting)
    db.flush()

    if body.visibility == "restricted" and body.access_user_ids:
        # 白名单用户必须属于本机构,防止跨机构授权
        ids = set(body.access_user_ids)
        org_user_ids = set(
            db.scalars(
                select(User.id).where(User.org_id == user.org_id, User.id.in_(ids))
            ).all()
        )
        invalid = ids - org_user_ids
        if invalid:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"白名单用户不属于本机构: {sorted(invalid)}",
            )
        db.add_all(
            MeetingAccess(user_id=uid, meeting_id=meeting.id) for uid in ids
        )

    db.commit()
    db.refresh(meeting)
    return meeting


@router.get("", response_model=list[MeetingOut])
def list_meetings(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """仅返回当前用户有权访问的会谈(机构 + 可见性条件在 SQL 中过滤)。"""
    return db.scalars(
        select(Meeting)
        .where(visible_meeting_condition(user))
        .order_by(Meeting.scheduled_at.desc())
    ).all()


@router.get("/{meeting_id}", response_model=MeetingDetail)
def get_meeting(
    meeting_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    meeting = get_accessible_meeting(db, user, meeting_id)
    if meeting is None:
        # 无权与不存在统一 404,不泄露存在性
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会谈不存在或无权访问")

    recordings = []
    for rec in meeting.recordings:
        job = rec.job
        recordings.append(
            RecordingBrief(
                id=rec.id,
                original_filename=rec.original_filename,
                checksum=rec.checksum,
                job_id=job.id if job else None,
                job_status=job.status.value if job else None,
                job_current_stage=job.current_stage if job else None,
            )
        )
    # 会话内先按 MeetingOut 取标量字段,再显式挂 recordings:
    # 直接 model_validate(meeting) 会把 ORM Recording 对象当 RecordingBrief 校验,
    # 而 job_* 是派生字段(只存在于 ProcessingJob),必然 500。
    base = MeetingOut.model_validate(meeting).model_dump()
    return MeetingDetail(**base, recordings=recordings)
