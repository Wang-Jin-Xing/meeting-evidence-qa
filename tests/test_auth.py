"""认证与身份解析测试。"""
from tests.conftest import make_org, make_user


def test_login_success(client, session):
    org = make_org(session)
    user = make_user(session, org, username="login-ok")
    resp = client.post(
        "/auth/login", json={"username": "login-ok", "password": "pass1234"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]


def test_login_wrong_password(client, session):
    org = make_org(session)
    make_user(session, org, username="login-bad")
    resp = client.post(
        "/auth/login", json={"username": "login-bad", "password": "wrong"}
    )
    assert resp.status_code == 401


def test_login_unknown_user(client):
    resp = client.post(
        "/auth/login", json={"username": "ghost", "password": "x"}
    )
    assert resp.status_code == 401


def test_me_requires_token(client):
    assert client.get("/auth/me").status_code == 401


def test_me_invalid_token(client):
    resp = client.get("/auth/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert resp.status_code == 401


def test_me_returns_backend_identity(client, session, two_orgs):
    headers = {}
    resp = client.post(
        "/auth/login",
        json={"username": two_orgs["a_staff"].username, "password": "pass1234"},
    )
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    me = client.get("/auth/me", headers=headers).json()
    assert me["org_id"] == two_orgs["org_a"].id
    assert me["role"] == "staff"
    assert me["id"] == two_orgs["a_staff"].id
