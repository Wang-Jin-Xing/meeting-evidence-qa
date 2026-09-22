from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.org import User
from app.schemas.recording import JobOut, StageOut, UploadAccepted
from app.services import recording_service
from app.services.access import get_accessible_meeting

router = APIRouter(tags=["recordings"])


@router.post(
    "/meetings/{meeting_id}/recordings",
    response_model=UploadAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def upload_recording(
    meeting_id: int,
    file: UploadFile,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """上传录音:仅落盘与建任务,模型调用全部由后台 worker 执行。"""
    meeting = get_accessible_meeting(db, user, meeting_id)
    if meeting is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="会谈不存在或无权访问"
        )
    try:
        recording, job = recording_service.save_recording(db, meeting, file, user)
    except recording_service.UnsupportedFileTypeError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except recording_service.FileTooLargeError as e:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(e)
        )
    except recording_service.DuplicateRecordingError as e:
        # 重复提交不创建新任务,返回已有 recording/job 信息
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "该会谈已存在相同内容的录音",
                "existing_recording_id": e.existing.id,
                "existing_job_id": e.existing_job_id,
            },
        )
    return UploadAccepted(
        recording_id=recording.id,
        job_id=job.id,
        job_status=job.status.value,
    )


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(
    job_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """任务状态与各阶段进度。访问范围跟随录音所属会谈。"""
    from sqlalchemy import select

    from app.models.recording import ProcessingJob, Recording
    from app.services.access import visible_meeting_condition

    job = db.scalar(
        select(ProcessingJob)
        .join(ProcessingJob.recording)
        .join(Recording.meeting)
        .where(ProcessingJob.id == job_id)
        .where(visible_meeting_condition(user))
    )
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在或无权访问"
        )
    return JobOut(
        id=job.id,
        recording_id=job.recording_id,
        meeting_id=job.recording.meeting_id,
        status=job.status.value,
        current_stage=job.current_stage,
        attempt_count=job.attempt_count,
        last_error=job.last_error,
        stages=[
            StageOut(
                stage_name=s.stage_name.value,
                status=s.status.value,
                attempt_count=s.attempt_count,
                started_at=s.started_at.isoformat() if s.started_at else None,
                finished_at=s.finished_at.isoformat() if s.finished_at else None,
                error=s.error,
            )
            for s in job.stages
        ],
    )
