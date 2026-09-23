from fastapi import (
    APIRouter, Depends, HTTPException, Request, UploadFile, status,
)
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.meeting import Meeting
from app.models.org import User
from app.models.recording import Recording
from app.schemas.recording import JobOut, StageOut, UploadAccepted
from app.services import recording_service
from app.services.access import get_accessible_meeting, visible_meeting_condition

router = APIRouter(tags=["recordings"])

_CHUNK = 1024 * 1024


def _parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """解析单区间 Range。返回 (start, end) 闭区间;None=无 Range;…=无法满足。"""
    if not header or not header.startswith("bytes="):
        return (0, size - 1)
    spec = header[6:].split(",")[0].strip()
    if "-" not in spec:
        return None
    head, _, tail = spec.partition("-")
    try:
        if head == "":                      # bytes=-N  = 末尾 N 字节
            n = int(tail)
            if n <= 0:
                return None
            return (max(size - n, 0), size - 1)
        start = int(head)
        end = int(tail) if tail else size - 1
    except ValueError:
        return None
    if start >= size or end < start:
        return None
    return (start, min(end, size - 1))


@router.get("/recordings/{recording_id}/audio")
def get_recording_audio(
    recording_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """受权限保护的录音字节(证据回放用),支持单区间 Range 以便拖动进度条。

    - 权限:走 visible_meeting_condition,不可见一律 404(不区分"存在但无权");
    - 所服务的字节可能不是原件本身:24/32-bit 原件会降位成 16-bit 派生副本
      (浏览器普遍不放 24-bit WAV),原件始终只读。X-Audio-Source 如实标注;
    - 音频文件不经静态目录暴露:storage/ 不在任何 mount 之下,只能走本接口。
    """
    rec = db.scalar(
        select(Recording)
        .join(Meeting, Meeting.id == Recording.meeting_id)
        .where(Recording.id == recording_id)
        .where(visible_meeting_condition(user))
    )
    if rec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="录音不存在或无权访问"
        )
    try:
        path, meta = recording_service.playable_audio(rec)
    except recording_service.AudioFileMissingError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="录音登记的文件不在磁盘上(原件需从备份放回)",
        )

    size = path.stat().st_size
    rng = _parse_range(request.headers.get("range"), size)
    if rng is None:
        raise HTTPException(
            status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            headers={"Content-Range": f"bytes */{size}"},
            detail="Range 超出文件范围",
        )
    start, end = rng
    length = end - start + 1

    def body():
        with path.open("rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                block = f.read(min(_CHUNK, left))
                if not block:
                    break
                left -= len(block)
                yield block

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Content-Disposition": f'inline; filename="recording-{rec.id}.wav"',
        "X-Audio-Source": meta["source"],
        "X-Audio-Served-Bits": (str(meta["sample_width"] * 8)
                                if meta["sample_width"] else "unknown"),
        "X-Audio-Original-Bits": (str(meta["original_sample_width"] * 8)
                                  if meta["original_sample_width"] else "unknown"),
        "X-Audio-Sample-Rate": str(meta["sample_rate"] or "unknown"),
        "X-Audio-Channels": str(meta["channels"] or "unknown"),
        "X-Audio-Duration-Ms": (str(round(meta["n_frames"] * 1000 / meta["sample_rate"]))
                                if meta["n_frames"] and meta["sample_rate"] else "unknown"),
        "Cache-Control": "private, no-store",
    }
    if (start, end) != (0, size - 1):
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(
        body(),
        status_code=206 if rng != (0, size - 1) else 200,
        media_type=rec.mime or "audio/wav",
        headers=headers,
    )


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
