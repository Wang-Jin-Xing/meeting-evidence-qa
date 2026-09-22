"""P2 任务恢复与故障验证:真实 PostgreSQL + worker 子进程注入故障。

覆盖六类场景(均为真实子进程,故障注入后由新 worker 回收接管):
1. 分片部分成功后杀进程 → 接管者只处理未完成片段;
2. 模型已响应、结果未提交时杀进程 → model_run 记录 unknown,
   重发调用如实留痕,不宣称避免重复调用或计费;
3. 阶段产物与阶段状态已提交后杀进程 → 恢复复用产物,不重复生成;
4. 旧 worker 租约失效后被接管 → 旧 epoch 续租/提交均被拒绝;
5. 索引部分完成时中断 → 旧发布版本仍可检索,新版本完成后原子切换;
6. 两个 worker 竞争回收 → 只有一个有效接管者。

约定:
- 每个测试同时核对数据库状态、产物数量、mock 调用记录(model_run);
- 区分"结果没有重复"(每 input_hash 恰好一条 succeeded,产物唯一)与
  "外部调用没有重复"(物理调用计数,含 unknown/failed);
- "旧 worker 恢复后写入"以进程内模拟(等价网络分区恢复;被杀进程无法
  复活,边界如实声明)。
"""
import time
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.db import engine
from tests.conftest import auth_headers, make_meeting, make_wav_bytes
from tests.test_worker_e2e import q, start_worker, wait_job_status

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------- 公共辅助 ----------

@pytest.fixture(autouse=True)
def _no_stale_pending_jobs():
    """测试开始前把库中遗留的 queued/running 任务置为终态 failed。

    防护:恢复类测试失败后可能留下可领取的任务,被后续测试的 worker
    抢走会导致故障注入错位(claim 按 created_at 取最老)。succeeded/failed
    不在领取候选内,qa_world 等已完成数据不受影响。
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE processing_job SET status = 'failed',"
                " last_error = '测试间清理:残留任务置终态'"
                " WHERE status IN ('queued', 'running')"
            )
        )
    yield


def wait_for(cond, timeout: float = 30, msg: str = "条件等待超时"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(0.05)
    raise AssertionError(msg)


def kill_worker(proc):
    """强杀子进程(TerminateProcess),并等待收尸。"""
    proc.kill()
    proc.wait(timeout=10)


def upload(client, session, org, user, seconds: float = 25):
    """上传 WAV 并返回 (meeting, job_id)。25s → 3 片(语料前 3 句)。"""
    meeting = make_meeting(session, org, user)
    resp = client.post(
        f"/meetings/{meeting.id}/recordings",
        files={"file": (f"rec-{meeting.id}.wav", make_wav_bytes(seconds=seconds), "audio/wav")},
        headers=auth_headers(client, user.username),
    )
    assert resp.status_code == 202
    return meeting, resp.json()["job_id"]


def transcribe_run_stats(job_id: int) -> dict[str, int]:
    """transcribe 阶段 model_run 按 status 计数(物理调用事实)。"""
    rows = q(
        "SELECT status, count(*) FROM model_run"
        " WHERE job_id = :j AND stage = 'transcribe' GROUP BY status",
        j=job_id,
    )
    return {r[0]: r[1] for r in rows}


def assert_no_duplicate_success(job_id: int, stage: str = "transcribe"):
    """结果没有重复:每个 input_hash 恰好一条 succeeded。"""
    dups = q(
        "SELECT input_hash FROM model_run"
        " WHERE job_id = :j AND stage = :s AND status = 'succeeded'"
        " GROUP BY input_hash HAVING count(*) > 1",
        j=job_id, s=stage,
    )
    assert dups == [], f"{stage} 存在重复成功结果: {dups}"


def recording_of_job(job_id: int) -> int:
    return q(
        "SELECT recording_id FROM processing_job WHERE id = :j", j=job_id
    )[0][0]


# ---------- 场景 1:分片部分成功后接管 ----------

def test_recovery_partial_slices_only_missing_processed(client, session, two_orgs):
    """分片 0 提交后杀进程:接管者只转写未完成片段,成功结果不重复。"""
    _, job_id = upload(client, session, two_orgs["org_a"], two_orgs["a_admin"])

    # 慢速 worker:每片调用延迟 400ms,给轮询留捕获窗口
    p = start_worker({
        "MEET_MOCK_ASR_DELAY_MS": "400",
        "MEET_WORKER_LEASE_TTL_SECONDS": "2",
    })
    try:
        wait_for(
            lambda: q(
                "SELECT count(*) FROM transcribe_slice"
                " WHERE job_id = :j AND status = 'succeeded'",
                j=job_id,
            )[0][0] >= 1,
            msg="等待片0成功超时",
        )
    finally:
        kill_worker(p)

    done_before = q(
        "SELECT count(*) FROM transcribe_slice"
        " WHERE job_id = :j AND status = 'succeeded'",
        j=job_id,
    )[0][0]
    assert 1 <= done_before < 3, "前置:应处于部分成功状态"

    # 接管者完成剩余片段
    p2 = start_worker()
    try:
        assert wait_job_status(job_id, {"succeeded", "failed"}) == "succeeded"
    finally:
        p2.terminate()

    # 数据库状态与产物数量
    rid = recording_of_job(job_id)
    assert q("SELECT count(*) FROM transcript_version WHERE recording_id = :r", r=rid)[0][0] == 1
    assert q("SELECT count(*) FROM transcript_segment ts JOIN transcript_version tv ON tv.id = ts.version_id WHERE tv.recording_id = :r", r=rid)[0][0] == 3
    assert q(
        "SELECT count(*), count(*) FILTER (WHERE is_active) FROM embedding_chunk"
        " JOIN transcript_version tv ON tv.id = embedding_chunk.version_id"
        " WHERE tv.recording_id = :r",
        r=rid,
    )[0] == (3, 3)

    # mock 调用记录:每片恰好一次成功(外部调用无重复);
    # kill 时刻挂起的调用允许留 unknown(结果未知,重发必要)
    stats = transcribe_run_stats(job_id)
    assert stats.get("succeeded") == 3, stats
    assert stats.get("unknown", 0) <= 1, stats
    assert stats.get("failed", 0) == 0, stats
    assert_no_duplicate_success(job_id)


# ---------- 场景 2:模型已响应、结果未提交 ----------

def test_model_responded_but_uncommitted_records_unknown(client, session, two_orgs):
    """模型返回后、结果提交前杀进程:调用记为 unknown,重发后成功。

    不宣称避免重复调用或计费——unknown 行 + 重发行各自留痕,
    外部调用物理上重复(同 input_hash 两条记录),如实审计。
    """
    _, job_id = upload(client, session, two_orgs["org_a"], two_orgs["a_admin"])

    # 第 1 次任意 Provider 调用(= 片0 ASR)返回后立即自杀
    p = start_worker({
        "MEET_MOCK_KILL_AFTER_CALL": "1",
        "MEET_WORKER_LEASE_TTL_SECONDS": "2",
    })
    try:
        wait_for(lambda: p.poll() is not None, msg="等待 worker 自杀超时")
    finally:
        if p.poll() is None:
            kill_worker(p)
    assert p.returncode == 70, f"应死于注入自杀(exit 70),实际 {p.returncode}"

    # 死亡现场:片0 调用已发出(begin 前置记录)但结果未提交
    assert transcribe_run_stats(job_id) == {"unknown": 1}
    assert q(
        "SELECT count(*) FROM transcribe_slice"
        " WHERE job_id = :j AND status = 'succeeded'",
        j=job_id,
    )[0][0] == 0

    # 接管者重发(unknown 非 succeeded,缓存未命中),最终完成
    p2 = start_worker()
    try:
        assert wait_job_status(job_id, {"succeeded", "failed"}) == "succeeded"
    finally:
        p2.terminate()

    # 产物:一套版本,3 片段,文本正确(mock 确定性)
    rid = recording_of_job(job_id)
    segs = q(
        "SELECT ts.seq, ts.text FROM transcript_segment ts"
        " JOIN transcript_version tv ON tv.id = ts.version_id"
        " WHERE tv.recording_id = :r ORDER BY ts.seq",
        r=rid,
    )
    assert len(segs) == 3
    assert "X200" in segs[0][1] and "注册证" in segs[1][1]

    # 调用审计:片0 = 1 unknown + 1 succeeded(外部调用重复,如实记录);
    # 片1/2 各 1 succeeded;成功结果无重复
    rows = q(
        "SELECT input_hash, status FROM model_run"
        " WHERE job_id = :j AND stage = 'transcribe'",
        j=job_id,
    )
    by_hash: dict[str, list[str]] = {}
    for h, st in rows:
        by_hash.setdefault(h, []).append(st)
    assert len(by_hash) == 3
    assert any(sorted(v) == ["succeeded", "unknown"] for v in by_hash.values()), by_hash
    assert sum(v.count("succeeded") for v in by_hash.values()) == 3
    assert_no_duplicate_success(job_id)


# ---------- 场景 3:阶段已提交后杀进程,恢复不重复生成 ----------

def test_stage_committed_recovery_no_duplicate_artifacts(client, session, two_orgs):
    """转写阶段(版本+片段+指针)已提交后死于纪要调用窗口:
    接管者复用已提交产物——ASR 零重调、版本不重建、纪要仅一条。"""
    _, job_id = upload(client, session, two_orgs["org_a"], two_orgs["a_admin"])

    # 调用序:ASR 片0/1/2 = 1/2/3,纪要 LLM = 4 → 纪要返回后自杀
    p = start_worker({
        "MEET_MOCK_KILL_AFTER_CALL": "4",
        "MEET_WORKER_LEASE_TTL_SECONDS": "2",
    })
    try:
        wait_for(
            lambda: q(
                "SELECT active_transcript_version_id FROM recording"
                " WHERE id = (SELECT recording_id FROM processing_job WHERE id = :j)",
                j=job_id,
            )[0][0] is not None,
            msg="等待转写版本发布超时",
        )
        wait_for(lambda: p.poll() is not None, msg="等待 worker 自杀超时")
    finally:
        if p.poll() is None:
            kill_worker(p)
    assert p.returncode == 70

    vid_before = q(
        "SELECT active_transcript_version_id FROM recording"
        " WHERE id = (SELECT recording_id FROM processing_job WHERE id = :j)",
        j=job_id,
    )[0][0]
    # LLM 调用死于"已响应、未提交"→ unknown
    llm_stats = dict(q(
        "SELECT status, count(*) FROM model_run"
        " WHERE job_id = :j AND stage = 'minutes' GROUP BY status",
        j=job_id,
    ))
    assert llm_stats == {"unknown": 1}, llm_stats

    # 接管者:对账复用转写版本,只补纪要与索引
    p2 = start_worker()
    try:
        assert wait_job_status(job_id, {"succeeded", "failed"}) == "succeeded"
    finally:
        p2.terminate()

    rid = recording_of_job(job_id)
    # 产物唯一且被复用:同一版本 id,不重建
    assert q("SELECT count(*) FROM transcript_version WHERE recording_id = :r", r=rid)[0][0] == 1
    assert q("SELECT active_transcript_version_id FROM recording WHERE id = :r", r=rid)[0][0] == vid_before
    assert q("SELECT count(*) FROM minutes_version WHERE job_id = :j", j=job_id)[0][0] == 1

    # 外部调用:ASR 零重调(阶段产物复用);LLM = 1 unknown(死于窗口)+ 1 succeeded
    stats = transcribe_run_stats(job_id)
    assert stats == {"succeeded": 3}, stats
    llm_after = dict(q(
        "SELECT status, count(*) FROM model_run"
        " WHERE job_id = :j AND stage = 'minutes' GROUP BY status",
        j=job_id,
    ))
    assert llm_after == {"succeeded": 1, "unknown": 1}, llm_after
    assert_no_duplicate_success(job_id, "minutes")


# ---------- 场景 4:旧 worker 租约失效后被接管,写入被拒 ----------

def test_stale_worker_cannot_commit_after_takeover(client, session, two_orgs):
    """旧 worker 的租约过期 → 新 worker 接管(epoch+1)完成后,
    旧 epoch 的续租与提交均被拒绝。"""
    from app.worker.lease import LeaseLostError, leased_transaction, renew_lease

    _, job_id = upload(client, session, two_orgs["org_a"], two_orgs["a_admin"])

    p = start_worker({
        "MEET_MOCK_ASR_DELAY_MS": "600",
        "MEET_WORKER_LEASE_TTL_SECONDS": "2",
    })
    try:
        wait_for(
            lambda: q("SELECT status FROM processing_job WHERE id = :j", j=job_id)[0][0] == "running",
            msg="等待 worker 领取超时",
        )
    finally:
        kill_worker(p)
    stale_epoch = q("SELECT lease_epoch FROM processing_job WHERE id = :j", j=job_id)[0][0]

    # 等租约过期(TTL=2s)
    wait_for(
        lambda: q(
            "SELECT lease_expires_at < clock_timestamp()::timestamp"
            " FROM processing_job WHERE id = :j",
            j=job_id,
        )[0][0],
        msg="等待租约过期超时",
    )

    # 接管者完成
    p2 = start_worker()
    try:
        assert wait_job_status(job_id, {"succeeded", "failed"}) == "succeeded"
    finally:
        p2.terminate()
    assert q("SELECT lease_epoch FROM processing_job WHERE id = :j", j=job_id)[0][0] > stale_epoch

    # 模拟旧 worker 恢复(网络分区恢复;死进程无法复活,进程内等价模拟):
    # 续租被拒
    assert renew_lease(engine, job_id, stale_epoch, 120) is False
    # 提交被拒
    with pytest.raises(LeaseLostError):
        with leased_transaction(engine, job_id, stale_epoch) as conn:
            conn.execute(
                text(
                    "UPDATE job_stage SET error = '旧worker写入' "
                    "WHERE job_id = :j AND stage_name = 'prepare'"
                ),
                {"j": job_id},
            )
    # 确认旧 worker 的写入未生效
    assert q(
        "SELECT error FROM job_stage WHERE job_id = :j AND stage_name = 'prepare'",
        j=job_id,
    )[0][0] is None

    # 产物完整:接管者完成全部阶段
    rid = recording_of_job(job_id)
    assert q("SELECT count(*) FROM transcript_version WHERE recording_id = :r", r=rid)[0][0] == 1
    assert q(
        "SELECT count(*) FILTER (WHERE is_active) FROM embedding_chunk"
        " JOIN transcript_version tv ON tv.id = embedding_chunk.version_id"
        " WHERE tv.recording_id = :r",
        r=rid,
    )[0][0] == 3
    assert_no_duplicate_success(job_id)


# ---------- 场景 5:索引部分完成,旧版本可查,完成后切换 ----------

V2_TEXTS = [
    "第二轮转写:设备保修延长至8年",
    "第二轮转写:交货日期定在6月15日",
    "第二轮转写:培训周期5天",
]


def test_index_partial_old_version_searchable_then_switch(client, session, two_orgs):
    """索引部分完成时中断:旧发布版本仍可检索;新版本完成后原子切换,
    且历史纪要引用的旧片段仍可解析。"""
    from app.ai.mock_providers import MockEmbed

    meeting, job_id = upload(client, session, two_orgs["org_a"], two_orgs["a_admin"])
    user = two_orgs["a_admin"]

    # 第一轮:正常完成,v1 发布
    p = start_worker()
    try:
        assert wait_job_status(job_id, {"succeeded", "failed"}) == "succeeded"
    finally:
        p.terminate()

    def ask(question: str) -> dict:
        resp = client.post(
            f"/meetings/{meeting.id}/ask",
            json={"question": question},
            headers=auth_headers(client, user.username),
        )
        assert resp.status_code == 200
        return resp.json()

    # v1 可检索(前置)
    r = ask("X200的注册证什么时候补齐?")
    assert r["has_sufficient_evidence"] is True
    assert any("注册证" in c["text"] for c in r["citations"])

    v1_id, rid = q(
        "SELECT active_search_version_id, id FROM recording"
        " WHERE id = (SELECT recording_id FROM processing_job WHERE id = :j)",
        j=job_id,
    )[0]
    org_id = q("SELECT org_id FROM meeting WHERE id = :m", m=meeting.id)[0][0]

    # 构造 v2(re_asr):新版本+片段,指针指向 v2,预插 1 个 is_active=false
    # chunk 模拟"索引部分完成",index 阶段重置待跑
    with engine.begin() as conn:
        v2_id = conn.execute(
            text(
                "INSERT INTO transcript_version (recording_id, version_no, source_type)"
                " SELECT :rid, 2, 're_asr' RETURNING id"
            ),
            {"rid": rid},
        ).scalar()
        seg_ids = []
        for i, t in enumerate(V2_TEXTS):
            sid = conn.execute(
                text(
                    "INSERT INTO transcript_segment"
                    " (version_id, seq, start_ms, end_ms, speaker, text,"
                    "  timestamp_precision)"
                    " VALUES (:vid, :seq, :s, :e, '第二轮转写', :t,"
                    "  'segment_level_from_slice') RETURNING id"
                ),
                {"vid": v2_id, "seq": i + 1, "s": i * 10000, "e": (i + 1) * 10000, "t": t},
            ).scalar()
            seg_ids.append(sid)
        vec = MockEmbed._embed_one(V2_TEXTS[0])
        conn.execute(
            text(
                "INSERT INTO embedding_chunk"
                " (segment_id, version_id, org_id, meeting_id, is_active,"
                "  embedding, text)"
                " VALUES (:sid, :vid, :org, :mid, false, CAST(:vec AS vector), :t)"
            ),
            {"sid": seg_ids[0], "vid": v2_id, "org": org_id, "mid": meeting.id,
             "vec": "[" + ",".join(str(x) for x in vec) + "]", "t": V2_TEXTS[0]},
        )
        conn.execute(
            text("UPDATE recording SET active_transcript_version_id = :vid WHERE id = :rid"),
            {"vid": v2_id, "rid": rid},
        )
        conn.execute(
            text("UPDATE job_stage SET status = 'pending' WHERE job_id = :j AND stage_name = 'index'"),
            {"j": job_id},
        )
        conn.execute(
            text("UPDATE processing_job SET status = 'queued', attempt_count = 0 WHERE id = :j"),
            {"j": job_id},
        )

    # 中断:embed 持续失败 3 次 → job 终态 failed(索引停在部分完成)
    p2 = start_worker({"MEET_MOCK_EMBED_FAIL_ON_CALL": "1,2,3"})
    try:
        assert wait_job_status(job_id, {"failed"}, timeout=60) == "failed"
    finally:
        p2.terminate()

    # 中断状态:指针仍在 v1;v2 仅 1 个 chunk 且不可见
    assert q("SELECT active_search_version_id FROM recording WHERE id = :r", r=rid)[0][0] == v1_id
    assert q("SELECT count(*) FROM embedding_chunk WHERE version_id = :v", v=v2_id)[0][0] == 1
    assert q("SELECT count(*) FROM embedding_chunk WHERE version_id = :v AND is_active", v=v2_id)[0][0] == 0
    # embed 调用记录:v1 第一轮 1 次 succeeded + v2 中断 3 次 failed(如实记录)
    assert dict(q(
        "SELECT status, count(*) FROM model_run"
        " WHERE job_id = :j AND stage = 'index' GROUP BY status", j=job_id,
    )) == {"succeeded": 1, "failed": 3}

    # 旧版本仍可检索(注册证问题);新版本内容不可见
    # (该问题与 v1 三片段实测相似度 <=0.075,无 bigram 碰撞噪声)
    r_old = ask("X200的注册证什么时候补齐?")
    assert r_old["has_sufficient_evidence"] is True
    assert all(q(
        "SELECT 1 FROM transcript_segment WHERE id = :sid", sid=c["segment_id"]
    ) for c in r_old["citations"])
    assert any("注册证" in c["text"] for c in r_old["citations"])
    r_new = ask("保修延长至8年了吗")
    assert r_new["has_sufficient_evidence"] is False
    assert r_new["relevant_candidate_count"] == 0

    # 恢复:重置 index 阶段,worker 完成 v2 索引并原子切换
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE job_stage SET status = 'pending' WHERE job_id = :j AND stage_name = 'index'"),
            {"j": job_id},
        )
        conn.execute(
            text("UPDATE processing_job SET status = 'queued', attempt_count = 0 WHERE id = :j"),
            {"j": job_id},
        )
    p3 = start_worker()
    try:
        assert wait_job_status(job_id, {"succeeded", "failed"}, timeout=60) == "succeeded"
    finally:
        p3.terminate()

    # 切换后:v2 全部 active,指针切换,v1 不可见
    assert q("SELECT active_search_version_id FROM recording WHERE id = :r", r=rid)[0][0] == v2_id
    assert q("SELECT count(*) FROM embedding_chunk WHERE version_id = :v AND is_active", v=v2_id)[0][0] == 3
    assert q("SELECT count(*) FROM embedding_chunk WHERE version_id = :v AND is_active", v=v1_id)[0][0] == 0
    # v2 内容可检索(充分,引用含"8年")
    r_after = ask("保修延长至8年了吗")
    assert r_after["has_sufficient_evidence"] is True
    assert any("8年" in c["text"] for c in r_after["citations"])
    # v1 独有内容不再命中(注册证不在 v2)
    r_v1 = ask("X200的注册证什么时候补齐?")
    assert r_v1["relevant_candidate_count"] == 0
    assert r_v1["has_sufficient_evidence"] is False

    # 历史引用可解析:v1 时代生成的纪要,其证据片段(旧版本)仍可解析
    resp = client.get(
        f"/meetings/{meeting.id}/minutes",
        headers=auth_headers(client, user.username),
    )
    assert resp.status_code == 200
    evidences = [
        e for item in resp.json()["content"]["items"] for e in item["evidence"]
    ]
    assert evidences, "纪要应含证据解析"
    assert any("注册证" in e["text"] for e in evidences), "旧版本片段原文仍可解析"


# ---------- 场景 6:两个 worker 竞争回收 ----------

def test_concurrent_recovery_single_winner(client, session, two_orgs):
    """租约过期后两个 worker 同时竞争回收:claim 原子性保证仅一个接管。"""
    _, job_id = upload(client, session, two_orgs["org_a"], two_orgs["a_admin"])

    p = start_worker({
        "MEET_MOCK_ASR_DELAY_MS": "600",
        "MEET_WORKER_LEASE_TTL_SECONDS": "2",
    })
    try:
        wait_for(
            lambda: q("SELECT status FROM processing_job WHERE id = :j", j=job_id)[0][0] == "running",
            msg="等待 worker 领取超时",
        )
    finally:
        kill_worker(p)
    epoch_before = q("SELECT lease_epoch FROM processing_job WHERE id = :j", j=job_id)[0][0]
    wait_for(
        lambda: q(
            "SELECT lease_expires_at < clock_timestamp()::timestamp"
            " FROM processing_job WHERE id = :j",
            j=job_id,
        )[0][0],
        msg="等待租约过期超时",
    )

    # 两个 worker 同时启动竞争回收
    p2 = start_worker()
    p3 = start_worker()
    try:
        assert wait_job_status(job_id, {"succeeded", "failed"}, timeout=60) == "succeeded"
    finally:
        p2.terminate()
        p3.terminate()

    # 只有一个有效接管者:epoch 仅推进一次
    assert q("SELECT lease_epoch FROM processing_job WHERE id = :j", j=job_id)[0][0] == epoch_before + 1

    # 产物唯一:一套版本/片段/索引/纪要
    rid = recording_of_job(job_id)
    assert q("SELECT count(*) FROM transcript_version WHERE recording_id = :r", r=rid)[0][0] == 1
    assert q(
        "SELECT count(*) FROM transcript_segment ts"
        " JOIN transcript_version tv ON tv.id = ts.version_id"
        " WHERE tv.recording_id = :r",
        r=rid,
    )[0][0] == 3
    assert q(
        "SELECT count(*) FILTER (WHERE is_active) FROM embedding_chunk"
        " JOIN transcript_version tv ON tv.id = embedding_chunk.version_id"
        " WHERE tv.recording_id = :r",
        r=rid,
    )[0][0] == 3
    assert q("SELECT count(*) FROM minutes_version WHERE job_id = :j", j=job_id)[0][0] == 1

    # 成功结果无重复;kill 时刻挂起的调用至多一条 unknown
    stats = transcribe_run_stats(job_id)
    assert stats.get("succeeded") == 3, stats
    assert stats.get("unknown", 0) <= 1, stats
    assert_no_duplicate_success(job_id)


# ---------- 回归:同会谈多录音索引互不覆盖(run_index 切换范围修复) ----------

def test_multi_recording_meeting_both_searchable(client, session, two_orgs):
    """同会谈两条录音各自发布索引后均可检索。

    修复前缺陷:index 切换按 meeting 级翻转 is_active,后完成的录音
    会踢掉先完成录音的索引;修复后翻转范围限定本录音旧版本。
    """
    org, user = two_orgs["org_a"], two_orgs["a_admin"]
    meeting = make_meeting(session, org, user)
    headers = auth_headers(client, user.username)

    # R1:15s(2 片=语料句0/1),R2:25s(3 片=句0/1/2)
    job_ids = []
    for seconds in (15, 25):
        resp = client.post(
            f"/meetings/{meeting.id}/recordings",
            files={"file": (f"m{seconds}.wav", make_wav_bytes(seconds=seconds), "audio/wav")},
            headers=headers,
        )
        assert resp.status_code == 202
        job_ids.append(resp.json()["job_id"])

    p = start_worker()
    try:
        for jid in job_ids:
            assert wait_job_status(jid, {"succeeded", "failed"}) == "succeeded"
    finally:
        p.terminate()

    # 两条录音的索引同时 active(修复前 R1 会被 R2 踢掉)
    assert q(
        "SELECT count(*) FROM embedding_chunk WHERE meeting_id = :m AND is_active",
        m=meeting.id,
    )[0][0] == 5

    # 句2 内容(X300 保修期)仅存在于 R2 → 引用必须来自 R2 的录音
    resp = client.post(
        f"/meetings/{meeting.id}/ask",
        json={"question": "X300型号的保修期是多久?"},
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["relevant_candidate_count"] > 0
    r2_of = recording_of_job(job_ids[1])
    for c in body["citations"]:
        owner = q(
            "SELECT tv.recording_id FROM transcript_segment ts"
            " JOIN transcript_version tv ON tv.id = ts.version_id"
            " WHERE ts.id = :sid",
            sid=c["segment_id"],
        )[0][0]
        assert owner == r2_of

    # 句1 内容(注册证)两条录音都有 → 候选可来自任一/两者,但均可命中
    resp = client.post(
        f"/meetings/{meeting.id}/ask",
        json={"question": "注册证和检测报告什么时候补齐?"},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["has_sufficient_evidence"] is True
