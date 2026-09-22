"""租约机制单元测试(进程内,真实 PostgreSQL)。

覆盖:
- 并发领取只有一个成功;
- 正确租约可提交,过期租约与旧 epoch 均被拒绝(业务结果不落库);
- 续租仅允许当前 epoch 且未过期。
"""
import threading

import pytest
from sqlalchemy import text

from app.core.db import engine
from tests.conftest import auth_headers, make_meeting, make_wav_bytes
from app.worker.lease import (
    LeaseLostError,
    claim_job,
    leased_transaction,
    renew_lease,
)


@pytest.fixture
def queued_job(client, session, two_orgs):
    """上传一段录音,返回 queued 状态的 job_id。"""
    meeting = make_meeting(session, two_orgs["org_a"], two_orgs["a_admin"])
    headers = auth_headers(client, two_orgs["a_admin"].username)
    resp = client.post(
        f"/meetings/{meeting.id}/recordings",
        files={"file": ("a.wav", make_wav_bytes(seconds=10), "audio/wav")},
        headers=headers,
    )
    assert resp.status_code == 202
    return resp.json()["job_id"]


def test_concurrent_claim_single_winner(queued_job):
    """两个线程同时领取,只有一个拿到有效租约。"""
    results = []
    barrier = threading.Barrier(2)

    def try_claim():
        barrier.wait()
        results.append(claim_job(engine, "w-test", ttl_seconds=60))

    t1 = threading.Thread(target=try_claim)
    t2 = threading.Thread(target=try_claim)
    t1.start(); t2.start(); t1.join(); t2.join()

    won = [r for r in results if r is not None]
    assert len(won) == 1, f"应有且仅有一个领取者,实际 {results}"
    assert won[0] == queued_job
    with engine.connect() as conn:
        epoch = conn.execute(
            text("SELECT lease_epoch FROM processing_job WHERE id = :j"),
            {"j": queued_job},
        ).scalar()
    assert epoch == 1


def test_valid_lease_can_commit(queued_job):
    job_id = claim_job(engine, "w1", ttl_seconds=60)
    assert job_id == queued_job
    epoch = _current_epoch(job_id)
    with leased_transaction(engine, job_id, epoch) as conn:
        conn.execute(
            text(
                "UPDATE job_stage SET error = 'probe-ok'"
                " WHERE job_id = :j AND stage_name = 'prepare'"
            ),
            {"j": job_id},
        )
    with engine.connect() as conn:
        err = conn.execute(
            text("SELECT error FROM job_stage WHERE job_id = :j AND stage_name = 'prepare'"),
            {"j": job_id},
        ).scalar()
    assert err == "probe-ok"


def test_stale_epoch_cannot_commit(queued_job):
    """旧 epoch 提交被拒,业务结果不落库。"""
    claim_job(engine, "w1", ttl_seconds=60)          # epoch=1
    job_id2 = claim_job(engine, "w2", ttl_seconds=60)  # 重新领取 → epoch=2?
    # 领取候选只含 queued/过期 running;当前租约未过期,第二次领取应为空
    # → 手动模拟接管:置过期后再领取
    assert job_id2 is None
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE processing_job SET lease_expires_at = clock_timestamp() - interval '1s' WHERE id = :j"),
            {"j": queued_job},
        )
    job_id2 = claim_job(engine, "w2", ttl_seconds=60)
    assert job_id2 == queued_job

    # 旧 worker(epoch=1)尝试提交 → 必须被拒
    with pytest.raises(LeaseLostError):
        with leased_transaction(engine, queued_job, epoch=1) as conn:
            conn.execute(
                text("UPDATE job_stage SET error = 'stale-write' WHERE job_id = :j"),
                {"j": queued_job},
            )
    with engine.connect() as conn:
        err = conn.execute(
            text("SELECT error FROM job_stage WHERE job_id = :j AND stage_name = 'prepare'"),
            {"j": queued_job},
        ).scalar()
    assert err != "stale-write"  # 旧 worker 未覆盖任何结果


def test_expired_lease_cannot_commit(queued_job):
    claim_job(engine, "w1", ttl_seconds=60)
    epoch = _current_epoch(queued_job)
    # 人为将租约置为过期(模拟 worker 停跳)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE processing_job SET lease_expires_at = clock_timestamp() - interval '1s' WHERE id = :j"),
            {"j": queued_job},
        )
    with pytest.raises(LeaseLostError):
        with leased_transaction(engine, queued_job, epoch) as conn:
            conn.execute(text("UPDATE job_stage SET error = 'expired-write' WHERE job_id = :j"), {"j": queued_job})


def test_renew_rules(queued_job):
    job_id = claim_job(engine, "w1", ttl_seconds=60)
    epoch = _current_epoch(job_id)
    # 当前 epoch 且未过期 → 可续
    assert renew_lease(engine, job_id, epoch, 60) is True
    # 旧 epoch → 拒绝
    assert renew_lease(engine, job_id, epoch - 1, 60) is False
    # 已过期 → 拒绝(不允许复活,必须走回收)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE processing_job SET lease_expires_at = clock_timestamp() - interval '1s' WHERE id = :j"),
            {"j": job_id},
        )
    assert renew_lease(engine, job_id, epoch, 60) is False


def _current_epoch(job_id: int) -> int:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT lease_epoch FROM processing_job WHERE id = :j"),
            {"j": job_id},
        ).scalar()
