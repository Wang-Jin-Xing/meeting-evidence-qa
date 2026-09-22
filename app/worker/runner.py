"""worker 主循环与 CLI 入口(python -m app.worker)。

能力边界(如实声明,P2 收尾后):
- 阶段内重试(含跨进程失败后重跑)已实现并测试;
- 租约过期回收已实现,并经 worker 子进程 kill 的系统测试验证
  ("kill → 租约过期 → 接管 epoch+1 → 产物唯一",见 tests/test_recovery.py);
  被杀进程无法复活,"旧 worker 恢复后写入被拒"以进程内持旧 epoch 模拟;
- 重试退避已实现(next_run_at,按 attempt 递增封顶),重试上限与费用预算
  在 claim 侧参与调度;调用级预算预留尚未实现(长任务总开销不受约束);
- 阶段执行带期限与单调用超时(见 stages._call_provider);超时后的调用线程
  无法强杀,只保证任务不再阻塞,不保证远端已停止处理。
"""
import argparse
import sys
import threading
import time

from sqlalchemy import text

from app.ai.mock_providers import get_providers
from app.core.config import get_settings
from app.core.db import engine
from app.models.enums import STAGE_ORDER
from app.worker import stages
from app.worker.lease import (
    LeaseLostError,
    claim_job,
    leased_transaction,
    read_lease,
    renew_lease,
    requeue_or_fail,
    worker_id,
)


def _artifact_exists(eng, ctx: stages.JobContext, stage: str) -> bool:
    """对账:succeeded 阶段的业务产物是否真实存在,防'账面成功、实物丢失'。"""
    with eng.connect() as conn:
        if stage == "prepare":
            n = conn.execute(
                text("SELECT count(*) FROM transcribe_slice WHERE job_id = :j"),
                {"j": ctx.job_id},
            ).scalar()
            return n > 0
        if stage == "transcribe":
            v = conn.execute(
                text("SELECT active_transcript_version_id FROM recording WHERE id = :r"),
                {"r": ctx.recording_id},
            ).scalar()
            return v is not None
        if stage == "minutes":
            return conn.execute(
                text("SELECT 1 FROM minutes_version WHERE job_id = :j"),
                {"j": ctx.job_id},
            ).fetchone() is not None
        if stage == "index":
            pair = conn.execute(
                text(
                    "SELECT active_search_version_id, active_transcript_version_id"
                    " FROM recording WHERE id = :r"
                ),
                {"r": ctx.recording_id},
            ).fetchone()
            return pair[0] is not None and pair[0] == pair[1]
    return False


def process_job(eng, providers, job_id: int, settings, should_abort) -> str:
    """执行一个已领取的任务,返回最终状态。

    每阶段执行带期限(stage_deadline_seconds)与单调用超时
    (provider_call_timeout_seconds):防止心跳活着但任务永久卡在
    外部调用上——超时/超期后放弃本次执行并回队重试(心跳随之停止)。
    """
    asr, llm, embed = providers
    epoch, _ = read_lease(eng, job_id)
    ctx = stages.load_context(eng, job_id)

    def stage_deadline() -> float | None:
        if settings.stage_deadline_seconds <= 0:
            return None
        return time.monotonic() + settings.stage_deadline_seconds

    p_timeout = settings.provider_call_timeout_seconds
    for stage in STAGE_ORDER:
        stage_name = stage.value
        with eng.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT status FROM job_stage"
                    " WHERE job_id = :j AND stage_name = :s"
                ),
                {"j": job_id, "s": stage_name},
            ).fetchone()
        if row is None:
            raise StageErrorMissing(f"阶段行缺失: {stage_name}")
        if row[0] == "succeeded" and _artifact_exists(eng, ctx, stage_name):
            continue  # 复用已成功阶段
        # succeeded 但产物丢失 → 执行器幂等重跑

        try:
            if stage_name == "prepare":
                stages.run_prepare(
                    eng, ctx, epoch, settings.slice_seconds, should_abort
                )
            elif stage_name == "transcribe":
                stages.run_transcribe(
                    eng, ctx, epoch, asr, should_abort,
                    provider_timeout=p_timeout, deadline=stage_deadline(),
                )
            elif stage_name == "minutes":
                stages.run_minutes(
                    eng, ctx, epoch, llm, should_abort,
                    provider_timeout=p_timeout, deadline=stage_deadline(),
                )
            elif stage_name == "index":
                stages.run_index(
                    eng, ctx, epoch, embed, should_abort,
                    provider_timeout=p_timeout, deadline=stage_deadline(),
                )
        except LeaseLostError:
            raise  # 租约丢失:放弃本任务全部提交,交由上层处理
        except Exception as e:
            # StageError/StageDeadlineError/Provider 超时(结果未知)等
            # 一切阶段异常:统一走失败/重试路径(带退避),不让异常杀死 worker
            with eng.connect() as conn:
                attempt = conn.execute(
                    text("SELECT attempt_count FROM processing_job WHERE id = :j"),
                    {"j": job_id},
                ).scalar()
            return requeue_or_fail(
                eng, job_id, epoch, stage_name, f"{type(e).__name__}: {e}",
                settings.worker_max_attempts, attempt,
                backoff_seconds=settings.retry_backoff_seconds,
                backoff_max_seconds=settings.retry_backoff_max_seconds,
            )

    # 收尾:全部阶段成功
    with leased_transaction(eng, job_id, epoch) as conn:
        conn.execute(
            text(
                "UPDATE processing_job SET status = 'succeeded',"
                " current_stage = NULL, lease_expires_at = NULL,"
                " next_run_at = NULL, last_error = NULL,"
                " updated_at = clock_timestamp()"
                " WHERE id = :j"
            ),
            {"j": job_id},
        )
    return "succeeded"


class StageErrorMissing(Exception):
    pass


def run(idle_exit_seconds: float = 0.0) -> int:
    """主循环。idle_exit_seconds>0 时连续空闲该秒数后退出(测试用)。"""
    settings = get_settings()
    providers = get_providers()
    wid = worker_id()
    ttl = settings.worker_lease_ttl_seconds
    idle_since: float | None = None

    while True:
        job_id = claim_job(
            engine, wid, ttl, max_attempts=settings.worker_max_attempts
        )
        if job_id is None:
            now = time.monotonic()
            if idle_exit_seconds > 0:
                idle_since = idle_since or now
                if now - idle_since >= idle_exit_seconds:
                    print(f"[worker {wid}] 空闲退出", flush=True)
                    return 0
            time.sleep(settings.worker_poll_seconds)
            continue

        idle_since = None
        print(f"[worker {wid}] 领取任务 {job_id}", flush=True)
        lost = threading.Event()
        stop_hb = threading.Event()

        def heartbeat():
            interval = max(1.0, ttl / 3)
            while not stop_hb.wait(interval):
                try:
                    _, status = read_lease(engine, job_id)
                    if status != "running":
                        lost.set()
                        return
                    if not renew_lease(engine, job_id, _hb_epoch[0], ttl):
                        lost.set()
                        return
                except LeaseLostError:
                    lost.set()
                    return

        # 心跳线程启动后需要本线程的 epoch;先读取再启动
        _hb_epoch = [read_lease(engine, job_id)[0]]
        hb = threading.Thread(target=heartbeat, daemon=True)
        hb.start()
        try:
            result = process_job(engine, providers, job_id, settings, lost.is_set)
            print(f"[worker {wid}] 任务 {job_id} → {result}", flush=True)
        except LeaseLostError:
            print(f"[worker {wid}] 任务 {job_id} 租约丢失,放弃", flush=True)
        except StageErrorMissing as e:
            print(f"[worker {wid}] 任务 {job_id} 异常: {e}", flush=True)
        finally:
            stop_hb.set()
            hb.join(timeout=2)


def main():
    parser = argparse.ArgumentParser(description="会谈录音处理 worker")
    parser.add_argument(
        "--idle-exit-seconds", type=float, default=0,
        help="连续空闲 N 秒后退出(默认 0=常驻;测试用)",
    )
    args = parser.parse_args()
    sys.exit(run(args.idle_exit_seconds))


if __name__ == "__main__":
    main()
