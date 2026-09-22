"""worker 主循环与 CLI 入口(python -m app.worker)。

P1.2 能力边界(如实声明):
- 阶段内重试(含跨进程失败后重跑)已支持并测试;
- 租约过期回收路径已实现(claim 候选含过期 running),但
  "杀死 worker 进程后的完整崩溃恢复"系统测试属 P2,本轮不宣称验证;
- 无重试退避间隔(失败立即回队列),真实 Provider 接入前再评估。
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
    """执行一个已领取的任务,返回最终状态。"""
    asr, llm, embed = providers
    epoch, _ = read_lease(eng, job_id)
    ctx = stages.load_context(eng, job_id)

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
                stages.run_transcribe(eng, ctx, epoch, asr, should_abort)
            elif stage_name == "minutes":
                stages.run_minutes(eng, ctx, epoch, llm, should_abort)
            elif stage_name == "index":
                stages.run_index(eng, ctx, epoch, embed, should_abort)
        except LeaseLostError:
            raise  # 租约丢失:放弃本任务全部提交,交由上层处理
        except Exception as e:
            # StageError 及 Provider 等一切阶段异常:统一走失败/重试路径,
            # 不让异常杀死 worker 进程
            with eng.connect() as conn:
                attempt = conn.execute(
                    text("SELECT attempt_count FROM processing_job WHERE id = :j"),
                    {"j": job_id},
                ).scalar()
            return requeue_or_fail(
                eng, job_id, epoch, stage_name, f"{type(e).__name__}: {e}",
                settings.worker_max_attempts, attempt,
            )

    # 收尾:全部阶段成功
    with leased_transaction(eng, job_id, epoch) as conn:
        conn.execute(
            text(
                "UPDATE processing_job SET status = 'succeeded',"
                " current_stage = NULL, lease_expires_at = NULL,"
                " last_error = NULL, updated_at = clock_timestamp()"
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
        job_id = claim_job(engine, wid, ttl)
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
