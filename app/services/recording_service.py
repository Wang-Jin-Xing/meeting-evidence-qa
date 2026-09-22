"""录音上传:流式落盘 + SHA-256 查重 + 任务持久化。

上传请求内只做 IO 与建行,不调用任何模型服务(设计约定)。
"""
import hashlib
import os
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.enums import JobStatus, STAGE_ORDER, StageName, StageStatus
from app.models.meeting import Meeting
from app.models.org import User
from app.models.recording import JobStage, ProcessingJob, Recording

# 允许的音频扩展名(内容校验在 P1.2 prepare 阶段进行,决策 D-07)
ALLOWED_EXTS = {".wav", ".mp3", ".m4a", ".ogg", ".flac", ".aac", ".webm"}

_CHUNK_SIZE = 1024 * 1024  # 1MB


class DuplicateRecordingError(Exception):
    """同会谈下相同内容(checksum)的录音已存在。"""

    def __init__(self, existing: Recording, existing_job_id: int | None):
        self.existing = existing
        self.existing_job_id = existing_job_id


class FileTooLargeError(Exception):
    pass


class UnsupportedFileTypeError(Exception):
    pass


def _storage_dir() -> Path:
    d = Path(get_settings().storage_dir) / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_recording(
    db: Session, meeting: Meeting, file, user: User
) -> tuple[Recording, ProcessingJob]:
    """流式保存上传文件并创建任务。

    抛出:UnsupportedFileTypeError / FileTooLargeError / DuplicateRecordingError。
    """
    settings = get_settings()
    filename = file.filename or "upload"
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise UnsupportedFileTypeError(f"不支持的音频类型: {ext or '(无扩展名)'}")

    max_bytes = settings.max_upload_mb * 1024 * 1024
    tmp_path = _storage_dir() / f"tmp-{uuid.uuid4().hex}"
    hasher = hashlib.sha256()
    size = 0
    try:
        with tmp_path.open("wb") as out:
            while chunk := file.file.read(_CHUNK_SIZE):
                size += len(chunk)
                if size > max_bytes:
                    raise FileTooLargeError(
                        f"文件超过 {settings.max_upload_mb}MB 上限"
                    )
                hasher.update(chunk)
                out.write(chunk)
        checksum = hasher.hexdigest()

        # 重复提交检测:同 meeting + 同 checksum → 拒绝,返回已有 recording
        dup = db.scalar(
            select(Recording).where(
                Recording.meeting_id == meeting.id,
                Recording.checksum == checksum,
            )
        )
        if dup is not None:
            raise DuplicateRecordingError(dup, dup.job.id if dup.job else None)

        final_path = _storage_dir() / f"{uuid.uuid4().hex}{ext}"
        os.replace(tmp_path, final_path)

        recording = Recording(
            meeting_id=meeting.id,
            file_path=str(final_path),
            original_filename=filename,
            checksum=checksum,
            size_bytes=size,
            mime=file.content_type,
            created_by=user.id,
        )
        job = ProcessingJob(
            recording=recording,
            status=JobStatus.queued,
            current_stage=StageName.prepare.value,
            lease_epoch=0,
            attempt_count=0,
        )
        stages = [
            JobStage(job=job, stage_name=stage, status=StageStatus.pending)
            for stage in STAGE_ORDER
        ]
        db.add_all([recording, job, *stages])
        db.commit()
        db.refresh(recording)
        db.refresh(job)
        return recording, job
    finally:
        # 无论成功失败,清理临时文件(成功路径已被 os.replace 移走)
        if tmp_path.exists():
            tmp_path.unlink()
