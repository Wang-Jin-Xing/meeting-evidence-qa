"""P2 收尾故障测试:结果未知分类、调用超时、阶段期限、重试退避与调度守卫。

关键区分(评审要求):
- "结果没有重复"(每 input_hash 恰好一条 succeeded)≠ "外部调用没有重复"
  (物理调用数,由 Provider 入口调用记录 MEET_MOCK_REQUEST_LOG 验证——
   它记录 mock 方法被调用的事实;model_run 的前置记录只证明调用意图。
   注:该记录在本地,不能证明真实服务商已收到请求);
- 结果未知(超时/断连)保留 unknown,不记 failed;明确失败才 failed;
- 心跳存活不代表任务有进展:调用超时 + 阶段期限 + 退避 + attempt/预算
  参与调度,防止任务永久卡住或风暴式重试。
"""
import json
import time
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.db import engine
from tests.conftest import auth_headers, make_meeting, make_wav_bytes
from tests.test_worker_e2e import q, start_worker, wait_job_status

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _no_stale_pending_jobs():
    """测试开始前清理遗留 queued/running 任务(防注入错位,见 test_recovery)。"""
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE processing_job SET status = 'failed',"
                " last_error = '测试间清理:残留任务置终态'"
                " WHERE status IN ('queued', 'running')"
            )
        )
    yield


def upload(client, session, org, user, seconds: float = 25):
    meeting = make_meeting(session, org, user)
    resp = client.post(
        f"/meetings/{meeting.id}/recordings",
        files={"file": (f"rec-{meeting.id}.wav", make_wav_bytes(seconds=seconds), "audio/wav")},
        headers=auth_headers(client, user.username),
    )
    assert resp.status_code == 202
    return meeting, resp.json()["job_id"]


def read_requests(path) -> list[dict]:
    """读取 Provider 入口调用记录(本地:证明 mock 被调用,不证明服务商已接收)。"""
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line]


def stage_stats(job_id: int, stage: str) -> dict[str, int]:
    rows = q(
        "SELECT status, count(*) FROM model_run"
        " WHERE job_id = :j AND stage = :s GROUP BY status",
        j=job_id, s=stage,
    )
    return dict(rows)


# ---------- 请求日志:物理调用数独立验证 ----------

def test_request_log_verifies_physical_call_count(client, session, two_orgs, tmp_path):
    """模型已响应未提交杀进程:请求日志独立证明物理调用数。

    model_run 只能证明意图/结果;mock 请求日志(服务端视角)证明请求
    实际发出——死前 1 次 + 重发 1 次 + 片1/2 各 1 次 = 4 次 asr 请求,
    其中成功结果只有 3 条(片0 结果丢失后重发),外部调用确实重复。
    """
    log = tmp_path / "req.jsonl"
    _, job_id = upload(client, session, two_orgs["org_a"], two_orgs["a_admin"])

    p = start_worker({
        "MEET_MOCK_KILL_AFTER_CALL": "1",
        "MEET_MOCK_REQUEST_LOG": str(log),
        "MEET_WORKER_LEASE_TTL_SECONDS": "2",
    })
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and p.poll() is None:
            time.sleep(0.1)
        assert p.poll() is not None and p.returncode == 70
    finally:
        if p.poll() is None:
            p.kill(); p.wait()

    # 死亡现场:请求已发出且到达(日志 1 条),model_run 记录 unknown(仅意图+未知)
    reqs = read_requests(log)
    assert [r["provider"] for r in reqs] == ["asr"]
    assert stage_stats(job_id, "transcribe") == {"unknown": 1}

    # 接管重发,最终完成
    p2 = start_worker({"MEET_MOCK_REQUEST_LOG": str(log)})
    try:
        assert wait_job_status(job_id, {"succeeded", "failed"}) == "succeeded"
    finally:
        p2.terminate()

    # 物理调用数(asr=4)> 成功结果数(3):片0 死前 1 次 + 重发 1 次 + 片1/2
    reqs = read_requests(log)
    asr = [r for r in reqs if r["provider"] == "asr"]
    assert len(asr) == 4, asr
    assert sum(1 for r in asr if r["slice_index"] == 0) == 2, "片0 物理上调用了两次"
    assert stage_stats(job_id, "transcribe") == {"succeeded": 3, "unknown": 1}


# ---------- 结果未知 vs 明确失败的分类 ----------

def test_timeout_semantic_kept_as_unknown_not_failed(client, session, two_orgs):
    """超时/断连语义(结果未知)保留 unknown;重试后成功,不误标 failed。"""
    _, job_id = upload(client, session, two_orgs["org_a"], two_orgs["a_admin"])
    p = start_worker({"MEET_MOCK_ASR_UNKNOWN_ON_CALL": "1"})
    try:
        assert wait_job_status(job_id, {"succeeded", "failed"}, timeout=60) == "succeeded"
    finally:
        p.terminate()
    stats = stage_stats(job_id, "transcribe")
    assert stats == {"succeeded": 3, "unknown": 1}, stats
    assert stats.get("failed", 0) == 0, "结果未知不得记为 failed"


def test_explicit_failure_recorded_as_failed(client, session, two_orgs):
    """明确执行失败(远端确认失败)记 failed,与结果未知区分。"""
    _, job_id = upload(client, session, two_orgs["org_a"], two_orgs["a_admin"])
    p = start_worker({"MEET_MOCK_ASR_FAIL_ON_CALL": "1"})
    try:
        assert wait_job_status(job_id, {"succeeded", "failed"}, timeout=60) == "succeeded"
    finally:
        p.terminate()
    stats = stage_stats(job_id, "transcribe")
    assert stats == {"succeeded": 3, "failed": 1}, stats


# ---------- 调用超时兜底:挂起调用不永久卡任务 ----------

def test_provider_call_timeout_breaks_hang(client, session, two_orgs, tmp_path):
    """外部调用挂起:超时兜底打断(结果未知),重试后完成,任务不卡死。"""
    log = tmp_path / "req.jsonl"
    _, job_id = upload(client, session, two_orgs["org_a"], two_orgs["a_admin"])
    p = start_worker({
        "MEET_MOCK_ASR_HANG_ON_CALL": "1",       # 第 1 次 ASR 调用挂起 1 小时
        "MEET_WORKER_PROVIDER_TIMEOUT_SECONDS": "2",  # 2 秒超时兜底
        "MEET_MOCK_REQUEST_LOG": str(log),
    })
    try:
        assert wait_job_status(job_id, {"succeeded", "failed"}, timeout=90) == "succeeded"
    finally:
        p.terminate()

    stats = stage_stats(job_id, "transcribe")
    assert stats == {"succeeded": 3, "unknown": 1}, stats
    # 挂起的那次请求已发出(日志 4 条),结果未知,重发后成功
    asr = [r for r in read_requests(log) if r["provider"] == "asr"]
    assert len(asr) == 4, asr


def test_stage_deadline_forces_retry_and_progress(client, session, two_orgs, tmp_path):
    """阶段期限:每片 600ms × 3 片 > 1s 期限 → 期限内做不完即回队重试,
    已成功分片复用(缓存),最终有限次尝试内完成,任务不卡死。"""
    log = tmp_path / "req.jsonl"
    _, job_id = upload(client, session, two_orgs["org_a"], two_orgs["a_admin"])
    p = start_worker({
        "MEET_MOCK_ASR_DELAY_MS": "600",
        "MEET_WORKER_STAGE_DEADLINE_SECONDS": "1",
        "MEET_WORKER_PROVIDER_TIMEOUT_SECONDS": "30",
        "MEET_MOCK_REQUEST_LOG": str(log),
    })
    try:
        assert wait_job_status(job_id, {"succeeded", "failed"}, timeout=90) == "succeeded"
    finally:
        p.terminate()

    attempts = q("SELECT attempt_count FROM processing_job WHERE id = :j", j=job_id)[0][0]
    assert attempts >= 2, "期限应至少触发一次回队重试"
    stats = stage_stats(job_id, "transcribe")
    assert stats.get("succeeded") == 3
    assert stats.get("unknown", 0) >= 1, "期限耗尽的调用以超时(结果未知)结束"
    # 已成功分片经缓存复用:物理调用 = 3 片 + 至少 1 次超时调用
    asr = [r for r in read_requests(log) if r["provider"] == "asr"]
    assert len(asr) >= 4


def test_call_provider_deadline_unit():
    """进程内单测:期限耗尽时不再发出调用(StageDeadlineError,可重试路径)。"""
    from app.worker.stages import StageDeadlineError, _call_provider

    def slow():
        time.sleep(5)
        return "x"

    with pytest.raises(StageDeadlineError):
        _call_provider(slow, 0)  # 期限已耗尽 → 不发出调用
    with pytest.raises(Exception) as ei:
        _call_provider(slow, 0.2)  # 超时 → 结果未知
    assert type(ei.value).__name__ == "ProviderResultUnknownError"


# ---------- 重试退避与调度守卫 ----------

def test_retry_backoff_delays_reclaim(client, session, two_orgs):
    """失败后按 attempt 退避:next_run_at 未到不被重领。"""
    _, job_id = upload(client, session, two_orgs["org_a"], two_orgs["a_admin"])
    p = start_worker({
        "MEET_MOCK_ASR_FAIL_ON_CALL": "1",     # 第一次调用失败
        "MEET_WORKER_RETRY_BACKOFF_SECONDS": "3",  # 退避 3s
    }, idle_exit=20)
    try:
        t_requeued = None
        t_second_attempt = None
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            status, attempt = q(
                "SELECT status, attempt_count FROM processing_job WHERE id = :j",
                j=job_id,
            )[0]
            if status == "queued" and attempt == 1 and t_requeued is None:
                t_requeued = time.monotonic()
            if attempt >= 2 and t_second_attempt is None:
                t_second_attempt = time.monotonic()
            if status == "succeeded":
                break
            time.sleep(0.1)
        assert q("SELECT status FROM processing_job WHERE id = :j", j=job_id)[0][0] == "succeeded"
        assert t_requeued is not None and t_second_attempt is not None
        assert t_second_attempt - t_requeued >= 2.5, (
            f"退避未生效:回队到重领仅 {t_second_attempt - t_requeued:.1f}s"
        )
    finally:
        p.terminate()
    # next_run_at 在成功后被清(不再阻塞)
    assert q("SELECT next_run_at FROM processing_job WHERE id = :j", j=job_id)[0][0] is None


def test_attempt_limit_blocks_claim(client, session, two_orgs):
    """attempt 达上限的任务即使被置回 queued 也不被领取(重试上限参与调度)。"""
    _, job_id = upload(client, session, two_orgs["org_a"], two_orgs["a_admin"])
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE processing_job SET attempt_count = 3, status = 'queued'"
                " WHERE id = :j"
            ),
            {"j": job_id},
        )
    p = start_worker(idle_exit=4)
    try:
        p.wait(timeout=15)
    finally:
        if p.poll() is None:
            p.terminate()
    row = q("SELECT status, attempt_count, lease_epoch FROM processing_job WHERE id = :j", j=job_id)[0]
    assert row == ("queued", 3, 0), f"超限任务不应被领取: {row}"


def test_budget_blocks_claim_until_raised(client, session, two_orgs):
    """组织累计费用达预算:任务不被领取;提高预算后恢复调度。"""
    from app.models.org import Organization

    org = two_orgs["org_a"]
    meeting, job_id = upload(client, session, org, two_orgs["a_admin"])

    # 制造费用:手工记一笔已成功调用的费用(真实 Provider 由适配器写入)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO model_run (org_id, stage, provider, model_name,"
                " input_hash, status, is_mock, idempotency_key_sent, cost_estimate)"
                " VALUES (:org, 'manual', 'test', 'test', 'budget-probe',"
                " 'succeeded', true, false, 100)"
            ),
            {"org": org.id},
        )
        conn.execute(
            text("UPDATE organization SET budget_cents = 50 WHERE id = :o"),
            {"o": org.id},
        )

    p = start_worker(idle_exit=4)
    try:
        p.wait(timeout=15)
    finally:
        if p.poll() is None:
            p.terminate()
    row = q("SELECT status, lease_epoch FROM processing_job WHERE id = :j", j=job_id)[0]
    assert row == ("queued", 0), f"超预算任务不应被领取: {row}"

    # 提高预算后恢复调度
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE organization SET budget_cents = 1000 WHERE id = :o"),
            {"o": org.id},
        )
    p2 = start_worker()
    try:
        assert wait_job_status(job_id, {"succeeded", "failed"}) == "succeeded"
    finally:
        p2.terminate()

    # 无预算组织不受影响(对照:org_b 无预算,任务正常领取)——由全量
    # 其他测试隐式覆盖;本测试聚焦预算开关联动。
    _ = Organization, meeting
