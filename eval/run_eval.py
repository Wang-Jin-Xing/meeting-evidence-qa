"""检索问答评测框架(P3 基线)。

用法(在仓库根目录):
    python eval/run_eval.py [--split dev|test|all] [--top-k N]
                            [--threshold F] [--out eval/reports/report.json]

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
        visibility=MeetingVisibility.org, created_by=user.id,
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
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.worker", "--idle-exit-seconds", "8"],
        cwd=REPO_ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        status = wait_job(job_id, {"succeeded", "failed"})
        assert status == "succeeded", (
            f"评测会谈处理失败: {spec['id']}: "
            f"{q('SELECT last_error FROM processing_job WHERE id=:j', j=job_id)}"
        )
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    return {"meeting_id": meeting.id, "headers": headers, "spec": spec}


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


def build_report(dataset_path, results_by_split, elapsed) -> dict:
    from app.ai.mock_providers import MockEmbed, MockLLM, MockASR
    from app.core.config import get_settings
    from app.models.transcript import EMBEDDING_DIM

    s = get_settings()
    data = json.loads(Path(dataset_path).read_text(encoding="utf-8"))
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
            "providers": {
                "asr": MockASR.model_name,
                "llm": MockLLM.model_name,
                "embed": MockEmbed.model_name,
            },
            "prompt_version": MockLLM.prompt_version,
            "embedding_dim": EMBEDDING_DIM,
            "vector_dim_note": (
                f"EMBEDDING_DIM={EMBEDDING_DIM} 固定;真实 Provider 维度"
                "不同需迁移重建(D-03),接入时必须核对"
            ),
            "retrieval": {
                "top_k": s.qa_top_k,
                "evidence_threshold": s.qa_evidence_threshold,
            },
            "elapsed_seconds": round(elapsed, 1),
            "is_mock": True,
            "declaration": (
                "mock Provider 基线:仅证明评测流程与数据链路正确,"
                "不用于声称真实效果提升;反例误报为待真实模型验证的"
                "测量对象(D-32)"
            ),
        },
        "splits": {
            split: {
                "questions": rows,
                "summary": summarize(rows),
            }
            for split, rows in results_by_split.items()
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
    results_by_split: dict[str, list[dict]] = {}
    with TestClient(__import__("app.main", fromlist=["app"]).app) as client:
        session = SessionLocal()
        try:
            for spec in specs:
                ctx = prepare_meeting(client, session, spec, workdir)
                print(f"[eval] 会谈就绪: {spec['id']} "
                      f"(meeting={ctx['meeting_id']}, "
                      f"{len(spec['corpus'])} 句)", flush=True)
                rows = []
                for gold in spec["questions"]:
                    result = ask(client, ctx, gold["question"])
                    row = score(result, gold)
                    rows.append(row)
                    print(f"  {gold['qid']}: correct={row['correct']} "
                          f"({row['latency_ms']}ms)", flush=True)
                results_by_split.setdefault(spec["split"], []).extend(rows)
        finally:
            session.close()

    report = build_report(args.dataset, results_by_split, time.monotonic() - t_start)

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
    return report


if __name__ == "__main__":
    main()
