"""预算执行:每次外部调用**前**原子预留,返回后结算(D-38/D-42)。

为什么不能只在领取任务时比较历史费用:一个长任务可能有数百个分片调用,
领取时"未超预算"不代表第 200 次调用时仍未超。预留必须在调用发出前完成。

口径:
- 机构已占用额度 = SUM(model_run.cost_estimate)(含 unknown 状态按预估计入的部分)
  + organization.reserved_cents(在途预留);
- 预算为 NULL = 不限;
- mock 调用不产生真实费用:预留额按 0 处理,不计入额度、也不触发核算缺口;
- 真实调用若 cost_estimate 为 NULL(成本未知)即构成**核算缺口**:该机构设置了
  预算时,后续调用与领取一律拒绝——宁可停住,也不把未知当 0。
"""
from sqlalchemy import text
from sqlalchemy.engine import Engine

SPEND_SUBQUERY = """COALESCE((
        SELECT SUM(mr.cost_estimate) FROM model_run mr
        WHERE mr.org_id = o.id), 0)"""

# 真实调用(is_mock=false)已成功或明确失败却没记到成本 → 预算不可核算
GAP_SUBQUERY = """NOT EXISTS (
        SELECT 1 FROM model_run mr2
        WHERE mr2.org_id = o.id
          AND mr2.is_mock = false
          AND mr2.cost_estimate IS NULL
          AND mr2.status IN ('succeeded', 'failed'))"""


class BudgetExceededError(Exception):
    """机构费用预算已耗尽:不得再发出外部调用。"""


class BudgetUnaccountedError(Exception):
    """设置了预算但成本不可核算(Provider 无价格口径,或存在未知成本的真实调用)。"""


def reserved_view(eng: Engine, org_id: int) -> dict:
    """诊断视图:当前额度占用(测试与审计用)。"""
    with eng.connect() as conn:
        row = conn.execute(
            text(
                f"SELECT o.budget_cents, o.reserved_cents, {SPEND_SUBQUERY} AS spent,"
                " EXISTS (SELECT 1 FROM model_run mr2 WHERE mr2.org_id = o.id"
                "   AND mr2.is_mock = false AND mr2.cost_estimate IS NULL"
                "   AND mr2.status IN ('succeeded','failed')) AS has_gap"
                " FROM organization o WHERE o.id = :org"
            ),
            {"org": org_id},
        ).fetchone()
    if row is None:
        raise KeyError(f"机构不存在: {org_id}")
    return {
        "budget_cents": row[0],
        "reserved_cents": float(row[1]),
        "spent_cents": float(row[2]),
        "accounting_gap": row[3],
    }


def reserve(eng: Engine, org_id: int, est_cents: float | None, *, is_mock: bool) -> float:
    """调用发出前预留。返回实际预留额(调用方须在结算时原样归还)。

    预留与额度检查在同一条 UPDATE 的 WHERE 中完成,并发下由行锁串行,
    不会出现两个 worker 同时通过检查再各自扣减的竞态。
    """
    if is_mock:
        return 0.0  # mock 不产生真实费用:既不占额度,也不受预算限制
    held = 0.0 if est_cents is None else max(float(est_cents), 0.0)
    sql = text(
        f"""
        UPDATE organization o
           SET reserved_cents = o.reserved_cents + :est
         WHERE o.id = :org
           AND (o.budget_cents IS NULL
                OR ({GAP_SUBQUERY}
                    AND {SPEND_SUBQUERY} + o.reserved_cents + :est <= o.budget_cents))
        """
    )
    with eng.begin() as conn:
        n = conn.execute(sql, {"org": org_id, "est": held}).rowcount
    if n:
        return held

    view = reserved_view(eng, org_id)
    if view["accounting_gap"]:
        raise BudgetUnaccountedError(
            f"机构 {org_id} 设置了预算 {view['budget_cents']} 分,"
            "但存在成本未知(NULL)的真实调用,预算不可核算;拒绝继续调用。"
        )
    if est_cents is None:
        raise BudgetUnaccountedError(
            f"机构 {org_id} 设置了预算 {view['budget_cents']} 分,"
            "但该 Provider 无价格口径(预估成本未知),无法在执行前限制开销。"
        )
    raise BudgetExceededError(
        f"机构 {org_id} 预算不足:已用 {view['spent_cents']} + 在途 "
        f"{view['reserved_cents']} + 本次预估 {held} > 预算 {view['budget_cents']}"
    )


def settle(eng: Engine, org_id: int, held_cents: float) -> None:
    """归还预留。实际成本此刻已由 model_run 承载(succeeded 按实际、
    unknown 按预估保守计入),因此不会重复计费;明确失败则归还且不计成本。"""
    if not held_cents:
        return
    with eng.begin() as conn:
        conn.execute(
            text(
                "UPDATE organization SET reserved_cents = reserved_cents - :held"
                " WHERE id = :org"
            ),
            {"org": org_id, "held": held_cents},
        )
