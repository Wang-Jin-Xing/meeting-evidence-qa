"""检索问答评测框架(P3 基线)。

用法(在仓库根目录):
    python eval/run_eval.py [--split dev|test|all] [--top-k N]
                            [--threshold F] [--out eval/reports/report.json]
                            [--transcript asr|human_corrected]

流程:按数据集准备会谈(自定义语料注入 mock ASR,子进程 worker 完成四阶段)
→ 逐问调用问答 API → 对照金标评分 → 输出报告 JSON。

诚实声明(评审约定):
- 当前仅 mock Provider:结果单列,只证明流程正确,
  不用于声称真实效果提升;
- 真实 Provider 接入后:先真实 Embedding + LLM(以人工校对转写评测问答,
  隔离 ASR 错误),再测真实 ASR 端到端;
- 真实向量维度须与 EMBEDDING_DIM=1024 核对,不同需迁移重建(D-03);
- 基线完成后,一次只调整一种检索策略,报告收益与代价。
"""
import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("PYTHONUTF8", "1")


def gen_wav_bytes(seconds: float, sample_rate: int = 8000) -> bytes:
    """静音 WAV(时长=语料句数×10s,切片边界即时间来源)。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\x00\x00" * int(sample_rate * seconds))
    return buf.getvalue()


def q(sql, **params):
    from sqlalchemy import text

    from app.core.db import engine

    with engine.connect() as conn:
        return conn.execute(text(sql), params).fetchall()


def _current_database() -> str | None:
    """评测会写入配置指向的库:如实记录是哪一个(测试库 or 应用库)。"""
    try:
        return q("SELECT current_database()")[0][0]
    except Exception:
        return None


def wait_job(job_id: int, statuses: set[str], timeout: float = 180) -> str:
    last = None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        last = q("SELECT status FROM processing_job WHERE id = :j", j=job_id)[0][0]
        if last in statuses:
            return last
        time.sleep(0.3)
    raise RuntimeError(f"评测任务等待 {statuses} 超时,最后状态: {last}")


def prepare_meeting(client, session, spec, workdir) -> dict:
    """建机构/用户/会谈并上传语料录音,起 worker 完成四阶段。"""
    from app.core.security import hash_password
    from app.models.enums import MeetingVisibility, UserRole
    from app.models.meeting import Meeting
    from app.models.org import Organization, User

    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    org = Organization(name=f"eval-{spec['id']}-{ts}")
    session.add(org)
    session.commit()
    user = User(
        org_id=org.id, username=f"eval-{spec['id']}-{ts}",
        password_hash=hash_password("eval-only"), display_name="评测用户",
        role=UserRole.org_admin,
    )
    session.add(user)
    session.commit()
    meeting = Meeting(
        org_id=org.id, title=spec["title"], vendor_name=spec["vendor_name"],
        topic=spec.get("topic"), scheduled_at=datetime(2026, 9, 1, 10, 0),
        # 可见性由数据集声明(restricted 才能构造越权反例)
        visibility=MeetingVisibility(spec.get("visibility", "org")),
        created_by=user.id,
    )
    session.add(meeting)
    session.commit()

    corpus_file = workdir / f"corpus-{spec['id']}.json"
    corpus_file.write_text(
        json.dumps(spec["corpus"], ensure_ascii=False), encoding="utf-8"
    )
    req_log = workdir / f"requests-{spec['id']}.jsonl"

    resp = client.post(
        "/auth/login", json={"username": user.username, "password": "eval-only"}
    )
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    resp = client.post(
        f"/meetings/{meeting.id}/recordings",
        files={
            "file": (
                f"eval-{spec['id']}.wav",
                gen_wav_bytes(len(spec["corpus"]) * 10),
                "audio/wav",
            )
        },
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]

    env = {
        **os.environ, "PYTHONUTF8": "1",
        "MEET_WORKER_POLL_SECONDS": "0.2",
        "MEET_WORKER_LEASE_TTL_SECONDS": "60",
        "MEET_WORKER_RETRY_BACKOFF_SECONDS": "0",
        "MEET_MOCK_ASR_CORPUS_FILE": str(corpus_file),
        "MEET_MOCK_REQUEST_LOG": str(req_log),
    }
    err_log = workdir / f"worker-{spec['id']}.err"
    with open(err_log, "ab") as err:
        proc = subprocess.Popen(
            [sys.executable, "-m", "app.worker", "--idle-exit-seconds", "8"],
            cwd=REPO_ROOT, env=env,
            stdout=subprocess.DEVNULL, stderr=err,
        )
        try:
            status = wait_job(job_id, {"succeeded", "failed"})
        finally:
            proc.terminate()
            proc.wait(timeout=10)
    if status != "succeeded":
        tail = "\n".join(
            err_log.read_text(encoding="utf-8", errors="replace").splitlines()[-25:]
        )
        raise AssertionError(
            f"评测会谈处理失败: {spec['id']}: "
            f"{q('SELECT last_error FROM processing_job WHERE id=:j', j=job_id)}\n"
            f"--- worker stderr 末尾 ---\n{tail}"
        )

    recording_id = q(
        "SELECT recording_id FROM processing_job WHERE id = :j", j=job_id
    )[0][0]
    return {
        "meeting_id": meeting.id, "org_id": org.id, "recording_id": recording_id,
        "user_id": user.id, "headers": headers, "spec": spec,
        "request_log": str(req_log),
    }


def apply_corrected_transcript(session, ctx) -> dict:
    """导入人工校对转写(新版本,不覆盖原始 ASR 版本)并重建检索索引。

    用途:真实模型评测先隔离 ASR 误差——用校对文本评 Embedding+LLM,
    再接真实 ASR 量端到端误差。mock 语料与校对文本一致,故此路径在
    mock 基线下只验证链路正确,不代表真实效果提升。
    """
    from app.ai.registry import build_providers
    from app.services import transcript_correction

    spec = ctx["spec"]
    lines = spec.get("corrected_corpus")
    if not lines:
        raise AssertionError(f"{spec['id']} 未提供 corrected_corpus")
    _asr, _llm, embed = build_providers()
    info = transcript_correction.import_corrected_transcript(
        session.get_bind(),
        recording_id=ctx["recording_id"],
        lines=[(l[0], l[1]) for l in lines],
        created_by=ctx["user_id"],
        source_note="eval:corrected_corpus",
    )
    idx = transcript_correction.rebuild_search_index(
        session.get_bind(),
        recording_id=ctx["recording_id"],
        version_id=info["version_id"],
        embed=embed,
    )
    return {"correction": info, "index": idx}


def run_permission_probes(client, session, ctx) -> list[dict]:
    """越权评测:无权访问该会谈必须 404;有权访问时引用不得越出本会谈。

    与检索质量指标分开统计——权限是正确性,必须 100%,不是可调参的效果项。
    """
    from app.core.security import hash_password
    from app.models.enums import UserRole
    from app.models.org import MeetingAccess, Organization, User

    rows = []
    for probe in ctx["spec"].get("permission_probes", []):
        kind = probe["kind"]
        ts = datetime.now(timezone.utc).strftime("%H%M%S%f")
        if kind == "cross_org":
            org = Organization(name=f"eval-probe-{ctx['spec']['id']}-{ts}")
            session.add(org)
            session.commit()
            user = User(
                org_id=org.id, username=f"eval-probe-x-{ts}",
                password_hash=hash_password("eval-only"),
                display_name="他机构用户", role=UserRole.org_admin,
            )
        else:
            user = User(
                org_id=ctx["org_id"], username=f"eval-probe-{kind}-{ts}",
                password_hash=hash_password("eval-only"),
                display_name=f"探针-{kind}", role=UserRole.staff,
            )
        session.add(user)
        session.commit()
        if kind == "whitelisted":
            session.add(MeetingAccess(meeting_id=ctx["meeting_id"], user_id=user.id))
            session.commit()

        login = client.post(
            "/auth/login", json={"username": user.username, "password": "eval-only"}
        )
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        resp = client.post(
            f"/meetings/{ctx['meeting_id']}/ask",
            json={"question": probe["question"]}, headers=headers,
        )
        row = {
            "pid": probe["pid"], "kind": kind, "category": probe.get("category", "越权"),
            "question": probe["question"], "expected_status": probe["expect_status"],
            "status": resp.status_code,
            "correct": resp.status_code == probe["expect_status"],
            "citations_within_meeting": None,
        }
        if resp.status_code == 200:
            cited = [
                c["segment_id"] for c in resp.json().get("citations", [])
            ]
            # 引用必须落在本会谈的录音版本链内(跨会谈/跨机构引用即泄露)
            owned = {
                r[0] for r in q(
                    "SELECT ts.id FROM transcript_segment ts"
                    " JOIN transcript_version tv ON tv.id = ts.version_id"
                    " JOIN recording r ON r.id = tv.recording_id"
                    " WHERE r.meeting_id = :m", m=ctx["meeting_id"],
                )
            }
            row["citations_within_meeting"] = set(cited) <= owned
            row["correct"] = row["correct"] and row["citations_within_meeting"]
        rows.append(row)
    return rows


def ask(client, meeting_ctx, question: str) -> dict:
    t0 = time.perf_counter()
    resp = client.post(
        f"/meetings/{meeting_ctx['meeting_id']}/ask",
        json={"question": question},
        headers=meeting_ctx["headers"],
    )
    latency_ms = round((time.perf_counter() - t0) * 1000, 1)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    body["latency_ms"] = latency_ms
    return body


def score(result: dict, gold: dict) -> dict:
    """单题评分:区分无答案正确/误报与证据/答案命中/漏报。"""
    row = {
        "qid": gold["qid"],
        "question": gold["question"],
        "category": gold.get("category"),
        "latency_ms": result["latency_ms"],
        "has_sufficient_evidence": result["has_sufficient_evidence"],
        "relevant_candidate_count": result["relevant_candidate_count"],
        "citation_count": len(result["citations"]),
    }
    if gold.get("no_answer"):
        row["expected"] = "no_answer"
        row["correct"] = not result["has_sufficient_evidence"]
        row["false_positive"] = result["has_sufficient_evidence"]
        return row
    row["expected"] = "answer"
    citations_text = [c["text"] for c in result["citations"]]
    row["evidence_hit"] = any(
        gold["evidence_text"] in t for t in citations_text
    )
    row["answer_hit"] = bool(
        result["answer"] and gold["answer_clue"] in result["answer"]
    )
    row["correct"] = result["has_sufficient_evidence"] and row["evidence_hit"]
    row["missed"] = not result["has_sufficient_evidence"]
    return row


def summarize(rows: list[dict]) -> dict:
    total = len(rows)
    answer_rows = [r for r in rows if r["expected"] == "answer"]
    noanswer_rows = [r for r in rows if r["expected"] == "no_answer"]
    lat = sorted(r["latency_ms"] for r in rows)
    return {
        "total": total,
        "answer_questions": len(answer_rows),
        "evidence_hit_rate": (
            sum(r["evidence_hit"] for r in answer_rows) / len(answer_rows)
            if answer_rows else None
        ),
        "answer_hit_rate": (
            sum(r["answer_hit"] for r in answer_rows) / len(answer_rows)
            if answer_rows else None
        ),
        "missed_rate": (
            sum(r["missed"] for r in answer_rows) / len(answer_rows)
            if answer_rows else None
        ),
        "no_answer_questions": len(noanswer_rows),
        "no_answer_correct_rate": (
            sum(r["correct"] for r in noanswer_rows) / len(noanswer_rows)
            if noanswer_rows else None
        ),
        "no_answer_false_positive_rate": (
            sum(r["false_positive"] for r in noanswer_rows) / len(noanswer_rows)
            if noanswer_rows else None
        ),
        "latency_ms": {
            "avg": round(sum(lat) / total, 1) if total else None,
            "p95": lat[int(total * 0.95) - 1] if total else None,
        },
        "by_category": {
            cat: _summarize_group(group)
            for cat, group in sorted(_group(rows, "category").items())
        },
    }


def _summarize_group(group: list[dict]) -> dict:
    """按类别汇总:可回答题与无答案题分开计数,不混用同名指标。"""
    answers = [r for r in group if r["expected"] == "answer"]
    no_answers = [r for r in group if r["expected"] == "no_answer"]
    return {
        "n": len(group),
        "answer_n": len(answers),
        "answer_correct": sum(r["correct"] for r in answers),
        "no_answer_n": len(no_answers),
        "no_answer_correct": sum(r["correct"] for r in no_answers),
        "false_positive": sum(r["false_positive"] for r in no_answers),
    }


def _group(rows, key):
    out: dict[str, list] = {}
    for r in rows:
        k = r.get(key) or "normal"
        out.setdefault(k, []).append(r)
    return out


def git_rev() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT,
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except Exception:
        return "unknown"


def _git(args: list[str]) -> str:
    return subprocess.run(
        ["git", "-c", "core.quotepath=off"] + args, cwd=REPO_ROOT,
        capture_output=True, text=True, timeout=60,
    ).stdout


def worktree_state() -> dict:
    """工作区状态:仅记 git_rev 不足以复现——未提交的改动不在任何 rev 里。

    记录脏标记、改动清单与内容指纹(已跟踪文件的 diff + 未跟踪被忽略外
    文件的内容哈希)。指纹相同 ≈ 当时的那份代码可被还原核对。
    """
    try:
        rev = _git(["rev-parse", "HEAD"]).strip()
        branch = _git(["rev-parse", "--abbrev-ref", "HEAD"]).strip()
        status = _git(["status", "--porcelain=v1"]).strip()
        diff = _git(["diff", "HEAD", "--no-color"])
    except Exception as e:  # git 不可用时如实标注为未知,不假装干净
        return {"available": False, "rev": git_rev(), "dirty": None,
                "note": f"git 状态读取失败: {type(e).__name__}: {e}"}
    untracked = sorted(
        ln[3:] for ln in status.splitlines() if ln.startswith("?? ")
    )
    modified = sorted(
        ln[3:] for ln in status.splitlines() if ln and not ln.startswith("?? ")
    )
    h = hashlib.sha256()
    h.update(diff.encode("utf-8"))
    h.update(b"\x00".join(p.encode("utf-8") for p in untracked))
    for p in untracked:
        path = REPO_ROOT / p
        candidates = [path] if path.is_file() else (
            sorted(f for f in path.rglob("*") if f.is_file()) if path.is_dir() else []
        )
        for f in candidates:
            h.update(f.name.encode("utf-8"))
            try:
                h.update(f.read_bytes())
            except OSError:
                continue
    return {
        "available": True,
        "rev": rev,
        "branch": branch,
        "dirty": bool(status),
        "modified_files": modified,
        "untracked_files": untracked,
        "content_fingerprint": h.hexdigest()[:16],
        "note": (
            "dirty=True 表示本报告部分代码未提交,仅凭 rev 无法复现;"
            "content_fingerprint 覆盖未提交改动内容"
        ),
    }


def cost_summary(org_ids: list[int]) -> dict:
    """按 model_run 汇总费用口径。

    NULL 成本不按 0 统计:某口径若没有任何"有价"调用,sum_cents 记 None,
    而不是 0——"没花钱"和"不知道花没花钱"必须能区分(mock 属于后者:
    本地计算不产生真实费用,但也不能据此宣称真实成本为 0)。
    """
    if not org_ids:
        return {"runs": 0, "by_basis": {}, "unpriced_real_calls": 0}
    ids = ",".join(str(int(i)) for i in org_ids)  # 全部来自数据库主键,int 化后拼接
    rows = q(
        f"SELECT status, cost_basis, count(*), count(cost_estimate),"
        f" SUM(cost_estimate) FROM model_run WHERE org_id IN ({ids})"
        " GROUP BY status, cost_basis ORDER BY status, cost_basis"
    )
    by_status: dict[str, int] = {}
    by_basis: dict[str, dict] = {}
    for status, basis, n, priced, total in rows:
        by_status[status] = by_status.get(status, 0) + n
        entry = by_basis.setdefault(
            basis, {"calls": 0, "priced_calls": 0, "sum_cents": None}
        )
        entry["calls"] += n
        entry["priced_calls"] += priced
        if priced:
            entry["sum_cents"] = round((entry["sum_cents"] or 0.0) + float(total), 6)
    unpriced = q(
        "SELECT count(*) FROM model_run WHERE org_id IN (%s)"
        " AND is_mock = false AND cost_estimate IS NULL"
        " AND status IN ('succeeded','failed')" % ids
    )[0][0]
    return {
        "runs_by_status": by_status,
        "by_basis": by_basis,
        "unpriced_real_calls": unpriced,
        "budget_cents": [
            r[0] for r in q(
                f"SELECT budget_cents FROM organization WHERE id IN ({ids})"
            )
        ],
        "note": (
            "cost_basis=unknown 的行成本为 NULL(未知),不按 0 计入;"
            "mock 调用恒为 unknown,不得据此声称真实调用零成本"
        ),
    }


def build_report(dataset_path, results_by_split, elapsed, transcript_mode) -> dict:
    from app.ai.registry import build_providers, describe_providers
    from app.core.config import get_settings
    from app.models.transcript import EMBEDDING_DIM

    s = get_settings()
    desc = describe_providers(build_providers())
    all_mock = desc["all_mock"]
    data = json.loads(Path(dataset_path).read_text(encoding="utf-8"))
    org_ids = [i for block in results_by_split.values() for i in block["org_ids"]]
    return {
        "meta": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "git": worktree_state(),
            "database": _current_database(),
            "dataset": {
                "path": str(dataset_path),
                "version": data.get("dataset_version"),
                "sha256": hashlib.sha256(
                    Path(dataset_path).read_bytes()
                ).hexdigest()[:16],
                "split_by": data.get("split_by"),
            },
            "provider_config": {
                "asr": s.asr_provider, "llm": s.llm_provider,
                "embed": s.embed_provider,
            },
            "providers": {
                "asr": desc["asr"]["model"],
                "llm": desc["llm"]["model"],
                "embed": desc["embed"]["model"],
            },
            "providers_detail": desc,
            "prompt_version": desc["llm"]["prompt_version"],
            "embedding_dim": desc["embed"]["dim"],
            "vector_dim_note": (
                f"向量列维度固定 {EMBEDDING_DIM};Provider 声明维度须与之一致,"
                "不符时工厂直接报错(需迁移重建,见 D-03)"
            ),
            "transcript_mode": transcript_mode,
            "transcript_mode_note": (
                "human_corrected=问答基于人工校对转写(隔离 ASR 误差);"
                "asr=基于流水线转写输出(端到端,含 ASR 误差)"
            ),
            "retrieval": {
                "top_k": s.qa_top_k,
                "evidence_threshold": s.qa_evidence_threshold,
            },
            "elapsed_seconds": round(elapsed, 1),
            "is_mock": all_mock,
            "cost": cost_summary(org_ids),
            "declaration": (
                "mock Provider 基线:仅证明评测流程与数据链路正确,"
                "不用于声称真实效果提升;反例误报为待真实模型验证的"
                "测量对象(D-32)"
                if all_mock else
                "非 mock Provider:结果可用于真实效果对比,"
                "但仍受数据集规模与金标质量限制"
            ),
        },
        "splits": {
            split: {
                "questions": block["questions"],
                "summary": summarize(block["questions"]),
                "permission_probes": block["permissions"],
                "permission_summary": {
                    "n": len(block["permissions"]),
                    "correct": sum(r["correct"] for r in block["permissions"]),
                },
            }
            for split, block in results_by_split.items()
        },
    }


def main(argv=None) -> dict:
    parser = argparse.ArgumentParser(description="检索问答评测(mock 基线)")
    parser.add_argument("--dataset", default=str(Path(__file__).parent / "dataset.json"))
    parser.add_argument("--split", choices=["dev", "test", "all"], default="all")
    parser.add_argument("--top-k", type=int, default=None,
                        help="覆盖检索 top_k(一次只调一种策略)")
    parser.add_argument("--threshold", type=float, default=None,
                        help="覆盖证据相关性阈值")
    parser.add_argument("--transcript", choices=["asr", "human_corrected"],
                        default="asr",
                        help="问答所依据的转写来源:流水线 ASR 输出,或先导入"
                             "人工校对转写(隔离 ASR 误差,评 Embedding+LLM)")
    parser.add_argument("--out", default=None, help="报告输出路径(JSON)")
    args = parser.parse_args(argv)

    # 检索参数覆盖(环境变量 → settings 缓存失效重读)
    if args.top_k is not None:
        os.environ["QA_TOP_K"] = str(args.top_k)
    if args.threshold is not None:
        os.environ["QA_EVIDENCE_THRESHOLD"] = str(args.threshold)

    from fastapi.testclient import TestClient

    from app.core.config import get_settings
    from app.core.db import SessionLocal

    get_settings.cache_clear()
    _s = get_settings()
    print(f"[eval] 检索参数: top_k={_s.qa_top_k}, "
          f"threshold={_s.qa_evidence_threshold}", flush=True)

    dataset = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    wanted = (
        {"dev", "test"} if args.split == "all" else {args.split}
    )
    specs = [m for m in dataset["meetings"] if m["split"] in wanted]
    assert specs, f"数据集中无 split={args.split} 的会谈"

    workdir = Path("eval") / ".work"
    workdir.mkdir(parents=True, exist_ok=True)

    t_start = time.monotonic()
    results_by_split: dict[str, dict] = {}
    with TestClient(__import__("app.main", fromlist=["app"]).app) as client:
        session = SessionLocal()
        try:
            for spec in specs:
                ctx = prepare_meeting(client, session, spec, workdir)
                print(f"[eval] 会谈就绪: {spec['id']} "
                      f"(meeting={ctx['meeting_id']}, "
                      f"{len(spec['corpus'])} 句)", flush=True)
                if args.transcript == "human_corrected":
                    info = apply_corrected_transcript(session, ctx)
                    c = info["correction"]
                    print(f"[eval] 人工校对版本已发布: v{c['version_id']} "
                          f"(改动 {c['edited']}/{c['segments']} 句,"
                          f"索引 {info['index']['chunks']} 块)", flush=True)
                rows = []
                for gold in spec["questions"]:
                    result = ask(client, ctx, gold["question"])
                    row = score(result, gold)
                    rows.append(row)
                    print(f"  {gold['qid']}: correct={row['correct']} "
                          f"({row['latency_ms']}ms)", flush=True)
                probes = run_permission_probes(client, session, ctx)
                for p in probes:
                    print(f"  {p['pid']}: status={p['status']} "
                          f"expect={p['expected_status']} correct={p['correct']}",
                          flush=True)
                block = results_by_split.setdefault(spec["split"], {
                    "questions": [], "permissions": [], "org_ids": [],
                })
                block["questions"].extend(rows)
                block["permissions"].extend(probes)
                block["org_ids"].append(ctx["org_id"])
        finally:
            session.close()

    report = build_report(
        args.dataset, results_by_split, time.monotonic() - t_start,
        transcript_mode=args.transcript,
    )

    out = args.out or str(
        Path("eval") / "reports" /
        f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[eval] 报告已写入: {out_path}", flush=True)
    for split, block in report["splits"].items():
        print(f"[eval] {split}: {json.dumps(block['summary'], ensure_ascii=False)}",
              flush=True)
        print(f"[eval] {split} 权限探针: "
              f"{json.dumps(block['permission_summary'], ensure_ascii=False)}",
              flush=True)
    print(f"[eval] 费用: {json.dumps(report['meta']['cost']['by_basis'], ensure_ascii=False)}"
          f" 未定价真实调用={report['meta']['cost']['unpriced_real_calls']}", flush=True)
    return report


if __name__ == "__main__":
    main()
