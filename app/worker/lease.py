"""任务租约:原子领取、续租与短事务提交守卫。

设计约定(P1.2):
- 领取为独立事务,立即提交;不持有任何数据库锁执行模型调用;
- 所有业务提交走 leased_transaction 短事务:
  锁 job 行 → 用数据库 clock_timestamp()(实时钟,区别于事务开始时刻的 now())
  校验 epoch 与有效期 → 写产物并更新阶段 → 提交;校验失败即放弃,旧 worker 不得覆盖;
- 续租只允许当前 epoch 且尚未过期的租约(过期必须等回收,不允许复活);
- 保护范围 = 数据库提交与产物发布;不能阻止旧 worker 继续产生外部调用或临时文件
  (能力边界,如实声明)。
"""
import uuid
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from app.core.db import engine
from app.models.enums import JobStatus


class LeaseLostError(Exception):
    """租约已过期或被其他 worker 接管,必须放弃本次任务的所有提交。"""


def worker_id() -> str:
    return f"{uuid.uuid4().hex[:8]}@{uuid.uuid4().hex[:4]}"


def claim_job(eng: Engine, wid: str, ttl_seconds: int, max_attempts: int = 3) -> int | None:
    """原子领取一个任务,返回 job_id;无任务返回 None。

    领取事务内完成并立即提交。调度守卫(P2 收尾):
    - 候选含 queued 与租约过期的 running(过期回收);
    - attempt_count 达上限的任务不领取(重试上限参与调度);
    - next_run_at 未到的任务不领取(失败退避);
    - 组织累计模型费用(SUM(model_run.cost_estimate))已达预算
      (organization.budget_cents,NULL=不限)的任务不领取(费用预算参与调度)。
    """
    sql = text(
        """
        UPDATE processing_job SET
            status = 'running',
            lease_epoch = lease_epoch + 1,
            lease_expires_at = clock_timestamp()::timestamp + (:ttl || ' seconds')::interval,
            locked_by = :wid,
            attempt_count = attempt_count + 1,
            updated_at = clock_timestamp()
        WHERE id = (
            SELECT j.id FROM processing_job j
            JOIN recording r ON r.id = j.recording_id
            JOIN meeting m ON m.id = r.meeting_id
            JOIN organization o ON o.id = m.org_id
            WHERE (j.status = 'queued'
                   OR (j.status = 'running' AND j.lease_expires_at < clock_timestamp()))
              AND (j.next_run_at IS NULL
                   OR j.next_run_at <= clock_timestamp()::timestamp)
              AND j.attempt_count < :max_attempts
              AND (o.budget_cents IS NULL OR o.budget_cents > COALESCE((
                    SELECT SUM(mr.cost_estimate) FROM model_run mr
                    WHERE mr.org_id = o.id), 0))
            ORDER BY j.created_at
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING id
        """
    )
    with eng.begin() as conn:
        row = conn.execute(
            sql, {"ttl": str(ttl_seconds), "wid": wid, "max_attempts": max_attempts}
        ).fetchone()
    return row[0] if row else None


def renew_lease(eng: Engine, job_id: int, epoch: int, ttl_seconds: int) -> bool:
    """续租:仅当前 epoch 且未过期才允许;失败意味着租约已丢失。"""
    sql = text(
        """
        UPDATE processing_job
        SET lease_expires_at = clock_timestamp()::timestamp + (:ttl || ' seconds')::interval,
            updated_at = clock_timestamp()
        WHERE id = :job_id
          AND lease_epoch = :epoch
          AND lease_expires_at > clock_timestamp()
        """
    )
    with eng.begin() as conn:
        result = conn.execute(
            sql, {"job_id": job_id, "epoch": epoch, "ttl": str(ttl_seconds)}
        )
    return result.rowcount == 1


def read_lease(eng: Engine, job_id: int) -> tuple[int, str | None]:
    """读取当前 epoch 与状态(供 runner 校验自持租约)。"""
    with eng.connect() as conn:
        row = conn.execute(
            text(
                "SELECT lease_epoch, status FROM processing_job WHERE id = :id"
            ),
            {"id": job_id},
        ).fetchone()
    if row is None:
        raise LeaseLostError(f"job {job_id} 不存在")
    return row[0], row[1]


@contextmanager
def leased_transaction(eng: Engine, job_id: int, epoch: int):
    """短事务提交守卫:锁行 → clock_timestamp 校验 → yield 连接 → 提交。

    用法:
        with leased_transaction(engine, job_id, my_epoch) as conn:
            conn.execute(insert_stmt)
            conn.execute(update_stage_stmt)
    校验失败抛 LeaseLostError,事务回滚,旧 worker 不得覆盖结果。
    """
    conn: Connection = eng.connect()
    try:
        conn.begin()
        row = conn.execute(
            text(
                "SELECT lease_epoch, lease_expires_at, status"
                " FROM processing_job WHERE id = :id FOR UPDATE"
            ),
            {"id": job_id},
        ).fetchone()
        # 取数据库实时钟(转为 naive 与列类型一致;clock_timestamp 区别于
        # 事务开始时刻的 now,避免等待行锁期间租约已过期却被误判有效)
        now = conn.execute(text("SELECT clock_timestamp()::timestamp")).scalar()
        if (
            row is None
            or row[0] != epoch
            or row[1] is None
            or row[1] <= now
            or row[2] != JobStatus.running.value
        ):
            conn.rollback()
            raise LeaseLostError(
                f"job {job_id} 租约校验失败(epoch={epoch}, 当前epoch={row[0] if row else '?'})"
            )
        yield conn
        conn.commit()
    except LeaseLostError:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def requeue_or_fail(
    eng,
    job_id: int,
    epoch: int,
    stage_name: str,
    error: str,
    max_attempts: int,
    attempt_count: int,
    backoff_seconds: float = 0.0,
    backoff_max_seconds: float = 60.0,
) -> str:
    """阶段失败后的短事务处理:记录错误,回 queued 或终态 failed。

    返回新状态。attempt_count 为领取时已累计的值(claim 时 +1 过)。
    回队时写入退避:next_run_at = now + min(attempt * backoff, cap);
    backoff_seconds=0 禁用(立即可领)。
    """
    backoff = 0.0
    if backoff_seconds > 0 and attempt_count < max_attempts:
        backoff = min(attempt_count * backoff_seconds, backoff_max_seconds)
    with leased_transaction(eng, job_id, epoch) as conn:
        conn.execute(
            text(
                "UPDATE job_stage SET status = 'failed',"
                " error = :err, attempt_count = attempt_count + 1,"
                " finished_at = clock_timestamp()"
                " WHERE job_id = :job_id AND stage_name = :stage"
            ),
            {"err": error, "job_id": job_id, "stage": stage_name},
        )
        new_status = (
            JobStatus.failed.value
            if attempt_count >= max_attempts
            else JobStatus.queued.value
        )
        conn.execute(
            text(
                "UPDATE processing_job SET status = :st,"
                " last_error = :err, current_stage = :stage,"
                " lease_expires_at = NULL,"
                " next_run_at = CASE WHEN CAST(:st AS varchar) = 'queued'"
                "  THEN clock_timestamp()::timestamp + (:backoff || ' seconds')::interval"
                "  ELSE NULL END,"
                " updated_at = clock_timestamp()"
                " WHERE id = :job_id"
            ),
            {"st": new_status, "err": error, "stage": stage_name,
             "job_id": job_id, "backoff": str(backoff)},
        )
    return new_status
