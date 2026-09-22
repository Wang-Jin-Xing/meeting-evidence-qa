"""录音上传、任务持久化与访问控制测试。"""
import hashlib
from pathlib import Path

from app.models.recording import ProcessingJob, Recording
from tests.conftest import auth_headers, make_meeting, make_wav_bytes


def _upload(client, headers, meeting_id, content=None, filename="demo.wav"):
    content = content or make_wav_bytes(seconds=2)
    return client.post(
        f"/meetings/{meeting_id}/recordings",
        files={"file": (filename, content, "audio/wav")},
        headers=headers,
    )


def test_upload_creates_queued_job_without_model_calls(client, session, two_orgs):
    """上传仅落盘+建任务:job=queued、4 阶段 pending、无 model_run、文件存在。"""
    meeting = make_meeting(session, two_orgs["org_a"], two_orgs["a_admin"])
    headers = auth_headers(client, two_orgs["a_staff"].username)
    content = make_wav_bytes(seconds=2)

    resp = _upload(client, headers, meeting.id, content=content)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["job_status"] == "queued"
    assert body["duplicate"] is False

    job = session.get(ProcessingJob, body["job_id"])
    assert job.status == "queued"
    assert job.current_stage == "prepare"
    assert len(job.stages) == 4
    assert all(s.status == "pending" for s in job.stages)
    assert [s.stage_name.value for s in job.stages] == [
        "prepare", "transcribe", "minutes", "index"
    ]

    # 请求内不产生任何模型调用
    from sqlalchemy import select
    from app.models.model_run import ModelRun
    assert session.scalars(select(ModelRun)).all() == []

    # 文件已落盘且 checksum 与内容一致
    recording = session.get(Recording, body["recording_id"])
    assert Path(recording.file_path).exists()
    assert recording.checksum == hashlib.sha256(content).hexdigest()


def test_duplicate_upload_returns_409(client, session, two_orgs):
    """同会谈同内容重复上传:409,不新建 recording/job。"""
    meeting = make_meeting(session, two_orgs["org_a"], two_orgs["a_admin"])
    headers = auth_headers(client, two_orgs["a_admin"].username)
    content = make_wav_bytes(seconds=1)

    first = _upload(client, headers, meeting.id, content=content)
    assert first.status_code == 202
    second = _upload(client, headers, meeting.id, content=content)
    assert second.status_code == 409
    detail = second.json()["detail"]
    assert detail["existing_recording_id"] == first.json()["recording_id"]

    # 仍只有一个任务
    from sqlalchemy import select, func
    count = session.scalar(
        select(func.count()).select_from(Recording).where(Recording.meeting_id == meeting.id)
    )
    assert count == 1


def test_upload_rejects_non_audio(client, session, two_orgs):
    meeting = make_meeting(session, two_orgs["org_a"], two_orgs["a_admin"])
    headers = auth_headers(client, two_orgs["a_admin"].username)
    resp = _upload(
        client, headers, meeting.id,
        content=b"not audio", filename="note.txt",
    )
    assert resp.status_code == 400


def test_cross_org_upload_404(client, session, two_orgs):
    """机构 B 用户不能向机构 A 会谈上传。"""
    meeting_a = make_meeting(session, two_orgs["org_a"], two_orgs["a_admin"])
    b_headers = auth_headers(client, two_orgs["b_admin"].username)
    resp = _upload(client, b_headers, meeting_a.id)
    assert resp.status_code == 404


def test_job_status_endpoint_and_isolation(client, session, two_orgs):
    """任务状态可见性跟随会谈访问范围。"""
    meeting = make_meeting(session, two_orgs["org_a"], two_orgs["a_admin"])
    headers = auth_headers(client, two_orgs["a_admin"].username)
    upload = _upload(client, headers, meeting.id)
    job_id = upload.json()["job_id"]

    # 本机构可见,阶段列表完整
    resp = client.get(f"/jobs/{job_id}", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["meeting_id"] == meeting.id
    assert [s["stage_name"] for s in body["stages"]] == [
        "prepare", "transcribe", "minutes", "index"
    ]

    # 跨机构不可见
    b_headers = auth_headers(client, two_orgs["b_admin"].username)
    assert client.get(f"/jobs/{job_id}", headers=b_headers).status_code == 404


def test_upload_requires_authentication(client, session, two_orgs):
    meeting = make_meeting(session, two_orgs["org_a"], two_orgs["a_admin"])
    resp = client.post(
        f"/meetings/{meeting.id}/recordings",
        files={"file": ("a.wav", make_wav_bytes(), "audio/wav")},
    )
    assert resp.status_code == 401
