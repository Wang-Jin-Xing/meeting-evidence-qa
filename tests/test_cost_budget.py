"""费用三态与"每次外部调用前预留预算"(D-38 / D-42)。

都在测试进程内直接驱动 stages/billed_call(不起 worker 子进程):
被验证的是记账与预算逻辑本身,子进程路径已由 e2e/恢复测试覆盖。

口径要点:
- mock 调用:is_mock=true、cost_basis=unknown、cost_estimate=NULL,
  既不占预算也不得被当作真实成本;
- cost_estimate 为 NULL 是"未知",不是 0:设置了预算的机构一旦出现
  成本未知的真实调用,预算即不可核算,宁可停住;
- 预算检查发生在**调用发出之前**,所以长任务不会在领取后突破预算。
"""
import pytest
from sqlalchemy import text

from app.ai.mock_providers import MockASR, MockEmbed
from app.ai.providers import (
    ASRSlice,
    ASRProvider,
    ProviderResultUnknownError,
    ProviderUsage,
)
from app.core.db import engine
from app.models.enums import CostBasis
from app.services import budget, call_accounting
from app.worker import stages
from app.worker.lease import claim_job, worker_id
from tests.conftest import claim_and_load, upload_recording


class PricedASR(ASRProvider):
    """按次计费的本地替身(声明 is_mock=false 以走真实成本路径)。

    它不是真实服务商,只用于验证"预估/用量/未知"三种记账与调用前预算约束。
    """

    name = "priced-asr"
    model_name = "priced-v1"
    is_mock = False

    def __init__(self, per_call_cents=10.0, reported_cents=None, unknown_on=None):
        self.per_call = per_call_cents
        self.reported = reported_cents
        self.unknown_on = unknown_on
        self.calls = 0

    def estimate_cost(self, call, units):
        return self.per_call

    def usage_for(self, call, units, result):
        if self.reported is None:
            return None
        return ProviderUsage(
            basis=CostBasis.provider_reported.value, cost_cents=self.reported,
            tokens_in=11, tokens_out=3,
        )

    def transcribe_slice(self, audio, slice_index):
        self.calls += 1
        if self.unknown_on == slice_index:
            raise ProviderResultUnknownError("[test] 注入:结果未知")
        return ASRSlice(speaker="甲", text=f"片段{slice_index}", is_mock=False)


_probe_job = upload_recording


def _run_transcribe(job_id, provider):
    epoch, ctx = claim_and_load(job_id)
    stages.run_prepare(engine, ctx, epoch, 10, lambda: False)
    stages.run_transcribe(
        engine, ctx, epoch, provider, lambda: False,
        provider_timeout=60.0, deadline=None,
    )
    return ctx


def _rows(job_id=None, org_id=None, stage=None):
    where, params = ["1=1"], {}
    if job_id is not None:
        where.append("job_id = :j"); params["j"] = job_id
    if org_id is not None:
        where.append("org_id = :o"); params["o"] = org_id
    if stage:
        where.append("stage = :s"); params["s"] = stage
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT status, cost_basis, cost_estimate FROM model_run"
                f" WHERE {' AND '.join(where)} ORDER BY id"
            ),
            params,
        ).fetchall()


def _set_budget(org_id, cents):
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE organization SET budget_cents = :c WHERE id = :o"),
            {"c": cents, "o": org_id},
        )


# ---------- 记账三态 ----------

def test_mock_call_cost_is_unknown_not_zero(client, session, two_orgs):
    """mock 调用:is_mock=true、成本 unknown 且为 NULL(不得记成 0)。"""
    org = two_orgs["org_a"]
    _set_budget(org.id, None)
    embed = MockEmbed()
    call_accounting.billed_call(
        engine, provider=embed, org_id=org.id, job_id=None, stage="probe",
        call="embed_batch", units={"texts": 1}, input_hash="probe-mock-1",
        invoke=lambda: embed.embed_batch(["甲"]),
    )
    row = _rows(org_id=org.id, stage="probe")[0]
    assert row == ("succeeded", "unknown", None), row
    with engine.connect() as conn:
        is_mock = conn.execute(
            text("SELECT is_mock FROM model_run WHERE org_id=:o AND stage='probe'"),
            {"o": org.id},
        ).scalar()
    assert is_mock is True
    # mock 不占预算:预留账本仍为 0
    assert budget.reserved_view(engine, org.id)["reserved_cents"] == 0


def test_succeeded_call_with_provider_usage_is_provider_reported(client, session, two_orgs):
    """服务商返回用量 → basis=provider_reported,金额取实际用量。"""
    org = two_orgs["org_a"]
    meeting, job_id = _probe_job(client, session, org, two_orgs["a_admin"])
    _set_budget(org.id, None)
    _run_transcribe(job_id, PricedASR(reported_cents=7.5))
    rows = _rows(job_id=job_id, stage="transcribe")
    assert [r[1] for r in rows] == ["provider_reported"] * 3, rows
    assert {float(r[2]) for r in rows} == {7.5}
    view = budget.reserved_view(engine, org.id)
    assert view["spent_cents"] == 22.5  # 3 片 × 7.5
    assert view["reserved_cents"] == 0  # 结算后无泄漏


def test_succeeded_call_without_usage_falls_back_to_estimated(client, session, two_orgs):
    """无用量但有单价 → estimated(估算口径,与服务商返回口径不得混记)。"""
    org = two_orgs["org_b"]
    meeting, job_id = _probe_job(client, session, org, two_orgs["b_admin"])
    _run_transcribe(job_id, PricedASR(per_call_cents=4.0))
    rows = _rows(job_id=job_id, stage="transcribe")
    assert {r[1] for r in rows} == {"estimated"}
    assert {float(r[2]) for r in rows} == {4.0}
    assert budget.reserved_view(engine, org.id)["spent_cents"] == 12.0


# ---------- 调用前预算约束 ----------

def test_budget_stops_long_task_before_the_third_call(client, session, two_orgs):
    """预算 25、每片 10:第 3 次调用在发出前即被拒(证明是调用级约束)。

    领取时历史费用为 0,任何"只在 claim 比较历史费用"的实现都拦不住这个任务;
    物理调用数停在 2 是这里的判定证据。
    """
    org = two_orgs["org_a"]
    meeting, job_id = _probe_job(client, session, org, two_orgs["a_admin"])
    _set_budget(org.id, 25)
    asr = PricedASR(per_call_cents=10.0)
    with pytest.raises(budget.BudgetExceededError):
        _run_transcribe(job_id, asr)
    assert asr.calls == 2, "第 3 次调用必须在发出前被拒"
    rows = _rows(job_id=job_id, stage="transcribe")
    assert len(rows) == 2, "被拒的调用不得留下 model_run 行"
    view = budget.reserved_view(engine, org.id)
    assert view["reserved_cents"] == 0, "拒绝时不得残留预留"
    assert view["spent_cents"] == 20.0


def test_mock_provider_ignores_budget_but_priced_one_does_not(client, session, two_orgs):
    """mock 不产生真实费用:预算 1 分仍可跑完整流水线;计费的替身第一次调用就被挡。"""
    org = two_orgs["org_a"]
    meeting, job_id = _probe_job(client, session, org, two_orgs["a_admin"])
    _set_budget(org.id, 1)
    _run_transcribe(job_id, MockASR())  # 不应抛错
    view = budget.reserved_view(engine, org.id)
    assert (view["spent_cents"], view["reserved_cents"]) == (0, 0)

    # 同机构换成有价格的 Provider:预算在执行前就生效,一次都不发
    _meeting2, job2 = _probe_job(client, session, org, two_orgs["a_admin"])
    epoch, ctx = claim_and_load(job2)
    stages.run_prepare(engine, ctx, epoch, 10, lambda: False)
    asr = PricedASR(per_call_cents=10.0)
    with pytest.raises(budget.BudgetExceededError):
        stages.run_transcribe(
            engine, ctx, epoch, asr, lambda: False,
            provider_timeout=60.0, deadline=None,
        )
    assert asr.calls == 0, "有预算约束时不得先调用再记账"


def test_unknown_outcome_is_charged_conservatively(client, session, two_orgs):
    """结果未知(超时/断连):保留 unknown 状态,但按预估计入已用额度。

    可能已计费的调用不能从账面上消失——这是"不宣称避免重复计费"的落地。
    """
    org = two_orgs["org_a"]
    meeting, job_id = _probe_job(client, session, org, two_orgs["a_admin"])
    _set_budget(org.id, None)
    asr = PricedASR(per_call_cents=10.0, unknown_on=0)
    with pytest.raises(ProviderResultUnknownError):
        _run_transcribe(job_id, asr)
    rows = _rows(job_id=job_id, stage="transcribe")
    assert rows[0][0] == "unknown", rows
    assert rows[0][1] == "estimated" and float(rows[0][2]) == 10.0, rows
    view = budget.reserved_view(engine, org.id)
    assert view["reserved_cents"] == 0, "预留已转为记账,不得双计"
    assert view["spent_cents"] == 10.0


def test_explicit_failure_releases_reservation_and_records_no_cost(client, session, two_orgs):
    """明确失败:归还预留、不记成本(与"未知按预估计"区分)。"""
    org = two_orgs["org_a"]
    meeting, job_id = _probe_job(client, session, org, two_orgs["a_admin"])
    _set_budget(org.id, None)

    class Boom(PricedASR):
        def transcribe_slice(self, audio, slice_index):
            self.calls += 1
            raise RuntimeError("[test] 注入:明确失败")

    asr = Boom(per_call_cents=10.0)
    with pytest.raises(RuntimeError):
        _run_transcribe(job_id, asr)
    rows = _rows(job_id=job_id, stage="transcribe")
    assert rows[0] == ("failed", "unknown", None), rows
    assert budget.reserved_view(engine, org.id)["reserved_cents"] == 0


# ---------- 预算不可核算(NULL 不当 0) ----------

def test_unpriced_real_call_blocks_claim(client, session, two_orgs):
    """设置了预算但存在成本未知的真实调用 → 不可核算,claim 侧拒绝领取。"""
    org = two_orgs["org_a"]
    meeting, job_id = _probe_job(client, session, org, two_orgs["a_admin"])
    _set_budget(org.id, 1000)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO model_run (org_id, stage, provider, model_name,"
                " input_hash, status, is_mock, idempotency_key_sent, cost_basis,"
                " cost_estimate)"
                " VALUES (:o, 'manual', 'fake', 'fake', 'unpriced-1', 'succeeded',"
                "  false, false, 'unknown', NULL)"
            ),
            {"o": org.id},
        )
    assert claim_job(engine, worker_id(), ttl_seconds=120) is None
    # 补齐成本后恢复可领取(证明确实是"不可核算"而非"已超额"在挡)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE model_run SET cost_estimate = 5, cost_basis = 'estimated'"
                 " WHERE org_id = :o AND input_hash = 'unpriced-1'"),
            {"o": org.id},
        )
    assert claim_job(engine, worker_id(), ttl_seconds=120) == job_id
