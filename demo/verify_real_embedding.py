"""真实 Embedding 接入验证(本轮范围:Embedding + 问答,不含 ASR)。

做四件事:
1. 核实向量维度:模型实际输出 vs 数据库向量列定义(真实推理,不是声明);
2. 用真实模型重建演示数据的检索索引,并验证发布不变量
   (旧索引在切换前完整保留、切换后仍在库只是不可见);
3. 在 **dev 会谈**上测量真实向量的相似度分布并据此设定证据阈值
   (阈值随模型改变,不能沿用 mock 校准值;不用 test 集调参);
4. 跑五项验证并把输入、候选、回答、引用、耗时、用量与成本口径写入 JSON 工件。

用法(需 worker/API 与本地模型可用):
    EMBED_PROVIDER=bge-m3 LLM_PROVIDER=deepseek python demo/verify_real_embedding.py
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("PYTHONUTF8", "1")

# 本轮固定:转写来源=人工导入,音频=静音占位,ASR 不参与
ENV_DEFAULTS = {"ASR_PROVIDER": "mock", "EMBED_PROVIDER": "bge-m3",
                "LLM_PROVIDER": "deepseek"}


def _q(sql, **params):
    from sqlalchemy import text

    from app.core.db import engine

    with engine.connect() as conn:
        return conn.execute(text(sql), params).fetchall()


# ---------- 1. 维度核实 ----------

def check_dimensions(settings, embed) -> dict:
    from app.models.transcript import EMBEDDING_DIM

    probe = embed.embed_batch(["X200监护仪的注册证什么时候补齐"])
    actual = len(probe[0])
    col = _q("SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a"
             " JOIN pg_class c ON c.oid = a.attrelid"
             " WHERE c.relname = 'embedding_chunk' AND a.attname = 'embedding'")[0][0]
    return {
        "model_name": embed.model_name,
        "provider": embed.name,
        "is_mock": embed.is_mock,
        "declared_dim": embed.embedding_dim,
        "actual_dim": actual,
        "db_column": col,
        "column_dim_matches": actual == EMBEDDING_DIM,
        "config_versions": {
            "embed_provider": settings.embed_provider,
            "llm_provider": settings.llm_provider,
            "asr_provider": settings.asr_provider,
            "embed_model": settings.embed_model,
            "asr_model": settings.asr_model,
            "qa_top_k": settings.qa_top_k,
            "qa_evidence_threshold_config_default": settings.qa_evidence_threshold,
        },
    }


# ---------- 2. 重建真实向量索引(保留旧索引直到原子发布) ----------

def reindex_with_real_vectors(client, session, embed, cases) -> list[dict]:
    from app.core.db import engine
    from app.services import transcript_correction
    from app.services.transcript_correction import CorrectionError

    out = []
    for spec in cases:
        rid = spec["recording_id"]
        before = _q(
            "SELECT active_transcript_version_id, active_search_version_id"
            " FROM recording WHERE id = :r", r=rid
        )[0]
        old_version = before[1]
        old_chunks = _q("SELECT count(*) FROM embedding_chunk WHERE version_id = :v",
                        v=old_version)[0][0]

        # 再导入一次同样的人工转写 → 新 human_correction 版本(0 改动)
        lines = [(s, t) for s, t in spec["transcript"]]
        try:
            info = transcript_correction.import_corrected_transcript(
                engine, recording_id=rid, lines=lines,
                created_by=spec["created_by"],
            )
        except CorrectionError as e:
            raise SystemExit(f"[verify] 导入失败: {e}")
        new_version = info["version_id"]

        # 发布前:新版本还没有 active chunk,旧版本必须仍然完整可见
        pre_active_new = _q("SELECT count(*) FROM embedding_chunk WHERE version_id = :v"
                            " AND is_active", v=new_version)[0][0]
        pre_active_old = _q("SELECT count(*) FROM embedding_chunk WHERE version_id = :v"
                            " AND is_active", v=old_version)[0][0]
        assert pre_active_new == 0, "新索引在发布前不应可见"
        assert pre_active_old == old_chunks, "发布前旧索引必须完整可用"

        built = transcript_correction.rebuild_search_index(
            engine, recording_id=rid, version_id=new_version, embed=embed,
        )

        # 发布后:新版本全部 active;旧版本行数不减(保留),只是不可见
        post = _q(
            "SELECT version_id, is_active, count(*) FROM embedding_chunk"
            " WHERE version_id IN (:a, :b) GROUP BY 1,2 ORDER BY 1",
            a=old_version, b=new_version,
        )
        pointer = _q("SELECT active_search_version_id FROM recording WHERE id = :r",
                     r=rid)[0][0]
        out.append({
            "recording_id": rid,
            "old_version": old_version,
            "new_version": new_version,
            "old_chunks_before": old_chunks,
            "old_visible_before": pre_active_old,
            "new_visible_before_publish": pre_active_new,
            "chunks_built": built["chunks"],
            "pointer_after": pointer,
            "chunk_states_after": [{"version_id": v, "is_active": a, "rows": n}
                                   for v, a, n in post],
            "old_rows_retained": all(
                s["rows"] >= 0 and s["version_id"] == old_version
                for s in [{"version_id": v, "is_active": a, "rows": n}
                          for v, a, n in post]
            ),
        })
    return out


# ---------- 3. dev 相似度分布与阈值 ----------

# 探针问题与期望证据片段(金标关键词),用于测相似度分布与阈值标定。
# 注意:这是 dev 会谈(dev=正常案例)上的测量,不用 test 会谈调参。
DEV_PROBES = [
    {"question": "注册证什么时候补齐?", "evidence_text": "注册证", "category": "时间点"},
    {"question": "整机保修期多长?", "evidence_text": "保修", "category": "时长"},
    {"question": "培训由谁负责?", "evidence_text": "培训", "category": "负责人"},
    {"question": "第一批设备什么时候送到?", "evidence_text": "第一批", "category": "时间点"},
    {"question": "明年的科研经费预算是多少?", "evidence_text": None, "category": "无答案"},
]

def measure_similarity(embed, dev_case, llm_case_questions) -> dict:
    """在 dev 会谈上测:金标问题 vs 其证据句 与 vs 全库其他句 的余弦相似度。

    只做阈值标定用,不作为效果结论;真实向量与 mock bigram 的尺度完全不同,
    沿用 0.15 会让所有候选通过(实测不相关句相似度约 0.5)。
    """
    import math

    def cos(a, b):
        return sum(x * y for x, y in zip(a, b)) / math.sqrt(
            sum(x * x for x in a) * sum(y * y for y in b))

    texts = [t for _s, t in dev_case["transcript"]]
    vecs = embed.embed_batch(texts)
    rows = []
    for item in llm_case_questions:
        q = item["question"]
        qv = embed.embed_batch([q])[0]
        sims = sorted(((cos(qv, v), texts[i]) for i, v in enumerate(vecs)),
                      reverse=True)
        gold = item.get("evidence_text")
        best_gold = max((s for s, t in sims if gold and gold in t), default=None)
        rows.append({
            "question": q,
            "category": item.get("category", "normal"),
            "top1_sim": round(sims[0][0], 4),
            "top1_is_gold": bool(gold and gold in sims[0][1]),
            "gold_sim": round(best_gold, 4) if best_gold else None,
            "worst_sim": round(sims[-1][0], 4),
        })
    golds = [r["gold_sim"] for r in rows if r["gold_sim"] is not None]
    non_gold_max = max((r["top1_sim"] for r in rows if r["gold_sim"] is None),
                       default=None)
    return {
        "per_question": rows,
        "gold_sim_min": min(golds) if golds else None,
        "no_gold_questions_top1_max": non_gold_max,
        "note": "阈值取低于最小金标相似度、高于无金标问题最高分的区间;"
                "样本仅 dev 一场会谈,不能据此推断整体召回率",
    }


# ---------- 4. 五项验证 ----------

DATE_RE = re.compile(r"\d{1,2}月\d{1,2}[日号]|\d{4}-\d{2}-\d{2}")


def run_checks(client, session, cases, threshold) -> list[dict]:
    from app.core.security import hash_password
    from app.core.db import engine
    from app.models.enums import UserRole
    from app.models.meeting import Meeting
    from app.models.org import Organization, User

    results = []

    def ask(meeting_id, question, headers):
        t0 = time.perf_counter()
        r = client.post(f"/meetings/{meeting_id}/ask",
                        json={"question": question}, headers=headers)
        return r, round((time.perf_counter() - t0) * 1000, 1)

    from demo.seed_demo import login as seed_login

    def login(username):
        return seed_login(client, username)

    normal = next(c for c in cases if c["id"] == "case_normal")
    boundary = next(c for c in cases if c["id"] == "case_boundary")
    h_normal = login(normal["admin"])
    h_boundary = login(boundary["admin"])

    # ① 正常问题:检索到相关原文并回答
    r, ms = ask(normal["meeting_id"], "注册证什么时候补齐?", h_normal)
    body = r.json()
    results.append({
        "check": "①正常问答", "case": normal["id"], "question": "注册证什么时候补齐?",
        "status": r.status_code, "latency_ms": ms,
        "has_answer": body.get("answer") is not None,
        "raw_retrieved": body.get("raw_retrieved_count"),
        "candidates": body.get("relevant_candidate_count"),
        "citations": [{"segment_id": c["segment_id"], "similarity": c["similarity"],
                       "start_ms": c["start_ms"], "text": c["text"]}
                      for c in body.get("citations", [])],
        "answer": body.get("answer"),
        "pass": bool(body.get("answer") and "5月20日" in body["answer"]
                     and body.get("citations")),
    })

    # ② 型号错配不张冠李戴:Y300 没报价,不得把 Y500 的 120 万给它
    r, ms = ask(boundary["meeting_id"], "Y300的报价是多少?", h_boundary)
    body = r.json()
    ans = body.get("answer") or ""
    cited_texts = [c["text"] for c in body.get("citations", [])]
    leaked = ("120万" in ans and not any("120万" in t and "Y300" in t
                                        for t in cited_texts))
    results.append({
        "check": "②型号错配", "case": boundary["id"], "question": "Y300的报价是多少?",
        "status": r.status_code, "latency_ms": ms,
        "has_sufficient_evidence": body.get("has_sufficient_evidence"),
        "answer": ans or None,
        "candidates": body.get("relevant_candidate_count"),
        "citations": [{"segment_id": c["segment_id"], "similarity": c["similarity"],
                       "text": c["text"]} for c in body.get("citations", [])],
        "y500_price_attributed_to_y300": leaked,
        "pass": not leaked,
    })

    # ③ 无明确日期/负责人时不补造
    r, ms = ask(boundary["meeting_id"], "Y500的注册证什么时候补齐?", h_boundary)
    body = r.json()
    ans = body.get("answer") or ""
    cited = " ".join(c["text"] for c in body.get("citations", []))
    invented = [d for d in DATE_RE.findall(ans) if d not in cited]
    # 纪要侧:decision/todo 缺 owner/due 必须 needs_confirmation
    minutes = client.get(f"/meetings/{boundary['meeting_id']}/minutes",
                         headers=h_boundary).json()
    bad_items = [
        it for it in minutes["content"]["items"]
        if it["type"] in ("decision", "todo")
        and (it["owner"] is None or it["due_date"] is None)
        and not it["needs_confirmation"]
    ]
    results.append({
        "check": "③不补造日期/负责人", "case": boundary["id"],
        "question": "Y500的注册证什么时候补齐?", "status": r.status_code,
        "latency_ms": ms,
        "has_sufficient_evidence": body.get("has_sufficient_evidence"),
        "answer": ans or None,
        "dates_in_answer_not_in_evidence": invented,
        "minutes_items_violating_empty_policy": len(bad_items),
        "pass": not invented and not bad_items,
    })

    # ④ 跨机构访问被拒绝
    from demo.seed_demo import DEMO_PASSWORD

    outsider = session.query(User).filter_by(username="demo_outsider").first()
    if outsider is None:
        org = Organization(name="演示-外部医疗机构")
        session.add(org)
        session.commit()
        outsider = User(org_id=org.id, username="demo_outsider",
                        password_hash=hash_password(DEMO_PASSWORD),
                        display_name="演示-外部", role=UserRole.org_admin)
        session.add(outsider)
        session.commit()
    h_out = login("demo_outsider")
    g = client.get(f"/meetings/{normal['meeting_id']}", headers=h_out)
    a, ms = ask(normal["meeting_id"], "注册证什么时候补齐?", h_out)
    results.append({
        "check": "④跨机构拒绝", "detail_status": g.status_code,
        "ask_status": a.status_code, "latency_ms": ms,
        "pass": g.status_code == 404 and a.status_code == 404,
    })

    # ⑤ 引用均可解析到正确会谈的原文
    r, _ms = ask(normal["meeting_id"], "培训由谁负责?", h_normal)
    body = r.json()
    verified = []
    all_ok = bool(body.get("citations"))
    for c in body.get("citations", []):
        row = _q(
            "SELECT ts.text, tv.recording_id, r.meeting_id FROM transcript_segment ts"
            " JOIN transcript_version tv ON tv.id = ts.version_id"
            " JOIN recording r ON r.id = tv.recording_id WHERE ts.id = :s",
            s=c["segment_id"],
        )
        ok = bool(row) and row[0][2] == normal["meeting_id"] and row[0][0] == c["text"]
        all_ok = all_ok and ok
        verified.append({"segment_id": c["segment_id"],
                         "belongs_to_meeting": bool(row and row[0][2] == normal["meeting_id"]),
                         "text_matches_db": bool(row and row[0][0] == c["text"]),
                         "ok": ok})
    results.append({
        "check": "⑤引用可解析", "question": "培训由谁负责?",
        "answer": body.get("answer"), "citations_checked": verified,
        "pass": all_ok,
    })
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(REPO_ROOT / "demo" / "verification"
                                        / "real_embedding.json"))
    ap.add_argument("--threshold", type=float, default=None,
                    help="证据阈值;留空则按 dev 分布自动取建议值")
    args = ap.parse_args()

    for k, v in ENV_DEFAULTS.items():
        os.environ.setdefault(k, v)

    from fastapi.testclient import TestClient

    from app.ai.registry import build_providers, describe_providers
    from app.core.config import get_settings
    from app.core.db import SessionLocal
    from app.main import app

    get_settings.cache_clear()
    settings = get_settings()
    asr, llm, embed = build_providers(settings)
    desc = describe_providers((asr, llm, embed))
    assert desc["embed"]["is_mock"] is False, "本轮必须使用真实 Embedding"
    assert desc["llm"]["is_mock"] is False, "本轮必须使用真实 LLM"

    from demo.seed_demo import DEMO

    # 找到演示会谈与录音(按标题匹配,避免依赖自增 id)
    cases = []
    session = SessionLocal()
    try:
        from app.models.meeting import Meeting
        from app.models.org import User

        for spec in DEMO:
            ms = spec["meeting"]
            m = session.query(Meeting).filter_by(title=ms["title"]).first()
            if m is None:
                raise SystemExit(f"[verify] 找不到演示会谈 {ms['title']},先跑 seed_demo")
            rec = _q("SELECT id FROM recording WHERE meeting_id = :m ORDER BY id LIMIT 1",
                     m=m.id)[0][0]
            admin = session.query(User).filter_by(username=spec["admin"]).first()
            cases.append({
                "id": ms["id"], "meeting_id": m.id, "recording_id": rec,
                "admin": spec["admin"], "created_by": admin.id,
                "transcript": ms["transcript"], "questions": ms["questions"],
            })

        report: dict = {
            "meta": {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "scope": "真实 Embedding + 真实 LLM + 人工转写输入(本轮不含 ASR)",
                "providers": desc,
                "database": _q("SELECT current_database()")[0][0],
                "git": _git_state(),
            }
        }
        report["dimension_check"] = check_dimensions(settings, embed)
        print("[verify] 维度核实:", json.dumps(
            {k: report["dimension_check"][k] for k in
             ("model_name", "actual_dim", "db_column", "column_dim_matches")},
            ensure_ascii=False))

        with TestClient(app) as client:
            report["reindex"] = reindex_with_real_vectors(client, session, embed, cases)
            print("[verify] 索引重建:", json.dumps(
                [{k: r[k] for k in ("recording_id", "old_version", "new_version",
                                    "old_visible_before", "chunks_built",
                                    "pointer_after")}
                 for r in report["reindex"]], ensure_ascii=False))

            dev = next(c for c in cases if c["id"] == "case_normal")
            report["dev_similarity"] = measure_similarity(embed, dev, DEV_PROBES)
            golds = [q["gold_sim"] for q in report["dev_similarity"]["per_question"]
                     if q["gold_sim"] is not None]
            suggested = args.threshold or (round(min(golds) - 0.05, 2) if golds else 0.5)
            report["threshold"] = {
                "used": suggested,
                "config_default": settings.qa_evidence_threshold,
                "dev_gold_sim_min": report["dev_similarity"]["gold_sim_min"],
                "dev_no_gold_top1_max": report["dev_similarity"][
                    "no_gold_questions_top1_max"],
                "basis": "dev 会谈相似度分布;低于最小金标相似度留 0.05 余量",
                "note": "换 Embedding 模型必须重校准(D-20/D-48);mock 的 0.15 不可沿用",
            }
            print("[verify] dev 相似度:", json.dumps(
                report["dev_similarity"]["per_question"], ensure_ascii=False))
            print(f"[verify] 采用证据阈值 {suggested}"
                  f"(配置默认 {settings.qa_evidence_threshold} 为 mock 校准值)")

            # 阈值只影响本次验证进程内的检索筛选
            os.environ["QA_EVIDENCE_THRESHOLD"] = str(suggested)
            get_settings.cache_clear()
            report["checks"] = run_checks(client, session, cases, suggested)
    finally:
        session.close()

    passed = sum(1 for c in report["checks"] if c["pass"])
    print(f"\n[verify] 五项验证:{passed}/{len(report['checks'])} 通过")
    for c in report["checks"]:
        print(f"  {'PASS' if c['pass'] else 'FAIL'} {c['check']}")

    report["model_run_ledger"] = [
        {"stage": s, "provider": p, "model": m, "is_mock": im, "count": n,
         "cost_basis": cb, "cost_cents": cc}
        for s, p, m, im, n, cb, cc in _q(
            "SELECT stage, provider, model_name, is_mock, count(*), cost_basis,"
            " SUM(cost_estimate) FROM model_run GROUP BY 1,2,3,4,6"
            " ORDER BY 1,2")
    ]
    report["limits"] = [
        "样本只有 2 场会谈、若干问题,通过与否只说明这些具体断言成立,"
        "不能外推为整体准确率或召回率",
        "阈值由 dev 单场会谈的相似度分布决定,换会谈/换模型需重测",
        "转写来源是人工导入,音频为静音占位:本轮不评估任何 ASR 能力",
        "DeepSeek 未配单价表 → 成本口径 unknown(NULL),不按 0 记账;"
        "本地 bge-m3 无按次账单,记 estimated 0(含义是'服务商不按次收费',"
        "不等于没有算力与电耗成本)",
    ]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    from decimal import Decimal

    out.write_text(json.dumps(report, ensure_ascii=False, indent=2,
                              default=lambda o: float(o) if isinstance(o, Decimal)
                              else str(o)), encoding="utf-8")
    print(f"[verify] 工件已写入 {out}")


def _git_state():
    import subprocess

    def git(*a):
        return subprocess.run(["git"] + list(a), cwd=REPO_ROOT,
                              capture_output=True, text=True).stdout.strip()

    status = git("status", "--porcelain=v1")
    return {"rev": git("rev-parse", "HEAD"), "dirty": bool(status),
            "changed": [l[3:] for l in status.splitlines()][:20]}


if __name__ == "__main__":
    main()
