"""worker 子进程端到端测试:真实独立进程完成流水线,非测试代码直接调用处理函数。"""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.db import engine
from tests.conftest import auth_headers, make_meeting, make_wav_bytes

REPO_ROOT = Path(__file__).resolve().parents[1]


def start_worker(extra_env: dict | None = None, idle_exit: float = 10) -> subprocess.Popen:
    env = {
        **os.environ,
        "PYTHONUTF8": "1",
        "MEET_WORKER_POLL_SECONDS": "0.2",
        "MEET_WORKER_LEASE_TTL_SECONDS": "60",
    }
    env.update(extra_env or {})
    return subprocess.Popen(
        [sys.executable, "-m", "app.worker", "--idle-exit-seconds", str(idle_exit)],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,  # 防止管道缓冲写满导致子进程阻塞
        stderr=subprocess.DEVNULL,
    )


def wait_job_status(job_id: int, statuses: set[str], timeout: float = 60) -> str:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT status, last_error FROM processing_job WHERE id = :j"),
                {"j": job_id},
            ).fetchone()
        last = row[0]
        if last in statuses:
            return last
        time.sleep(0.3)
    raise AssertionError(f"等待任务状态 {statuses} 超时,最后状态: {last}")


def q(sql: str, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).fetchall()


@pytest.fixture
def uploaded_job(client, session, two_orgs):
    """上传 25 秒 WAV(3 个分片:10s+10s+5s),返回 job_id。"""
    meeting = make_meeting(session, two_orgs["org_a"], two_orgs["a_admin"])
    headers = auth_headers(client, two_orgs["a_admin"].username)
    resp = client.post(
        f"/meetings/{meeting.id}/recordings",
        files={"file": ("demo.wav", make_wav_bytes(seconds=25), "audio/wav")},
        headers=headers,
    )
    assert resp.status_code == 202
    return resp.json()["job_id"]


def test_full_pipeline_by_subprocess_worker(uploaded_job):
    """验收1:独立进程完成四阶段;产物齐全且时间来自切片边界。"""
    proc = start_worker()
    try:
        status = wait_job_status(uploaded_job, {"succeeded", "failed"})
        assert status == "succeeded", q(
            "SELECT status, last_error FROM processing_job WHERE id=:j", j=uploaded_job
        )
    finally:
        proc.terminate()

    # 四阶段全部成功
    stages = q(
        "SELECT stage_name, status FROM job_stage WHERE job_id=:j ORDER BY id",
        j=uploaded_job,
    )
    assert [s[1] for s in stages] == ["succeeded"] * 4

    # 版本发布:transcript v1,active 指针就位
    rec = q(
        "SELECT active_transcript_version_id, active_search_version_id, duration_ms"
        " FROM recording r JOIN processing_job j ON j.recording_id = r.id WHERE j.id=:n",
        n=uploaded_job,
    )[0]
    assert rec[0] is not None and rec[1] == rec[0]
    assert rec[2] == 25000  # duration 来自 WAV 实际时长

    # 片段时间 = 切片边界(0-10000/10000-20000/20000-25000),顺序与序号正确
    segs = q(
        "SELECT ts.seq, ts.start_ms, ts.end_ms, ts.text, ts.speaker"
        " FROM transcript_segment ts WHERE ts.version_id=:v ORDER BY ts.seq",
        v=rec[0],
    )
    assert len(segs) == 3
    assert [(s[1], s[2]) for s in segs] == [(0, 10000), (10000, 20000), (20000, 25000)]
    assert all(s[3] for s in segs)  # 文本来自语料
    assert segs[0][4] == "张主任(机构)"  # 语料第0句说话人

    # 纪要:ai_draft 存在,含 todo 且经层1校验(入库即通过)
    minutes = q(
        "SELECT content, status FROM minutes_version WHERE job_id=:j",
        j=uploaded_job,
    )
    assert len(minutes) == 1
    items = minutes[0][0]["items"]
    todos = [i for i in items if i["type"] == "todo"]
    assert todos, "语料含承诺语句,应产生待办"
    # 语料第2句(分片1):注册证5月20日前补齐 → todo 带日期与厂商负责人
    target = [i for i in todos if "补齐" in i["text"]]
    assert target and target[0]["due_date"] == "5月20日"
    assert "李经理" in (target[0]["owner"] or "")
    assert target[0]["evidence_segment_ids"]

    # 索引:chunk 数=segment 数,is_active=true,权限键正确
    chunks = q(
        "SELECT count(*), count(*) FILTER (WHERE is_active),"
        " bool_and(org_id IS NOT NULL AND meeting_id IS NOT NULL)"
        " FROM embedding_chunk WHERE version_id=:v",
        v=rec[0],
    )[0]
    assert chunks[0] == 3 and chunks[1] == 3 and chunks[2]

    # 模型调用全部记录且明确标识 mock
    runs = q("SELECT stage, is_mock, status FROM model_run WHERE job_id=:j", j=uploaded_job)
    assert runs and all(r[1] is True and r[2] == "succeeded" for r in runs)
    assert {r[0] for r in runs} == {"transcribe", "minutes", "index"}


def test_slice_failure_retry_reuses_succeeded_slices(client, session, two_orgs):
    """验收4:分片失败重试时,已成功片段不再调用 mock ASR。"""
    meeting = make_meeting(session, two_orgs["org_a"], two_orgs["a_admin"])
    headers = auth_headers(client, two_orgs["a_admin"].username)
    resp = client.post(
        f"/meetings/{meeting.id}/recordings",
        files={"file": ("r.wav", make_wav_bytes(seconds=25), "audio/wav")},
        headers=headers,
    )
    job_id = resp.json()["job_id"]

    # 注入:全局第 2 次 ASR 调用失败(片0成功,片1失败中止)
    p1 = start_worker({"MEET_MOCK_ASR_FAIL_ON_CALL": "2"})
    try:
        wait_job_status(job_id, {"queued", "failed", "succeeded"}, timeout=30)
        # 第一次失败后 job 回 queued;等待 worker 完成整轮(它自己会重试)
        # 但注入是"第2次调用失败一次",重试后第3次调用会成功 → 直接等最终成功?
        # 为构造"中断后重启"场景,这里等片0成功、片1失败的第一轮即可:
    finally:
        p1.terminate()

    # 无论第一轮走到哪,断言:不存在重复 input_hash 的成功调用记录
    dups = q(
        "SELECT input_hash FROM model_run WHERE job_id=:j AND stage='transcribe'"
        " GROUP BY input_hash HAVING count(*) > 1",
        j=job_id,
    )
    assert dups == [], f"存在重复调用: {dups}"

    # 重启 worker(无注入)完成剩余工作
    p2 = start_worker()
    try:
        assert wait_job_status(job_id, {"succeeded"}) == "succeeded"
    finally:
        p2.terminate()

    # 每片恰好一次成功调用(3 片 3 条 transcribe run,无重复)
    runs = q(
        "SELECT count(*) FROM model_run WHERE job_id=:j AND stage='transcribe'",
        j=job_id,
    )[0][0]
    assert runs == 3


def test_minutes_failure_hides_partial_results(client, session, two_orgs):
    """验收5a:纪要阶段持续失败 → 转写已发布但索引/纪要不可见。"""
    meeting = make_meeting(session, two_orgs["org_a"], two_orgs["a_admin"])
    headers = auth_headers(client, two_orgs["a_admin"].username)
    resp = client.post(
        f"/meetings/{meeting.id}/recordings",
        files={"file": ("m.wav", make_wav_bytes(seconds=10), "audio/wav")},
        headers=headers,
    )
    job_id = resp.json()["job_id"]

    p = start_worker({"MEET_MOCK_LLM_FAIL_ON_CALL": "1,2,3"})
    try:
        assert wait_job_status(job_id, {"failed"}) == "failed"
    finally:
        p.terminate()

    rec = q(
        "SELECT active_transcript_version_id, active_search_version_id"
        " FROM recording r JOIN processing_job j ON j.recording_id=r.id WHERE j.id=:n",
        n=job_id,
    )[0]
    assert rec[0] is not None          # 转写已发布(该阶段成功)
    assert rec[1] is None              # 检索版本未切换
    assert q("SELECT 1 FROM minutes_version WHERE job_id=:j", j=job_id) == []
    assert q("SELECT 1 FROM embedding_chunk WHERE version_id=:v", v=rec[0]) == []
    assert q("SELECT last_error FROM processing_job WHERE id=:j", j=job_id)[0][0]


def test_index_failure_no_active_chunk(client, session, two_orgs):
    """验收5b:索引构建中途失败 → 半成品 chunk 全部 is_active=false。"""
    # 独立上传新录音,确保故障注入只消耗在本任务上
    meeting = make_meeting(session, two_orgs["org_a"], two_orgs["a_admin"])
    headers = auth_headers(client, two_orgs["a_admin"].username)
    resp = client.post(
        f"/meetings/{meeting.id}/recordings",
        files={"file": ("idx.wav", make_wav_bytes(seconds=10), "audio/wav")},
        headers=headers,
    )
    job_id = resp.json()["job_id"]

    p = start_worker({"MEET_MOCK_EMBED_FAIL_ON_CALL": "1,2,3"})
    try:
        assert wait_job_status(job_id, {"failed"}) == "failed"
    finally:
        p.terminate()

    rec = q(
        "SELECT active_transcript_version_id, active_search_version_id"
        " FROM recording r JOIN processing_job j ON j.recording_id=r.id WHERE j.id=:n",
        n=job_id,
    )[0]
    assert rec[0] is not None
    assert rec[1] is None
    # 若有部分 chunk 写入(10s 音频仅 1 片 1 批,可能 0 行),必须全部不可见
    active = q(
        "SELECT count(*) FROM embedding_chunk WHERE version_id=:v AND is_active",
        v=rec[0],
    )[0][0]
    assert active == 0


def test_rerun_after_stage_reset_no_duplicates(uploaded_job):
    """验收6:阶段已成功后重复执行,不新增重复产物。"""
    p = start_worker()
    try:
        assert wait_job_status(uploaded_job, {"succeeded"}) == "succeeded"
    finally:
        p.terminate()

    before = {
        "versions": q("SELECT count(*) FROM transcript_version")[0][0],
        "minutes": q("SELECT count(*) FROM minutes_version")[0][0],
        "segments": q("SELECT count(*) FROM transcript_segment")[0][0],
        "chunks": q("SELECT count(*) FROM embedding_chunk")[0][0],
        "runs": q("SELECT count(*) FROM model_run WHERE job_id=:j", j=uploaded_job)[0][0],
    }

    # 模拟阶段行丢失:全部 stage 置 pending,job 回队列(执行器须靠对账幂等)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE job_stage SET status='pending' WHERE job_id=:j"),
            {"j": uploaded_job},
        )
        conn.execute(
            text("UPDATE processing_job SET status='queued', attempt_count=0 WHERE id=:j"),
            {"j": uploaded_job},
        )

    p2 = start_worker()
    try:
        assert wait_job_status(uploaded_job, {"succeeded"}) == "succeeded"
    finally:
        p2.terminate()

    after = {
        "versions": q("SELECT count(*) FROM transcript_version")[0][0],
        "minutes": q("SELECT count(*) FROM minutes_version")[0][0],
        "segments": q("SELECT count(*) FROM transcript_segment")[0][0],
        "chunks": q("SELECT count(*) FROM embedding_chunk")[0][0],
        "runs": q("SELECT count(*) FROM model_run WHERE job_id=:j", j=uploaded_job)[0][0],
    }
    assert after == before, f"重复执行产生了新产物: {before} → {after}"


def test_attempt_limit_and_error_reported(client, session, two_orgs):
    """持续失败 3 次后任务终态 failed,原因落库。"""
    meeting = make_meeting(session, two_orgs["org_a"], two_orgs["a_admin"])
    headers = auth_headers(client, two_orgs["a_admin"].username)
    resp = client.post(
        f"/meetings/{meeting.id}/recordings",
        files={"file": ("f.wav", make_wav_bytes(seconds=10), "audio/wav")},
        headers=headers,
    )
    job_id = resp.json()["job_id"]

    p = start_worker({"MEET_MOCK_ASR_FAIL_SLICE": "0"})
    try:
        assert wait_job_status(job_id, {"failed"}, timeout=60) == "failed"
    finally:
        p.terminate()

    row = q(
        "SELECT attempt_count, last_error FROM processing_job WHERE id=:j", j=job_id
    )[0]
    assert row[0] >= 3
    assert row[1]  # 失败原因可读
