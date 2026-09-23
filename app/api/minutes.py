"""纪要:查看 + 人工修订 + 按版本确认。

GET  /meetings/{id}/minutes                 最新版完整内容 + 全部版本摘要
POST /meetings/{id}/minutes/versions        基于某版追加人工修订版(并发送 409)
POST /meetings/{id}/minutes/confirm         确认**指定版本**
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db, engine
from app.core.deps import get_current_user
from app.models.minutes import MinutesVersion
from app.models.org import User
from app.models.recording import Recording
from app.models.transcript import TranscriptSegment, TranscriptVersion
from app.schemas.minutes import (
    MinutesConfirmIn,
    MinutesContentOut,
    MinutesDetailOut,
    MinutesEditIn,
    MinutesItemOut,
    MinutesVersionBrief,
    SegmentEvidenceOut,
)
from app.services import minutes_workflow
from app.services.access import get_accessible_meeting
from app.services.minutes_validator import MinutesValidationError

router = APIRouter(tags=["minutes"])


@router.get("/meetings/{meeting_id}/minutes", response_model=MinutesDetailOut)
def get_meeting_minutes(
    meeting_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """查看会谈纪要:最新版本完整内容 + 版本摘要。

    条目证据按 segment_id 解析为片段详情(限本会谈录音的版本链;
    片段落库后不可变,人工纠错/重转写不破坏历史引用的可解析性)。
    """
    meeting = get_accessible_meeting(db, user, meeting_id)
    if meeting is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="会谈不存在或无权访问"
        )

    latest = db.scalar(
        select(MinutesVersion)
        .where(MinutesVersion.meeting_id == meeting_id)
        .order_by(MinutesVersion.version_no.desc())
        .limit(1)
    )
    if latest is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="纪要尚未生成"
        )

    versions = db.scalars(
        select(MinutesVersion)
        .where(MinutesVersion.meeting_id == meeting_id)
        .order_by(MinutesVersion.version_no)
    ).all()

    raw_items = (latest.content or {}).get("items", [])
    needed_ids = {sid for it in raw_items for sid in it.get("evidence_segment_ids", [])}
    segments = {}
    if needed_ids:
        # 解析范围限定本会谈:片段必须经版本链归属该会谈的录音(防跨会谈探测)
        rows = db.execute(
            select(TranscriptSegment, TranscriptVersion.recording_id)
            .join(TranscriptVersion, TranscriptVersion.id == TranscriptSegment.version_id)
            .join(Recording, Recording.id == TranscriptVersion.recording_id)
            .where(Recording.meeting_id == meeting_id)
            .where(TranscriptSegment.id.in_(needed_ids))
        ).all()
        segments = {seg.id: (seg, rid) for seg, rid in rows}

    items = [
        MinutesItemOut(
            type=it.get("type", ""),
            text=it.get("text", ""),
            certainty=it.get("certainty", "discussed"),
            owner=it.get("owner"),
            due_date=it.get("due_date"),
            evidence_segment_ids=list(it.get("evidence_segment_ids", [])),
            needs_confirmation=bool(it.get("needs_confirmation", False)),
            evidence=[
                SegmentEvidenceOut(
                    segment_id=s.id, seq=s.seq, speaker=s.speaker,
                    text=s.text, start_ms=s.start_ms, end_ms=s.end_ms,
                    timestamp_precision=(s.timestamp_precision.value
                                         if s.timestamp_precision else None),
                    recording_id=rid,
                )
                for s, rid in (segments[sid] for sid in it.get("evidence_segment_ids", [])
                               if sid in segments)
            ],
        )
        for it in raw_items
    ]

    return MinutesDetailOut(
        meeting_id=meeting_id,
        id=latest.id,
        version_no=latest.version_no,
        source_type=latest.source_type.value,
        status=latest.status.value,
        created_at=latest.created_at,
        created_by=latest.created_by,
        confirmed_by=latest.confirmed_by,
        confirmed_at=latest.confirmed_at,
        content=MinutesContentOut(items=items),
        versions=[
            MinutesVersionBrief.model_validate(v) for v in versions
        ],
    )


@router.post("/meetings/{meeting_id}/minutes/versions",
             response_model=MinutesVersionBrief,
             status_code=status.HTTP_201_CREATED)
def edit_minutes(
    meeting_id: int,
    body: MinutesEditIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """人工修订:追加 human_edit 新版本,AI 草稿原样保留。"""
    if get_accessible_meeting(db, user, meeting_id) is None:
        raise HTTPException(status_code=404, detail="会谈不存在或无权访问")
    try:
        return minutes_workflow.edit_minutes(
            engine, meeting_id, base_version_id=body.base_version_id,
            content=body.content, actor_id=user.id)
    except minutes_workflow.MinutesVersionNotFound:
        # 版本不存在 / 不属于本会谈 / 不属于可见会谈,对外同一形态,不泄露存在性
        raise HTTPException(status_code=404, detail="纪要版本不存在或无权访问")
    except minutes_workflow.StaleMinutesEdit as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "该纪要已被他人修改,请基于最新版本重试",
                "base_version_id": e.base_version_id,
                "latest_version_no": e.latest_version_no,
            },
        )
    except MinutesValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/meetings/{meeting_id}/minutes/confirm",
             response_model=MinutesVersionBrief)
def confirm_minutes(
    meeting_id: int,
    body: MinutesConfirmIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """确认指定版本(不是"整个会谈的纪要")。同版本重复确认幂等。"""
    if get_accessible_meeting(db, user, meeting_id) is None:
        raise HTTPException(status_code=404, detail="会谈不存在或无权访问")
    try:
        return minutes_workflow.confirm_minutes(
            engine, meeting_id, version_id=body.version_id, actor_id=user.id)
    except minutes_workflow.MinutesVersionNotFound:
        raise HTTPException(status_code=404, detail="纪要版本不存在或无权访问")
