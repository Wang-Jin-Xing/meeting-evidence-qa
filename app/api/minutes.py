"""纪要查看:GET /meetings/{id}/minutes。"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.minutes import MinutesVersion
from app.models.org import User
from app.models.recording import Recording
from app.models.transcript import TranscriptSegment, TranscriptVersion
from app.schemas.minutes import (
    MinutesContentOut,
    MinutesDetailOut,
    MinutesItemOut,
    MinutesVersionBrief,
    SegmentEvidenceOut,
)
from app.services.access import get_accessible_meeting

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
        rows = db.scalars(
            select(TranscriptSegment)
            .join(TranscriptVersion, TranscriptVersion.id == TranscriptSegment.version_id)
            .join(Recording, Recording.id == TranscriptVersion.recording_id)
            .where(Recording.meeting_id == meeting_id)
            .where(TranscriptSegment.id.in_(needed_ids))
        ).all()
        segments = {s.id: s for s in rows}

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
                )
                for s in (segments[sid] for sid in it.get("evidence_segment_ids", [])
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
        content=MinutesContentOut(items=items),
        versions=[
            MinutesVersionBrief.model_validate(v) for v in versions
        ],
    )
