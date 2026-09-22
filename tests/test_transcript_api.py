"""人工转写导入 API(POST /meetings/{id}/transcript)与 /meta。

覆盖:无自动转写时以人工转写建首版、切片数约束、跨机构拒绝、
以及"导入即可问答"(纪要 + 索引在同一条 Provider 记账路径上完成)。
本文件全部在 mock 组合下运行,真实 Provider 的行为由 E2E 脚本单独验证。
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.db import engine
from app.main import app
from app.worker import stages
from tests.conftest import auth_headers, upload_recording

LINES = [
    "今天讨论心电监护仪的采购与培训安排",
    "设备的注册证我们会在5月20日之前补齐",
    "整机保修期为两年,由厂商负责",
    "第一批设备5月10日到货",
    "培训需要三天,下周三开始",
    "耗材清单5月15日前提供",
    "合同本周内寄出",
    "发票在验收后7天内开具",
    "付款分两期,验收后付尾款",
    "安装场地由院方提前准备",
    "售后响应承诺2天上门",
    "价格含三年维保",
]


def _lines(spk_a="王主任(机构)", spk_b="李经理(厂商)"):
    """行内容对应 12 个 10 秒切片(时间位置由切片边界决定,不由文本推断)。"""
    speakers = [spk_a, spk_b, spk_b, spk_b, spk_b, spk_b, spk_b, spk_b, spk_a,
                spk_a, spk_b, spk_b]
    return [{"speaker": s, "text": t} for s, t in zip(speakers, LINES)]


def _prepared_recording(client, session, org, user):
    """上传录音并只跑 prepare(产出切片),不跑 ASR 阶段。"""
    from tests.conftest import claim_and_load

    meeting, job_id = upload_recording(client, session, org, user, seconds=120)
    epoch, ctx = claim_and_load(job_id)
    stages.run_prepare(engine, ctx, epoch, 10, lambda: False)
    return meeting, ctx.recording_id


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_import_human_transcript_builds_first_version(client, session, two_orgs):
    org, user = two_orgs["org_a"], two_orgs["a_admin"]
    meeting, rid = _prepared_recording(client, session, org, user)
    headers = auth_headers(client, user.username)

    resp = client.post(
        f"/meetings/{meeting.id}/transcript",
        json={"lines": _lines()}, headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["source_type"] == "human_transcription"
    assert body["segments"] == 12 and body["index"]["chunks"] == 12
    assert body["minutes"]["items"] >= 1
    assert body["providers"]["all_mock"] is True

    # 详情接口显示转写来源(界面据此区分人工导入与 mock 自动转写)
    detail = client.get(f"/meetings/{meeting.id}", headers=headers).json()
    assert detail["recordings"][0]["transcript_source"] == "human_transcription"
    assert detail["recordings"][0]["transcript_version_no"] == 1

    # 转写与检索指针都指向人工转写版本
    row = engine.connect().execute(
        text("SELECT active_transcript_version_id, active_search_version_id"
             " FROM recording WHERE id = :r"), {"r": rid}
    ).fetchone()
    assert row[0] == row[1] == body["transcript_version_id"]

    # 问答命中的是人工转写原文,引用带时间位置(时间仍来自切片边界)
    qa = client.post(f"/meetings/{meeting.id}/ask",
                     json={"question": "注册证什么时候补齐?"}, headers=headers).json()
    assert qa["has_sufficient_evidence"] is True
    assert any("5月20日" in c["text"] for c in qa["citations"]), qa["citations"]
    assert qa["citations"][0]["start_ms"] is not None
    assert qa["is_mock"] is True  # 当前组合为 mock,如实标注


def test_line_count_must_match_slices(client, session, two_orgs):
    org, user = two_orgs["org_a"], two_orgs["a_admin"]
    meeting, rid = _prepared_recording(client, session, org, user)
    headers = auth_headers(client, user.username)

    resp = client.post(
        f"/meetings/{meeting.id}/transcript",
        json={"lines": [{"text": "只有一行"}]}, headers=headers,
    )
    assert resp.status_code == 400
    assert "切片数" in resp.json()["detail"], resp.json()
    # 失败不留半成品:没有任何转写版本与索引
    assert engine.connect().execute(
        text("SELECT count(*) FROM transcript_version WHERE recording_id = :r"),
        {"r": rid},
    ).scalar() == 0


def test_second_import_becomes_correction_version(client, session, two_orgs):
    org, user = two_orgs["org_a"], two_orgs["a_admin"]
    meeting, rid = _prepared_recording(client, session, org, user)
    headers = auth_headers(client, user.username)
    first = client.post(
        f"/meetings/{meeting.id}/transcript",
        json={"lines": _lines()}, headers=headers,
    ).json()

    fixed = list(LINES)
    fixed[1] = "设备的注册证我们会补充提供,时间未定"
    second = client.post(
        f"/meetings/{meeting.id}/transcript",
        json={"lines": [{"speaker": d["speaker"], "text": tx}
                        for d, tx in zip(_lines(), fixed)]}, headers=headers,
    )
    assert second.status_code == 201
    body = second.json()
    assert body["source_type"] == "human_correction"
    assert body["edited"] == 1
    assert body["transcript_version_id"] != first["transcript_version_id"]

    # 检索切到新版:旧的"5月20日"不再被引用
    qa = client.post(f"/meetings/{meeting.id}/ask",
                     json={"question": "注册证什么时候补齐?"}, headers=headers).json()
    assert all("5月20日" not in c["text"] for c in qa["citations"]), qa["citations"]


def test_import_requires_access(client, session, two_orgs):
    meeting, rid = _prepared_recording(
        client, session, two_orgs["org_a"], two_orgs["a_admin"]
    )
    outsider = auth_headers(client, two_orgs["b_admin"].username)
    resp = client.post(
        f"/meetings/{meeting.id}/transcript",
        json={"lines": _lines()}, headers=outsider,
    )
    assert resp.status_code == 404  # 无权与不存在不区分
    # 指定他人会谈下的录音也 404(不泄露该 recording 是否存在)
    resp2 = client.post(
        f"/meetings/{meeting.id}/transcript",
        json={"recording_id": rid + 10000, "lines": _lines()},
        headers=auth_headers(client, two_orgs["a_admin"].username),
    )
    assert resp2.status_code == 404


def test_meta_reports_effective_providers(client, two_orgs):
    resp = client.get("/meta", headers=auth_headers(client, two_orgs["a_staff"].username))
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] == {"asr": "mock", "llm": "mock", "embed": "mock"}
    assert body["providers"]["all_mock"] is True
    assert set(body["cost_basis_values"]) == {
        "estimated", "provider_reported", "unknown"
    }


def test_meta_requires_authentication(client):
    assert client.get("/meta").status_code in (401, 403)


def test_meta_fails_loudly_on_unknown_provider(monkeypatch, client, two_orgs):
    """配置了不存在的 Provider → 503,不得伪装成 mock。"""
    from app.core import config as cfg

    bad = cfg.Settings(asr_provider="mock", llm_provider="nonexistent-llm",
                       embed_provider="mock")
    monkeypatch.setattr(cfg, "get_settings", lambda: bad)
    import app.ai.registry as registry_mod

    monkeypatch.setattr(registry_mod, "get_settings", lambda: bad)
    resp = client.get("/meta", headers=auth_headers(client, two_orgs["a_staff"].username))
    assert resp.status_code == 503, resp.text
    assert "不会回退 mock" in resp.json()["detail"]
