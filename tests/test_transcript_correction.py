"""人工校对转写导入的证据链不变量(版本 / 映射 / 发布 / 历史引用)。

对应设计约定"纠错新增版本,旧引用仍能解析":校对是**新增版本**,
绝不改写原版本片段;检索按新的发布指针走,历史纪要引用按旧片段解析。
"""
import pytest
from sqlalchemy import text

from app.ai.registry import build_providers
from app.core.db import engine
from app.services import transcript_correction
from app.services.transcript_correction import CorrectionError
from app.worker import stages
from tests.conftest import auth_headers, claim_and_load, upload_recording

ORIGINALS = [
    "今天主要讨论X200监护仪的采购和交付安排",
    "X200的注册证和检测报告我们会在5月20日之前补齐",
    "X300型号的保修期是多久?和X200有区别吗",
]
CORRECTED = [
    ORIGINALS[0],
    "X200的注册证与检测报告,我们会在5月20日之前补齐。",  # 唯一改动句
    ORIGINALS[2],
]


def _complete_pipeline(client, session, org, user):
    """跑完四阶段(进程内),返回 (meeting, job_id, recording_id, embed)。"""
    meeting, job_id = upload_recording(client, session, org, user)
    asr, llm, embed = build_providers()
    epoch, ctx = claim_and_load(job_id)
    stages.run_prepare(engine, ctx, epoch, 10, lambda: False)
    stages.run_transcribe(engine, ctx, epoch, asr, lambda: False)
    stages.run_minutes(engine, ctx, epoch, llm, lambda: False)
    stages.run_index(engine, ctx, epoch, embed, lambda: False)
    return meeting, job_id, ctx.recording_id, embed


def _q(sql, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).fetchall()


def _import(recording_id, user_id, lines=None):
    """只改文本时不指定说话人:沿用原片段发言人。"""
    return transcript_correction.import_corrected_transcript(
        engine, recording_id=recording_id,
        lines=[(None, text) for text in (lines or CORRECTED)],
        created_by=user_id,
    )


def test_correction_adds_version_without_touching_original(client, session, two_orgs):
    meeting, job_id, rid, _embed = _complete_pipeline(
        client, session, two_orgs["org_a"], two_orgs["a_admin"]
    )
    v1 = _q("SELECT active_transcript_version_id FROM recording WHERE id=:r", r=rid)[0][0]

    info = _import(rid, two_orgs["a_admin"].id)

    assert info["segments"] == 3 and info["edited"] == 1 and info["unchanged"] == 2
    # 原版本仍在,文本未被改写(片段不可变)
    old_texts = [r[0] for r in _q(
        "SELECT text FROM transcript_segment WHERE version_id=:v ORDER BY seq", v=v1)]
    assert old_texts == ORIGINALS
    # 新版本:human_correction + 指向被纠错版本 + 记录纠错人
    row = _q(
        "SELECT version_no, source_type, correction_of_version_id, created_by"
        " FROM transcript_version WHERE id=:v", v=info["version_id"])[0]
    assert (row[1], row[2], row[3]) == ("human_correction", v1, two_orgs["a_admin"].id)
    new_texts = [r[0] for r in _q(
        "SELECT text FROM transcript_segment WHERE version_id=:v ORDER BY seq",
        v=info["version_id"])]
    assert new_texts == CORRECTED
    # 转写指针已切到新版本;时间戳沿用切片边界(未因校对而改变)
    assert _q("SELECT active_transcript_version_id FROM recording WHERE id=:r",
              r=rid)[0][0] == info["version_id"]
    pairs = _q("SELECT start_ms, end_ms, seq FROM transcript_segment"
               " WHERE version_id=:v ORDER BY seq", v=info["version_id"])
    assert pairs == _q("SELECT start_ms, end_ms, seq FROM transcript_segment"
                       " WHERE version_id=:v ORDER BY seq", v=v1)
    # 逐条映射:改动句 text_edited,其余 unchanged
    mapping = dict(_q(
        "SELECT old_seq, change_type FROM segment_mapping"
        " WHERE new_version_id=:v", v=info["version_id"]))
    assert mapping == {1: "unchanged", 2: "text_edited", 3: "unchanged"}, mapping


def test_history_minutes_references_still_resolve_after_correction(client, session, two_orgs):
    """纠错后纪要仍指向旧版本片段,且必须仍能解析出原文(证据链不断)。"""
    meeting, job_id, rid, _embed = _complete_pipeline(
        client, session, two_orgs["org_a"], two_orgs["a_admin"]
    )
    headers = auth_headers(client, two_orgs["a_admin"].username)
    before = client.get(f"/meetings/{meeting.id}/minutes", headers=headers).json()
    cited_before = {
        e["segment_id"] for item in before["content"]["items"]
        for e in item["evidence"]
    }
    assert cited_before, "纪要条目应带可解析的证据"

    _import(rid, two_orgs["a_admin"].id)

    after = client.get(f"/meetings/{meeting.id}/minutes", headers=headers).json()
    assert after["version_no"] == before["version_no"], "纪要版本不因转写纠错而改变"
    cited_after = {
        e["segment_id"] for item in after["content"]["items"]
        for e in item["evidence"]
    }
    assert cited_after == cited_before
    # 引用的仍是旧版本原文(未被校对文本覆盖)
    ids = ",".join(str(int(i)) for i in cited_after)
    rows = _q(
        f"SELECT id, text FROM transcript_segment WHERE id IN ({ids})"
    )
    texts = dict(rows)
    assert texts and all(t in ORIGINALS for t in texts.values()), texts


def test_search_switches_to_corrected_version_atomically(client, session, two_orgs):
    """重建索引后:检索命中校对文本,旧版本 chunk 不再可见。"""
    meeting, job_id, rid, embed = _complete_pipeline(
        client, session, two_orgs["org_a"], two_orgs["a_admin"]
    )
    v1 = _q("SELECT active_transcript_version_id FROM recording WHERE id=:r", r=rid)[0][0]
    info = _import(rid, two_orgs["a_admin"].id)
    v2 = info["version_id"]

    # 校对版本先以 is_active=false 建 chunk,发布前检索仍走旧版本
    built = transcript_correction.rebuild_search_index(
        engine, recording_id=rid, version_id=v2, embed=embed
    )
    assert built["chunks"] == 3
    assert _q("SELECT count(*) FROM embedding_chunk WHERE version_id=:v"
              " AND is_active", v=v2)[0][0] == 3
    assert _q("SELECT count(*) FROM embedding_chunk WHERE version_id=:v"
              " AND is_active", v=v1)[0][0] == 0
    assert _q("SELECT active_search_version_id FROM recording WHERE id=:r",
              r=rid)[0][0] == v2

    headers = auth_headers(client, two_orgs["a_admin"].username)
    resp = client.post(
        f"/meetings/{meeting.id}/ask",
        json={"question": "X200的注册证什么时候补齐?"}, headers=headers,
    ).json()
    cited_texts = [c["text"] for c in resp["citations"]]
    assert resp["has_sufficient_evidence"] is True
    assert CORRECTED[1] in cited_texts, cited_texts
    assert ORIGINALS[1] not in cited_texts, "旧版本内容不得再进检索"

    # 校对导入的模型调用同样记账(job_id 为空),成本口径 unknown
    # 离线导入的调用同样记账:job_id 为空(不属于任何流水线任务)
    runs = _q("SELECT status, cost_basis, cost_estimate FROM model_run"
              " WHERE stage='index' AND org_id=:o AND job_id IS NULL",
              o=two_orgs["org_a"].id)
    assert runs, "校对版本索引必须留下 model_run 留痕"
    assert {r[0] for r in runs} == {"succeeded"}
    assert {r[1] for r in runs} == {"unknown"} and all(r[2] is None for r in runs)


def test_wrong_line_count_rejected_without_publishing(client, session, two_orgs):
    """条数与原版不符即拒绝:不产生新版本,也不移动任何指针(时轴不可被校对改动)。"""
    meeting, job_id, rid, _embed = _complete_pipeline(
        client, session, two_orgs["org_a"], two_orgs["a_admin"]
    )
    versions_before = _q("SELECT count(*) FROM transcript_version WHERE recording_id=:r",
                         r=rid)[0][0]
    pointers = _q("SELECT active_transcript_version_id, active_search_version_id"
                  " FROM recording WHERE id=:r", r=rid)[0]

    with pytest.raises(CorrectionError) as ei:
        _import(rid, two_orgs["a_admin"].id, lines=["只有一句,条数与原版不符"])

    assert "不符" in str(ei.value)
    assert _q("SELECT count(*) FROM transcript_version WHERE recording_id=:r",
              r=rid)[0][0] == versions_before
    assert _q("SELECT active_transcript_version_id, active_search_version_id"
              " FROM recording WHERE id=:r", r=rid)[0] == pointers


def test_correction_requires_existing_transcript(client, session, two_orgs):
    """未转写的录音不能做人工校对(没有可映射的原版本)。"""
    meeting, job_id = upload_recording(client, session, two_orgs["org_a"],
                                       two_orgs["a_admin"])
    rid = _q("SELECT recording_id FROM processing_job WHERE id=:j", j=job_id)[0][0]
    with pytest.raises(CorrectionError):
        transcript_correction.import_corrected_transcript(
            engine, recording_id=rid,
            lines=[("甲", "无中生有的校对")], created_by=two_orgs["a_admin"].id,
        )
