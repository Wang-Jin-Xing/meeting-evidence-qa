"""P1.3 检索问答与纪要查看 API 测试。

数据准备复用 P1.2 模式:真实子进程 worker 完成四阶段,
问答针对已发布的 active 索引(is_active=true 的 chunk)。
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.db import SessionLocal, engine
from app.main import app
from app.models.enums import MeetingVisibility
from tests.conftest import (
    auth_headers,
    make_meeting,
    make_user,
    make_wav_bytes,
)
from tests.test_worker_e2e import start_worker, wait_job_status


@pytest.fixture(scope="module")
def qa_world():
    """三份相同内容的 25s 录音(3 片段=语料前 3 句),子进程 worker 完成全部四阶段:
    - 机构 A 会谈一(问答主对象);
    - 机构 A 会谈二(同机构、同创建者可见 → 验证候选显式限定会谈);
    - 机构 B 会谈(跨机构 → 验证权限注入候选)。
    """
    from tests.conftest import make_org, _uniq

    session = SessionLocal()
    try:
        org_a = make_org(session, _uniq("机构A"))
        org_b = make_org(session, _uniq("机构B"))
        a_admin = make_user(session, org_a)
        a_staff = make_user(session, org_a)
        b_admin = make_user(session, org_b)

        wav = make_wav_bytes(seconds=25)
        job_ids = []
        with TestClient(app) as client:
            uploads = [
                (make_meeting(session, org_a, a_admin), a_admin),   # 机构 A 会谈一
                (make_meeting(session, org_a, a_admin), a_admin),   # 机构 A 会谈二
                (make_meeting(session, org_b, b_admin), b_admin),   # 机构 B 会谈
            ]
            for meeting, uploader in uploads:
                resp = client.post(
                    f"/meetings/{meeting.id}/recordings",
                    files={"file": (f"qa-{meeting.id}.wav", wav, "audio/wav")},
                    headers=auth_headers(client, uploader.username),
                )
                assert resp.status_code == 202
                job_ids.append(resp.json()["job_id"])

            proc = start_worker()
            try:
                for jid in job_ids:
                    assert wait_job_status(jid, {"succeeded", "failed"}) == "succeeded"
            finally:
                proc.terminate()

        meeting_a, meeting_a2, meeting_b = (m for m, _ in uploads)
    finally:
        session.close()

    return {
        "meeting_a_id": meeting_a.id,
        "meeting_a2_id": meeting_a2.id,
        "meeting_b_id": meeting_b.id,
        "org_a_id": org_a.id,
        "a_admin_id": a_admin.id,
        "a_admin": a_admin.username,
        "a_staff": a_staff.username,
        "b_admin": b_admin.username,
    }


def _segment_meeting_map(segment_ids: list[int]) -> dict[int, int]:
    """片段 id → 所属会谈 id(经 version → recording → meeting)。"""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT ts.id, r.meeting_id FROM transcript_segment ts"
                " JOIN transcript_version tv ON tv.id = ts.version_id"
                " JOIN recording r ON r.id = tv.recording_id"
                " WHERE ts.id = ANY(:ids)"
            ),
            {"ids": list(segment_ids)},
        ).fetchall()
    return {r[0]: r[1] for r in rows}


def _meeting_segment_ids(meeting_id: int) -> set[int]:
    with engine.connect() as conn:
        return set(
            conn.execute(
                text(
                    "SELECT ts.id FROM transcript_segment ts"
                    " JOIN transcript_version tv ON tv.id = ts.version_id"
                    " JOIN recording r ON r.id = tv.recording_id"
                    " WHERE r.meeting_id = :m"
                ),
                {"m": meeting_id},
            ).scalars().all()
        )


# ---------- 问答:正向链路 ----------

def test_ask_with_evidence_and_resolvable_citations(client, qa_world):
    """相关问题:有回答、引用可解析到片段原文/说话人/时间位置、mock 标识。"""
    resp = client.post(
        f"/meetings/{qa_world['meeting_a_id']}/ask",
        json={"question": "X200的注册证什么时候补齐?"},
        headers=auth_headers(client, qa_world["a_admin"]),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_sufficient_evidence"] is True
    assert body["is_mock"] is True
    assert body["answer"]

    # 引用可解析:segment 真实存在且属于本会谈,原文/时间位置齐备
    seg_map = _segment_meeting_map([c["segment_id"] for c in body["citations"]])
    assert body["citations"]
    for c in body["citations"]:
        assert seg_map[c["segment_id"]] == qa_world["meeting_a_id"]
        assert c["text"] and c["speaker"]
        assert c["start_ms"] is not None and c["end_ms"] is not None
        assert c["similarity"] >= 0.15

    # 高相似候选命中"注册证"片段(语料第2句,分片1,边界 10s-20s)
    reg = [c for c in body["citations"] if "注册证" in c["text"]]
    assert reg, "应召回含注册证的片段"
    assert (reg[0]["start_ms"], reg[0]["end_ms"]) == (10000, 20000)

    # mock 抽取式:回答文本由候选原文构成,不编造
    for c in body["citations"]:
        assert c["text"] in body["answer"]


def test_ask_insufficient_evidence_explicit(client, qa_world):
    """无关问题:明确返回证据不足——无回答、无引用,而非编造。"""
    resp = client.post(
        f"/meetings/{qa_world['meeting_a_id']}/ask",
        json={"question": "今天天气怎么样适合户外运动吗"},
        headers=auth_headers(client, qa_world["a_admin"]),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_sufficient_evidence"] is False
    assert body["answer"] is None
    assert body["citations"] == []
    assert body["is_mock"] is True
    assert body["retrieved_count"] == 0, "无关问题不应有过阈值的候选"


def test_ask_permission_injected_in_candidates(client, qa_world):
    """双机构相同内容索引(向量完全相同):候选只含本机构片段。"""
    b_segs = _meeting_segment_ids(qa_world["meeting_b_id"])
    assert b_segs, "前置:B 机构应存在同内容片段"

    resp = client.post(
        f"/meetings/{qa_world['meeting_a_id']}/ask",
        json={"question": "X200的注册证什么时候补齐?"},
        headers=auth_headers(client, qa_world["a_admin"]),
    )
    assert resp.status_code == 200
    cited = {c["segment_id"] for c in resp.json()["citations"]}
    assert cited, "应有引用"
    assert cited & b_segs == set(), "B 机构片段不得进入 A 用户候选/引用"
    assert cited <= _meeting_segment_ids(qa_world["meeting_a_id"])


def test_ask_same_org_other_visible_meeting_excluded(client, qa_world):
    """同机构、同用户可见的另一会谈:其片段不得进入候选或引用。

    候选 SQL 显式限定 meeting_id = URL 会谈,"属于可见集合"不能替代该条件。
    """
    a2_segs = _meeting_segment_ids(qa_world["meeting_a2_id"])
    assert a2_segs, "前置:同机构另一会谈应存在相同内容片段"

    resp = client.post(
        f"/meetings/{qa_world['meeting_a_id']}/ask",
        json={"question": "X200的注册证什么时候补齐?"},
        headers=auth_headers(client, qa_world["a_admin"]),
    )
    assert resp.status_code == 200
    cited = {c["segment_id"] for c in resp.json()["citations"]}
    assert cited, "应有引用"
    assert cited & a2_segs == set(), "同机构其他可见会谈的片段不得进入候选/引用"
    assert cited <= _meeting_segment_ids(qa_world["meeting_a_id"])


def test_ask_unpublished_version_misflagged_active_not_retrieved(
    client, session, qa_world
):
    """未发布版本即使误标 is_active=true 也不可检索:
    检索同时匹配 recording.active_search_version_id 指针,指针为权威。"""
    from sqlalchemy import select

    from app.ai.mock_providers import MockEmbed
    from app.models.recording import Recording
    from app.models.transcript import (
        EmbeddingChunk,
        TranscriptSegment,
        TranscriptVersion,
    )

    rec_id = session.scalar(
        select(Recording.id).where(Recording.meeting_id == qa_world["meeting_a_id"])
    )
    marker = "独特标记段落内容甲乙丙丁戊己庚辛"
    v2 = TranscriptVersion(recording_id=rec_id, version_no=2, source_type="re_asr")
    session.add(v2)
    session.flush()
    seg = TranscriptSegment(
        version_id=v2.id, seq=1, start_ms=0, end_ms=1000, speaker="测试", text=marker
    )
    session.add(seg)
    session.flush()
    # 误标:is_active=true 但版本未发布(active_search_version 指针未切换)
    session.add(
        EmbeddingChunk(
            segment_id=seg.id, version_id=v2.id, org_id=qa_world["org_a_id"],
            meeting_id=qa_world["meeting_a_id"], is_active=True,
            embedding=MockEmbed._embed_one(marker), text=marker,
        )
    )
    session.commit()
    try:
        # 问题与误标片段向量完全一致(相似度=1,本应排第一),
        # 与已发布的三片段完全不相关
        resp = client.post(
            f"/meetings/{qa_world['meeting_a_id']}/ask",
            json={"question": marker},
            headers=auth_headers(client, qa_world["a_admin"]),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["retrieved_count"] == 0, "误标 active 的未发布版本不得进入候选"
        assert body["has_sufficient_evidence"] is False
        assert body["answer"] is None
        assert body["citations"] == []
    finally:
        # 清理误标数据,不影响同模块其他测试
        session.execute(
            text("DELETE FROM embedding_chunk WHERE segment_id = :s"), {"s": seg.id}
        )
        session.execute(
            text("DELETE FROM transcript_segment WHERE id = :s"), {"s": seg.id}
        )
        session.execute(
            text("DELETE FROM transcript_version WHERE id = :v"), {"v": v2.id}
        )
        session.commit()


@pytest.mark.parametrize(
    "question",
    [
        "注册证由谁负责补齐?",           # 相关(片1)但负责人线索缺失
        "监护仪的采购交付什么时候确认?",  # 相关(片0)但日期缺失
        "X300的保修期是多久?",           # 相关(片2 为原问句转写)但时长数字缺失
    ],
)
def test_ask_related_but_missing_focus_clue_insufficient(client, qa_world, question):
    """候选相关(过相关性阈值)但问题焦点线索缺失:证据不足。

    区别于完全不相关(retrieved_count=0):此处候选非空,但询问的
    日期/负责人/时长未出现,不得拼接候选冒充答案。
    """
    resp = client.post(
        f"/meetings/{qa_world['meeting_a_id']}/ask",
        json={"question": question},
        headers=auth_headers(client, qa_world["a_admin"]),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["retrieved_count"] > 0, "前置:问题与候选相关,非完全不相关"
    assert body["has_sufficient_evidence"] is False
    assert body["answer"] is None
    assert body["citations"] == []


def test_query_log_written(client, qa_world):
    """问答审计:候选/回答/引用/充分性/mock 标识全部落库(充分路径)。"""
    question = "注册证和检测报告什么时候补齐?"  # 片1 含日期线索 → 充分
    resp = client.post(
        f"/meetings/{qa_world['meeting_a_id']}/ask",
        json={"question": question},
        headers=auth_headers(client, qa_world["a_admin"]),
    )
    assert resp.status_code == 200
    body = resp.json()

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT user_id, retrieved_chunk_ids, answer, cited_segment_ids,"
                " has_sufficient_evidence, is_mock"
                " FROM query_log WHERE question = :q ORDER BY id DESC LIMIT 1"
            ),
            {"q": question},
        ).fetchone()
    assert row is not None
    assert row[0] == qa_world["a_admin_id"]
    assert row[1]  # 检索候选已记录
    assert row[2] == body["answer"]
    assert row[3] == [c["segment_id"] for c in body["citations"]]
    assert row[4] is True
    assert row[5] is True


# ---------- 问答:权限与边界 ----------

def test_ask_cross_org_404(client, qa_world):
    resp = client.post(
        f"/meetings/{qa_world['meeting_a_id']}/ask",
        json={"question": "X200的保修期多久"},
        headers=auth_headers(client, qa_world["b_admin"]),
    )
    assert resp.status_code == 404


def test_ask_restricted_not_whitelisted_404(client, session, two_orgs):
    meeting = make_meeting(
        session, two_orgs["org_a"], two_orgs["a_admin"],
        visibility=MeetingVisibility.restricted, access_user_ids=[],
    )
    resp = client.post(
        f"/meetings/{meeting.id}/ask",
        json={"question": "内容"},
        headers=auth_headers(client, two_orgs["a_staff"].username),
    )
    assert resp.status_code == 404


def test_ask_meeting_without_index_returns_insufficient(client, session, two_orgs):
    """会谈可见但尚无可检索索引(未上传/未建索引):明确证据不足,而非 5xx。"""
    meeting = make_meeting(session, two_orgs["org_a"], two_orgs["a_staff"])
    resp = client.post(
        f"/meetings/{meeting.id}/ask",
        json={"question": "X200的注册证什么时候补齐?"},
        headers=auth_headers(client, two_orgs["a_staff"].username),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_sufficient_evidence"] is False
    assert body["retrieved_count"] == 0
    assert body["citations"] == []


def test_ask_input_validation(client, qa_world):
    headers = auth_headers(client, qa_world["a_admin"])
    url = f"/meetings/{qa_world['meeting_a_id']}/ask"
    # 空问题
    assert client.post(url, json={"question": ""}, headers=headers).status_code == 422
    # 多余字段(extra=forbid,不接受客户端自定 top_k 等参数)
    r = client.post(url, json={"question": "问题", "top_k": 99}, headers=headers)
    assert r.status_code == 422
    # 未认证
    assert client.post(url, json={"question": "问题"}).status_code == 401


# ---------- 纪要查看 ----------

def test_get_minutes_with_resolved_evidence(client, qa_world):
    """纪要内容完整,条目证据解析为片段原文与时间位置。"""
    resp = client.get(
        f"/meetings/{qa_world['meeting_a_id']}/minutes",
        headers=auth_headers(client, qa_world["a_admin"]),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["version_no"] == 1
    assert body["source_type"] == "ai_draft"
    assert body["status"] == "pending_review"
    assert [v["version_no"] for v in body["versions"]] == [1]

    items = body["content"]["items"]
    assert items
    # 语料第2句 → todo:注册证 5月20日前补齐,负责人为厂商
    target = [
        i for i in items if i["type"] == "todo" and "补齐" in i["text"]
    ]
    assert target
    t = target[0]
    assert t["due_date"] == "5月20日"
    assert "李经理" in (t["owner"] or "")
    assert t["evidence_segment_ids"]

    # 证据解析:内嵌片段与 evidence_segment_ids 一一对应
    assert [e["segment_id"] for e in t["evidence"]] == t["evidence_segment_ids"]
    ev = t["evidence"][0]
    assert "注册证" in ev["text"]
    assert ev["speaker"] == "李经理(厂商)"
    assert (ev["start_ms"], ev["end_ms"]) == (10000, 20000)


def test_get_minutes_404_when_not_generated(client, session, two_orgs):
    meeting = make_meeting(session, two_orgs["org_a"], two_orgs["a_admin"])
    resp = client.get(
        f"/meetings/{meeting.id}/minutes",
        headers=auth_headers(client, two_orgs["a_admin"].username),
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "纪要尚未生成"


def test_get_minutes_cross_org_404(client, qa_world):
    resp = client.get(
        f"/meetings/{qa_world['meeting_a_id']}/minutes",
        headers=auth_headers(client, qa_world["b_admin"]),
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "会谈不存在或无权访问"


def test_get_minutes_restricted_whitelisted_passes_access(client, session, two_orgs):
    """restricted 白名单用户通过访问检查(404 原因是未生成,而非无权)。"""
    meeting = make_meeting(
        session, two_orgs["org_a"], two_orgs["a_admin"],
        visibility=MeetingVisibility.restricted,
        access_user_ids=[two_orgs["a_staff"].id],
    )
    resp = client.get(
        f"/meetings/{meeting.id}/minutes",
        headers=auth_headers(client, two_orgs["a_staff"].username),
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "纪要尚未生成"
