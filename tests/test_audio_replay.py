"""证据回放:受权限保护的音频接口、Range 与 24-bit 兼容播放副本。

口径:音频只能经鉴权接口取,不可经静态目录公开;需要降位时必须生成**派生副本**,
原件不动,且副本必须与原件帧数/采样率/声道一致(否则切片时间定位就是错的)。
"""
import io
import wave

from app.models.enums import MeetingVisibility
from tests.conftest import auth_headers, make_meeting, make_user, make_wav_bytes


def _wav(seconds=1.0, rate=8000, width=2, channels=1) -> bytes:
    """生成指定声道/位宽的静音 WAV(width=3 即 24-bit)。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(b"\x00" * (int(rate * seconds) * width * channels))
    return buf.getvalue()


def _upload(client, auth, meeting_id, content, name="t.wav"):
    resp = client.post(f"/meetings/{meeting_id}/recordings",
                       files={"file": (name, content, "audio/wav")}, headers=auth)
    assert resp.status_code == 202, resp.text
    return resp.json()["recording_id"]


def _audio(client, auth, rec_id, range_header=None):
    headers = dict(auth)
    if range_header:
        headers["Range"] = range_header
    return client.get(f"/recordings/{rec_id}/audio", headers=headers)


def _meeting_with_wav(client, session, two_orgs, owner_key, content):
    meeting = make_meeting(session, two_orgs["org_a"], two_orgs[owner_key])
    auth = auth_headers(client, two_orgs[owner_key].username)
    return meeting, auth, _upload(client, auth, meeting.id, content)


def test_audio_requires_auth(client, session, two_orgs):
    _, _, rec = _meeting_with_wav(client, session, two_orgs, "a_admin", make_wav_bytes())
    assert client.get(f"/recordings/{rec}/audio").status_code == 401


def test_audio_denied_for_other_org(client, session, two_orgs):
    """跨机构取音频必须 404(不区分"存在但无权",避免录音 ID 探测)。"""
    _, _, rec = _meeting_with_wav(client, session, two_orgs, "a_admin", make_wav_bytes())
    b_auth = auth_headers(client, two_orgs["b_staff"].username)
    assert _audio(client, b_auth, rec).status_code == 404


def test_restricted_audio_needs_whitelist(client, session, two_orgs):
    """restricted 会谈:同机构未授权用户也拿不到音频,加白名单后可以。"""
    from app.models.org import MeetingAccess

    meeting = make_meeting(session, two_orgs["org_a"], two_orgs["a_admin"],
                           visibility=MeetingVisibility.restricted)
    owner = auth_headers(client, two_orgs["a_admin"].username)
    rec = _upload(client, owner, meeting.id, make_wav_bytes())

    outsider = auth_headers(client, two_orgs["a_staff"].username)   # 同机构但未授权
    assert _audio(client, outsider, rec).status_code == 404

    grant = make_user(session, two_orgs["org_a"])
    session.add(MeetingAccess(user_id=grant.id, meeting_id=meeting.id))
    session.commit()
    assert _audio(client, auth_headers(client, grant.username), rec).status_code == 200


def test_full_get_advertises_ranges_and_provenance(client, session, two_orgs):
    _, auth, rec = _meeting_with_wav(client, session, two_orgs, "a_admin",
                                     make_wav_bytes(seconds=2))
    resp = _audio(client, auth, rec)
    assert resp.status_code == 200
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.headers["x-audio-source"] == "original", "16-bit 原件应直接提供"
    assert resp.headers["x-audio-served-bits"] == "16"
    assert resp.headers["x-audio-original-bits"] == "16"
    assert int(resp.headers["content-length"]) == len(resp.content)


def test_range_returns_206_with_exact_slice(client, session, two_orgs):
    content = make_wav_bytes(seconds=2)
    _, auth, rec = _meeting_with_wav(client, session, two_orgs, "a_admin", content)

    resp = _audio(client, auth, rec, range_header="bytes=0-99")
    assert resp.status_code == 206
    assert len(resp.content) == 100
    assert resp.headers["content-range"] == f"bytes 0-99/{len(content)}"

    tail = _audio(client, auth, rec, range_header="bytes=-16")
    assert tail.status_code == 206 and tail.content == content[-16:]

    open_end = _audio(client, auth, rec, range_header="bytes=40-")
    assert open_end.content == content[40:]


def test_unsatisfiable_range_rejected(client, session, two_orgs):
    content = make_wav_bytes(seconds=1)
    _, auth, rec = _meeting_with_wav(client, session, two_orgs, "a_admin", content)
    resp = _audio(client, auth, rec, range_header=f"bytes={len(content) + 10}-")
    assert resp.status_code == 416
    assert resp.headers["content-range"] == f"bytes */{len(content)}"


def test_24bit_gets_16bit_copy_with_identical_timeline(client, session, two_orgs):
    """浏览器普遍不放 24-bit:必须给降位副本,且时间轴逐帧对得上。"""
    src = _wav(seconds=3, rate=22050, width=3, channels=2)
    _, auth, rec = _meeting_with_wav(client, session, two_orgs, "a_admin", src)
    resp = _audio(client, auth, rec)
    assert resp.status_code == 200
    assert resp.headers["x-audio-source"] == "derived_16bit_pcm"
    assert resp.headers["x-audio-original-bits"] == "24"
    assert resp.headers["x-audio-served-bits"] == "16"

    with wave.open(io.BytesIO(src), "rb") as a, \
            wave.open(io.BytesIO(resp.content), "rb") as b:
        assert b.getnchannels() == a.getnchannels() == 2
        assert b.getframerate() == a.getframerate() == 22050
        assert b.getnframes() == a.getnframes(), "帧数不等则切片起止时间会错位"
        assert b.getsampwidth() == 2 and a.getsampwidth() == 3
        assert round(b.getnframes() * 1000 / b.getframerate()) == \
               round(a.getnframes() * 1000 / a.getframerate()), "副本时长须与原件一致"
    assert int(resp.headers["x-audio-duration-ms"]) == round(a.getnframes() * 1000 / 22050)


def test_audio_files_are_not_served_as_static(client, session, two_orgs):
    """原件目录不得经 `/` 静态挂载公开:按 uuid 文件名直接取应 404。"""
    from pathlib import Path

    from app.core.db import SessionLocal
    from app.models.recording import Recording

    _, auth, rec_id = _meeting_with_wav(client, session, two_orgs, "a_admin",
                                        make_wav_bytes())
    db = SessionLocal()
    try:
        name = Path(db.get(Recording, rec_id).file_path).name
    finally:
        db.close()
    assert client.get(f"/uploads/{name}").status_code == 404
    assert client.get(f"/{name}").status_code == 404
