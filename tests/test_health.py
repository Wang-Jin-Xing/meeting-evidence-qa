"""/health 冒烟测试。

- liveness:不依赖数据库,进程可响应即 200(集成环境断网/DB 挂掉也必须通过)。
- readiness:依赖 PostgreSQL 容器运行,属集成测试;DB 不可达时返回 503。
"""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_liveness_ok():
    """存活检查:永不触碰外部依赖。"""
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["check"] == "liveness"


def test_readiness_db_up():
    """就绪检查(集成):要求 PG 容器已启动且迁移已应用。"""
    resp = client.get("/health/ready")
    assert resp.status_code == 200, f"数据库就绪检查失败: {resp.text}"
    body = resp.json()
    assert body["database"] == "up"
    assert body["check"] == "readiness"


def test_demo_ui_and_static_assets_served():
    """界面与静态资源必须可访问(演示当天打不开页面即为阻塞)。"""
    page = client.get("/")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert "会谈纪要与证据问答" in page.text
    for asset in ("/static/app.js", "/static/style.css"):
        r = client.get(asset)
        assert r.status_code == 200, asset
        assert len(r.content) > 200, asset
