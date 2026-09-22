"""会谈创建/查询与可见性测试。"""
from app.models.enums import MeetingVisibility
from tests.conftest import auth_headers, make_meeting


def _create_body(**overrides):
    body = {
        "title": "设备采购沟通",
        "vendor_name": "虚构厂商X",
        "topic": "监护仪交付",
        "scheduled_at": "2026-09-10T10:00:00",
    }
    body.update(overrides)
    return body


def test_create_meeting_org_from_backend_identity(client, two_orgs):
    """机构归属来自 JWT 身份,与请求体无关。"""
    headers = auth_headers(client, two_orgs["a_staff"].username)
    resp = client.post("/meetings", json=_create_body(), headers=headers)
    assert resp.status_code == 201
    body = resp.json()
    assert body["org_id"] == two_orgs["org_a"].id
    assert body["created_by"] == two_orgs["a_staff"].id
    assert body["visibility"] == "org"


def test_create_meeting_rejects_frontend_org_id(client, two_orgs):
    """请求体携带 org_id 必须被拒绝(extra=forbid)。"""
    headers = auth_headers(client, two_orgs["a_staff"].username)
    resp = client.post(
        "/meetings", json=_create_body(org_id=two_orgs["org_b"].id), headers=headers
    )
    assert resp.status_code == 422


def test_meeting_list_scoped_to_own_org(client, session, two_orgs):
    """机构 B 用户看不到机构 A 的会谈,反之亦然。"""
    make_meeting(session, two_orgs["org_a"], two_orgs["a_admin"])
    make_meeting(session, two_orgs["org_b"], two_orgs["b_admin"])

    a_headers = auth_headers(client, two_orgs["a_staff"].username)
    b_headers = auth_headers(client, two_orgs["b_staff"].username)
    a_list = client.get("/meetings", headers=a_headers).json()
    b_list = client.get("/meetings", headers=b_headers).json()
    assert a_list and all(m["org_id"] == two_orgs["org_a"].id for m in a_list)
    assert b_list and all(m["org_id"] == two_orgs["org_b"].id for m in b_list)


def test_cross_org_meeting_detail_404(client, session, two_orgs):
    meeting_a = make_meeting(session, two_orgs["org_a"], two_orgs["a_admin"])
    b_headers = auth_headers(client, two_orgs["b_admin"].username)
    resp = client.get(f"/meetings/{meeting_a.id}", headers=b_headers)
    assert resp.status_code == 404


def test_restricted_visibility(client, session, two_orgs):
    """restricted 会谈:白名单内可见,白名单外 staff 不可见,org_admin 可见。"""
    a_admin, a_staff = two_orgs["a_admin"], two_orgs["a_staff"]
    other_staff = two_orgs["a_staff"]  # 占位,下方另建真实"非白名单"用户
    from tests.conftest import make_user
    non_whitelist = make_user(session, two_orgs["org_a"])

    meeting = make_meeting(
        session, two_orgs["org_a"], a_admin,
        visibility=MeetingVisibility.restricted,
        access_user_ids=[a_staff.id],
    )

    # 白名单内 staff:可见
    staff_headers = auth_headers(client, a_staff.username)
    assert client.get(f"/meetings/{meeting.id}", headers=staff_headers).status_code == 200
    listed = client.get("/meetings", headers=staff_headers).json()
    assert any(m["id"] == meeting.id for m in listed)

    # 白名单外 staff:列表与详情均不可见(404,不区分无权与不存在)
    nw_headers = auth_headers(client, non_whitelist.username)
    assert client.get(f"/meetings/{meeting.id}", headers=nw_headers).status_code == 404
    listed = client.get("/meetings", headers=nw_headers).json()
    assert not any(m["id"] == meeting.id for m in listed)

    # 本机构 org_admin:无需白名单即可见
    admin_headers = auth_headers(client, a_admin.username)
    assert client.get(f"/meetings/{meeting.id}", headers=admin_headers).status_code == 200


def test_create_restricted_whitelist_must_be_same_org(client, session, two_orgs):
    """白名单不允许包含其他机构用户(防跨机构授权)。"""
    headers = auth_headers(client, two_orgs["a_admin"].username)
    resp = client.post(
        "/meetings",
        json=_create_body(
            visibility="restricted",
            access_user_ids=[two_orgs["b_staff"].id],
        ),
        headers=headers,
    )
    assert resp.status_code == 422


def test_meeting_detail_includes_recording_job_fields(client, session, two_orgs):
    """详情接口在已有录音时仍须 200,并带派生的 job_* 字段。

    回归:此前 MeetingDetail.model_validate(meeting) 会把 ORM Recording
    当 RecordingBrief 校验,而 job_id/job_status 只存在于 ProcessingJob 上,
    任何带录音的会谈详情都 500(测试界面点开会谈即触发)。
    """
    from tests.conftest import make_meeting, make_wav_bytes

    org, user = two_orgs["org_a"], two_orgs["a_admin"]
    meeting = make_meeting(session, org, user)
    headers = auth_headers(client, user.username)
    up = client.post(
        f"/meetings/{meeting.id}/recordings",
        files={"file": ("a.wav", make_wav_bytes(seconds=12), "audio/wav")},
        headers=headers,
    )
    assert up.status_code == 202

    body = client.get(f"/meetings/{meeting.id}", headers=headers)
    assert body.status_code == 200, body.text
    recs = body.json()["recordings"]
    assert len(recs) == 1
    assert recs[0]["job_id"] == up.json()["job_id"]
    assert recs[0]["job_status"] == "queued"
    assert set(recs[0]) >= {"id", "original_filename", "checksum",
                            "job_id", "job_status", "job_current_stage"}
