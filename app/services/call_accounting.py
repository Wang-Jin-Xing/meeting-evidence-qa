"""外部模型调用的唯一执行点:预算预留 → 发出调用 → 调用结果与成本三态记账。

worker 四个阶段与问答(HTTP 请求内)都经此处,保证:
1. 任何一次真实调用**发出之前**已完成预算预留(见 app/services/budget.py),
   长任务不会在领取后突破预算;
2. model_run 行 = 一次物理调用的事实:前置 unknown 行(调用意图),
   返回后按 succeeded / failed / 保留 unknown 收尾;
3. 成本口径三态(estimated / provider_reported / unknown),
   cost_estimate 为 NULL 表示未知而非 0;结果未知的真实调用按预估**保守计为已用**
   (与"不宣称避免重复计费"一致:可能已计费的调用不能从账面消失)。
"""
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.ai.providers import ProviderResultUnknownError
from app.models.enums import CostBasis
from app.services import budget


@dataclass
class CallRecord:
    """一次调用的记账结果(测试与审计可直接读回)。"""
    run_id: int
    status: str
    cost_basis: str
    cost_cents: float | None


@dataclass
class Billed:
    """billed_call 的返回:Provider 结果 + 本次调用的记账事实。"""
    result: object
    record: CallRecord


def _begin_run(eng: Engine, *, org_id: int, job_id: int | None, stage: str,
               provider, call: str, input_hash: str,
               prompt_version: str | None) -> int:
    """前置记录:status=unknown(调用已发出与否的唯一意图留痕),成本未知。"""
    idem = f"org{org_id}-job{job_id}-{stage}-{input_hash}"
    with eng.begin() as conn:
        return conn.execute(
            text(
                "INSERT INTO model_run"
                " (org_id, job_id, stage, provider, model_name, prompt_version,"
                "  input_hash, status, is_mock, idempotency_key, idempotency_key_sent,"
                "  cost_basis)"
                " VALUES (:org, :job, :stage, :provider, :model, :pv, :ih,"
                "  'unknown', :mock, :idem, false, 'unknown')"
                " RETURNING id"
            ),
            {
                "org": org_id, "job": job_id, "stage": stage,
                "provider": provider.name, "model": provider.model_name,
                "pv": prompt_version, "ih": input_hash,
                "mock": provider.is_mock, "idem": idem,
            },
        ).scalar()


def _to_json(obj) -> str | None:
    import json

    return json.dumps(obj, ensure_ascii=False) if obj is not None else None


def _resolve_cost(provider, call: str, units: dict, result, est: float | None):
    """成本三态判定:服务商用量 > 单价估算 > 未知(NULL)。"""
    usage = provider.usage_for(call, units, result)
    if usage is not None:
        if usage.cost_cents is not None:
            basis = usage.basis or CostBasis.provider_reported.value
            return basis, float(usage.cost_cents), usage.tokens_in, usage.tokens_out
        # 有用量无金额:有单价表则按估算,否则仍是未知
        if est is not None:
            return CostBasis.estimated.value, est, usage.tokens_in, usage.tokens_out
        return CostBasis.unknown.value, None, usage.tokens_in, usage.tokens_out
    if est is not None:
        return CostBasis.estimated.value, est, None, None
    return CostBasis.unknown.value, None, None, None


def _finish_succeeded(eng: Engine, run_id: int, output_ref, basis: str,
                      cents: float | None, tokens_in: int | None,
                      tokens_out: int | None) -> None:
    with eng.begin() as conn:
        conn.execute(
            text(
                "UPDATE model_run SET status = 'succeeded', output_ref = CAST(:out AS jsonb),"
                " cost_basis = :basis, cost_estimate = :cents,"
                " tokens_in = :ti, tokens_out = :to WHERE id = :id"
            ),
            {"out": _to_json(output_ref), "basis": basis, "cents": cents,
             "ti": tokens_in, "to": tokens_out, "id": run_id},
        )


def _finish_failed(eng: Engine, run_id: int) -> None:
    """明确失败:结果确定 → failed,成本不计(未获得任何输出)。"""
    with eng.begin() as conn:
        conn.execute(
            text("UPDATE model_run SET status = 'failed' WHERE id = :id"),
            {"id": run_id},
        )


def _finish_unknown_charged(eng: Engine, run_id: int, est: float | None) -> None:
    """结果未知:status 保留 unknown;真实调用按预估保守计为已用(可能已计费)。"""
    if est is None:
        return  # 无价格口径:成本仍是 NULL(未知),由预算缺口规则拦截后续调用
    basis = CostBasis.estimated.value
    with eng.begin() as conn:
        conn.execute(
            text("UPDATE model_run SET cost_basis = :basis, cost_estimate = :cents"
                 " WHERE id = :id"),
            {"basis": basis, "cents": est, "id": run_id},
        )


def billed_call(eng: Engine, *, provider, org_id: int, job_id: int | None,
                stage: str, call: str, units: dict, input_hash: str,
                invoke: Callable[[], object],
                to_ref: Callable[[object], object] | None = None,
                prompt_version: str | None = None) -> Billed:
    """执行一次外部模型调用并完整记账,返回 Billed(Provider 结果, 记账事实)。

    invoke 负责真正的调用(调用方已施加超时/期限包装);本函数负责预算、
    model_run 留痕与成本口径,不吞异常:异常原样上抛,交由阶段重试路径处理。
    预算不足/不可核算时在**发出调用之前**抛出,不产生 model_run 行。
    """
    est = provider.estimate_cost(call, units)
    held = budget.reserve(eng, org_id, est, is_mock=provider.is_mock)
    run_id = _begin_run(eng, org_id=org_id, job_id=job_id, stage=stage,
                        provider=provider, call=call, input_hash=input_hash,
                        prompt_version=prompt_version)
    try:
        result = invoke()
    except ProviderResultUnknownError:
        _finish_unknown_charged(eng, run_id, est)
        budget.settle(eng, org_id, held)
        raise
    except Exception:
        _finish_failed(eng, run_id)
        budget.settle(eng, org_id, held)
        raise

    basis, cents, tokens_in, tokens_out = _resolve_cost(provider, call, units, result, est)
    _finish_succeeded(
        eng, run_id, to_ref(result) if to_ref else None, basis, cents,
        tokens_in, tokens_out,
    )
    budget.settle(eng, org_id, held)
    return Billed(result=result, record=CallRecord(
        run_id=run_id, status="succeeded", cost_basis=basis, cost_cents=cents,
    ))
