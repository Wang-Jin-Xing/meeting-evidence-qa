"""纪要人工修订与按版本确认(D-62)。

覆盖三条不变量:AI 原版不改写、确认属于具体版本且编辑后需重新确认、
并发编辑不静默覆盖;另加引用校验与越权。
"""
import pytest

from app.core.db import SessionLocal
from app.models.enums import (
    MeetingVisibility, MinutesSourceType, MinutesStatus, TranscriptSourceType,
)
from app.models.minutes import MinutesVersion
from app.models.org import MeetingAccess
from app.models.recording import Recording
from app.models.transcript import TranscriptSegment, TranscriptVersion
from tests.conftest import auth_headers, make_meeting, make_user


def _item(seg_ids, text="X200 注册证 5月20日前补齐", owner="李经理", due="5月20日",
          needs=False, typ="todo"):
    return {"type": typ, "text": text, "certainty": "decided", "owner": owner,
            "due_date": due, "evidence_segment_ids": seg_ids,
            "needs_confirmation": needs}


def _world(session, two_orgs, *, visibility=MeetingVisibility.org, access_ids=()):
    """会谈 + 一段 3 片段的转写 + AI 纪要 v1(pending_review)。"""
    admin = two_orgs["a_admin"]
    meeting = make_meeting(session, two_orgs["org_a"], admin, visibility=visibility,
                           access_user_ids=access_ids)
    rec = Recording(meeting_id=meeting.id, file_path="tests://none.wav",
                    original_filename="none.wav", checksum="c" + str(meeting.id),
                    size_bytes=1, created_by=admin.id)
    session.add(rec)
    session.flush()
    ver = TranscriptVersion(recording_id=rec.id, version_no=1,
                            source_type=TranscriptSourceType.human_transcription)
    session.add(ver)
    session.flush()
    segs = []
    for i in range(3):
        s = TranscriptSegment(version_id=ver.id, seq=i + 1, start_ms=i * 10000,
                              end_ms=(i + 1) * 10000, speaker="李经理(厂商)",
                              text=f"片段{i + 1}",
                              timestamp_precision="segment_level_from_slice")
        session.add(s)
        segs.append(s)
    session.flush()   # 片段 id 要先生成,否则纪要里存进去的是 None
    v1 = MinutesVersion(meeting_id=meeting.id, version_no=1,
                        source_type=MinutesSourceType.ai_draft,
                        status=MinutesStatus.pending_review,
                        content={"items": [_item([segs[1].id])]},
                        created_by=admin.id)
    session.add(v1)
    session.commit()
    return {"meeting": meeting, "recording": rec, "segments": segs, "v1": v1,
            "admin": admin}


@pytest.fixture
def world(session, two_orgs):
    return _world(session, two_orgs)


def _edit(client, auth, mid, base_id, items):
    return client.post(f"/meetings/{mid}/minutes/versions",
                       json={"base_version_id": base_id, "content": {"items": items}},
                       headers=auth)


def test_edit_appends_version_and_keeps_ai_draft(client, world, session):
    auth = auth_headers(client, world["admin"].username)
    mid = world["meeting"].id
    seg_id = world["segments"][0].id

    resp = _edit(client, auth, mid, world["v1"].id,
                 [_item([seg_id], text="改成:5月25日前补齐")])
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["version_no"] == 2 and body["source_type"] == "human_edit"
    assert body["status"] == "pending_review"
    assert body["created_by"] == world["admin"].id

    v1 = _reload(world["v1"].id)
    assert v1.source_type is MinutesSourceType.ai_draft
    assert v1.content["items"][0]["text"] == "X200 注册证 5月20日前补齐", "AI 原版必须原样保留"
    assert v1.content["items"][0]["evidence_segment_ids"] == [world["segments"][1].id]


def _reload(version_id):
    db = SessionLocal()
    try:
        return db.get(MinutesVersion, version_id)
    finally:
        db.close()


def test_confirm_is_per_version_and_edit_requires_reconfirm(client, world):
    auth = auth_headers(client, world["admin"].username)
    mid = world["meeting"].id
    seg_id = world["segments"][0].id

    v2 = _edit(client, auth, mid, world["v1"].id, [_item([seg_id])]).json()
    ok = client.post(f"/meetings/{mid}/minutes/confirm",
                     json={"version_id": v2["id"]}, headers=auth)
    assert ok.status_code == 200
    confirmed = ok.json()
    assert confirmed["status"] == "confirmed"
    assert confirmed["confirmed_by"] == world["admin"].id
    assert confirmed["confirmed_at"]

    # 确认只落在被确认的那一版:AI 草稿不能被"顺带"标成已确认
    detail = client.get(f"/meetings/{mid}/minutes", headers=auth).json()
    by_no = {v["version_no"]: v for v in detail["versions"]}
    assert by_no[1]["status"] == "pending_review"
    assert by_no[1]["confirmed_by"] is None
    assert by_no[2]["status"] == "confirmed"

    # 再编辑 → 新版本必须重新待确认
    v3 = _edit(client, auth, mid, v2["id"], [_item([seg_id], text="又改了一次")])
    assert v3.status_code == 201
    assert v3.json()["status"] == "pending_review"
    assert v3.json()["confirmed_by"] is None


def test_confirm_same_version_twice_is_idempotent(client, world):
    auth = auth_headers(client, world["admin"].username)
    mid, vid = world["meeting"].id, world["v1"].id
    first = client.post(f"/meetings/{mid}/minutes/confirm",
                        json={"version_id": vid}, headers=auth).json()
    second = client.post(f"/meetings/{mid}/minutes/confirm",
                         json={"version_id": vid}, headers=auth).json()
    assert first["confirmed_at"] == second["confirmed_at"], "重复确认不得刷新时间或换确认人"


def test_stale_base_version_rejected_not_silently_overwritten(client, world):
    auth = auth_headers(client, world["admin"].username)
    mid, seg_id = world["meeting"].id, world["segments"][0].id
    v2 = _edit(client, auth, mid, world["v1"].id, [_item([seg_id])]).json()

    stale = _edit(client, auth, mid, world["v1"].id,
                  [_item([seg_id], text="基于旧版想覆盖别人的修改")])
    assert stale.status_code == 409
    detail = stale.json()["detail"]
    assert detail["latest_version_no"] == v2["version_no"] == 2
    assert "最新版本" in detail["message"]

    # 状态没被写坏:仍然是 v1+v2 两版
    versions = client.get(f"/meetings/{mid}/minutes", headers=auth).json()["versions"]
    assert [v["version_no"] for v in versions] == [1, 2]


def test_human_edit_still_validated(client, world, session, two_orgs):
    auth = auth_headers(client, world["admin"].username)
    mid, own_seg = world["meeting"].id, world["segments"][1].id

    # 引用不存在的片段 → 拒绝
    bad = _edit(client, auth, mid, world["v1"].id, [_item([999999])])
    assert bad.status_code == 422 and "片段" in bad.json()["detail"]

    # 引用**别的会谈**的片段 → 同样拒绝(否则能把别人的原文当成自己的证据)
    other = _world(session, two_orgs)
    cross = _edit(client, auth, mid, world["v1"].id,
                  [_item([other["segments"][0].id])])
    assert cross.status_code == 422, cross.text

    # decision/todo 缺负责人又不标待确认 → 拒绝(人工编辑不豁免空值策略)
    lazy = _edit(client, auth, mid, world["v1"].id,
                 [_item([own_seg], owner=None, due=None, needs=False)])
    assert lazy.status_code == 422 and "待确认" in lazy.json()["detail"]

    # 同上但如实标注待确认 → 通过
    honest = _edit(client, auth, mid, world["v1"].id,
                   [_item([own_seg], owner=None, due=None, needs=True)])
    assert honest.status_code == 201

    # 失败的那几次没有留下版本
    versions = client.get(f"/meetings/{mid}/minutes", headers=auth).json()["versions"]
    assert [v["version_no"] for v in versions] == [1, 2]


def test_cross_org_and_restricted_cannot_edit_or_confirm(client, session, two_orgs):
    w = _world(session, two_orgs)
    b_auth = auth_headers(client, two_orgs["b_staff"].username)
    mid, vid = w["meeting"].id, w["v1"].id
    seg_id = w["segments"][0].id

    assert _edit(client, b_auth, mid, vid, [_item([seg_id])]).status_code == 404
    assert client.post(f"/meetings/{mid}/minutes/confirm",
                       json={"version_id": vid}, headers=b_auth).status_code == 404
    assert client.get(f"/meetings/{mid}/minutes", headers=b_auth).status_code == 404

    # restricted:同机构未授权也不行,白名单内才可以
    w2 = _world(session, two_orgs, visibility=MeetingVisibility.restricted)
    outsider = auth_headers(client, two_orgs["a_staff"].username)
    assert client.get(f"/meetings/{w2['meeting'].id}/minutes",
                      headers=outsider).status_code == 404
    grant = make_user(session, two_orgs["org_a"])
    session.add(MeetingAccess(user_id=grant.id, meeting_id=w2["meeting"].id))
    session.commit()
    g_auth = auth_headers(client, grant.username)
    assert client.get(f"/meetings/{w2['meeting'].id}/minutes",
                      headers=g_auth).status_code == 200
    ok = _edit(client, g_auth, w2["meeting"].id, w2["v1"].id,
               [_item([w2["segments"][0].id])])
    assert ok.status_code == 201, ok.text
