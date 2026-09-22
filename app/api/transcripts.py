"""人工转写导入:POST /meetings/{id}/transcript。

为什么需要它:ASR 服务商不可用时(欠费/无凭证),处理闭环仍要能跑通——
转写来源换成人工导入,纪要与问答仍走配置指定的真实 Provider。
来源在响应与界面中如实标注(human_transcription / human_correction),
不与自动转写混记;失败一律显式返回,不回退 mock。
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.providers import ProviderResultUnknownError
from app.ai.registry import ProviderConfigError, build_providers, describe_providers
from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.org import User
from app.models.recording import Recording
from app.schemas.transcript import TranscriptImportOut, TranscriptIn
from app.services import transcript_correction
from app.services.access import get_accessible_meeting
from app.services.minutes_validator import MinutesValidationError
from app.services.budget import BudgetExceededError, BudgetUnaccountedError

router = APIRouter(tags=["transcript"])


@router.post(
    "/meetings/{meeting_id}/transcript",
    response_model=TranscriptImportOut,
    status_code=status.HTTP_201_CREATED,
)
def import_transcript(
    meeting_id: int,
    body: TranscriptIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """导入人工转写(无转写版本时建首版;已有版本时追加校正版)。

    随后生成结构化纪要并重建检索索引(同一条 Provider 路径与记账口径)。
    """
    meeting = get_accessible_meeting(db, user, meeting_id)
    if meeting is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="会谈不存在或无权访问"
        )
    stmt = select(Recording).where(Recording.meeting_id == meeting.id)
    if body.recording_id is not None:
        stmt = stmt.where(Recording.id == body.recording_id)
    recording = db.execute(
        stmt.order_by(Recording.created_at.desc()).limit(1)
    ).scalar_one_or_none()
    if recording is None:
        # 指定了不属于本会谈的 recording_id 同样 404(不区分,防探测)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="该会谈下没有可导入转写的录音(请先上传录音)",
        )

    try:
        _asr, llm, embed = build_providers()
    except ProviderConfigError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"模型配置不可用(不会回退 mock):{e}",
        )

    lines = [(ln.speaker, ln.text) for ln in body.lines]
    try:
        result = transcript_correction.publish_human_transcript(
            db.get_bind(),
            recording_id=recording.id,
            lines=lines,
            created_by=user.id,
            llm=llm if body.generate_minutes else None,
            embed=embed,
        )
    except (BudgetExceededError, BudgetUnaccountedError) as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"模型费用预算限制生效,未发出调用:{e}",
        )
    except transcript_correction.CorrectionError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except MinutesValidationError as e:
        # 模型输出未通过结构化校验(层1):如实失败,不落半成品、不改用 mock
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"模型输出未通过纪要结构校验:{e}",
        )
    except ProviderResultUnknownError as e:
        # 请求已发出但结果未知:如实告知(可能已计费),不重试、不回退
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"模型调用结果未知:{e}",
        )
    except ProviderConfigError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e)
        )

    t = result["transcript"]
    return TranscriptImportOut(
        meeting_id=meeting.id,
        recording_id=recording.id,
        transcript_version_id=t["version_id"],
        source_type=t.get("source_type", "human_correction"),
        segments=t["segments"],
        edited=t["edited"],
        minutes=result.get("minutes"),
        index=result["index"],
        providers=describe_providers((_asr, llm, embed)),
    )
