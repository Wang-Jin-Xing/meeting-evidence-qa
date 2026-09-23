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


# ---------- 证据回放:浏览器可播放的音频表示 ----------

class AudioFileMissingError(Exception):
    """录音登记的文件不在磁盘上(原件被外部移动/删除)。"""


def _derived_dir() -> Path:
    d = Path(get_settings().storage_dir) / "derived"
    d.mkdir(parents=True, exist_ok=True)
    return d


def read_wav_meta(path: Path) -> dict | None:
    """读取 WAV 的声道/位宽/采样率/帧数;不是整数 PCM 时返回 None(不做转换)。"""
    import wave

    try:
        with wave.open(str(path), "rb") as w:
            if w.getcomptype() != "NONE":  # 压缩或浮点格式:不猜,交给浏览器自己试
                return None
            return {
                "channels": w.getnchannels(),
                "sample_width": w.getsampwidth(),
                "sample_rate": w.getframerate(),
                "n_frames": w.getnframes(),
            }
    except wave.Error:
        return None


def _to_16bit(src: Path, meta: dict, dst: Path) -> None:
    """按位宽降位到 16-bit PCM,**保持帧数/采样率/声道不变**。

    只改位深不改时间轴:派生副本与原件帧数逐帧相等,
    因此界面按同一 start_ms/end_ms 定位不会错位。
    """
    import wave

    import numpy as np

    width = meta["sample_width"]
    with wave.open(str(src), "rb") as w:
        raw = w.readframes(meta["n_frames"])

    if width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = np.where(v >= (1 << 23), v - (1 << 24), v)
        pcm = np.right_shift(v, 8).astype("<i2").tobytes()   # 24→16 丢低位
    elif width == 4:
        v = np.frombuffer(raw, dtype="<i4")
        pcm = np.right_shift(v, 16).astype("<i2").tobytes()
    else:
        raise ValueError(f"不需要降位的位宽: {width * 8} bit")

    tmp = dst.with_name(f"{dst.name}.tmp-{uuid.uuid4().hex}")
    with wave.open(str(tmp), "wb") as out:
        out.setnchannels(meta["channels"])
        out.setsampwidth(2)
        out.setframerate(meta["sample_rate"])
        out.writeframes(pcm)
    os.replace(tmp, dst)


def playable_audio(rec: Recording) -> tuple[Path, dict]:
    """返回浏览器可播放的文件与**实际所提供字节**的元信息。

    原件永不修改:16-bit PCM 直接给原件;24/32-bit 整数 PCM 生成 16-bit 派生副本
    (多数浏览器不放 24-bit WAV)。副本按 checksum 缓存,写完立即复核帧数、
    采样率与声道与原件一致 —— 时间轴对不上就等于回放是错的。
    """
    src = Path(rec.file_path)
    if not src.exists():
        raise AudioFileMissingError(rec.file_path)

    meta = read_wav_meta(src)
    base = {
        "original_sample_width": (meta or {}).get("sample_width"),
        "sample_rate": (meta or {}).get("sample_rate"),
        "channels": (meta or {}).get("channels"),
        "n_frames": (meta or {}).get("n_frames"),
    }
    if meta is None or meta["sample_width"] == 2:
        # 非 WAV / 已是 16-bit:无需转换,原样提供(不谎称做过兼容处理)
        return src, {**base, "source": "original",
                     "sample_width": meta["sample_width"] if meta else None}

    dst = _derived_dir() / f"{rec.checksum}.s{meta['channels']}r{meta['sample_rate']}16.wav"
    if not dst.exists():
        _to_16bit(src, meta, dst)

    got = read_wav_meta(dst)
    if (got["n_frames"] != meta["n_frames"] or got["sample_rate"] != meta["sample_rate"]
            or got["channels"] != meta["channels"] or got["sample_width"] != 2):
        dst.unlink(missing_ok=True)
        raise RuntimeError("派生播放副本校验失败:时间轴或格式与原件不一致")

    return dst, {**base, "source": "derived_16bit_pcm", "sample_width": 2}


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
