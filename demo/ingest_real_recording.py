"""真实录音端到端:上传原文件 → 本地 whisper ASR → DeepSeek 纪要 → bge-m3 索引 → 问答。

本轮决策(P1=A / P2 / P3 / P5)的落地实现:
- 上传**原始文件**(不预先降采样),24-bit/48k/立体声等由 ASR 适配器在内存内
  标准化;原文件永不改写。若适配器报不支持,用 --convert-wav16 生成
  `<原名>.std16.wav`(标准 16-bit/16k 单声道)后继续,原文件保留(P3)。
- 保留 10 秒切片与边界错误(P1=A);时间标注为"切片范围,非精确语句定位"。
- 双链路(P5):会谈 A = 原始 ASR(主展示,不被替换);会谈 B = 同一录音先跑
  ASR 再导入人工校对稿为**新版本**并发布。两者走同一套发布/检索流程、
  同一模型与参数,各自只检索本会谈;不添加绕过 active 指针的旁路。
- 人工校对稿只作评测金标,**绝不**进入 ASR 的 initial_prompt(P2)。
- 单次处理预算默认 300 秒(运行预算,非性能保证),实际耗时记入报告与 model_run。

用法:
    ASR_PROVIDER=whisper EMBED_PROVIDER=bge-m3 LLM_PROVIDER=deepseek \\
    QA_EVIDENCE_THRESHOLD=0.66 HF_ENDPOINT=https://hf-mirror.com \\
      python demo/ingest_real_recording.py --audio "C:/.../会议录音.wav" \\
      [--corrected 校对稿.json] [--questions 问题.json] [--convert-wav16]
"""
import argparse
import hashlib
import io
import json
import re
import sys
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

CORPUS_DIR = REPO_ROOT / "demo" / "corpus"
OUT_DIR = REPO_ROOT / "demo" / "verification"

# 关键实体类别:型号 / 日期 / 金额 / 负责人 / 否定 —— 优先核对项
ENTITY_PATTERNS = {
    "型号": re.compile(r"[A-Za-z]{1,4}\s?-?\s?\d{2,4}[A-Za-z]?"),
    "日期": re.compile(r"\d{1,2}月\d{1,2}[日号]|\d{4}-\d{2}-\d{2}|本周|下周|本月|月底前"),
    "金额": re.compile(r"\d+(?:\.\d+)?\s*(?:万|元|块|k|K)"),
    "负责人": re.compile(r"由[^,。;；]{1,12}负责|[^,。;；]{0,6}(?:经理|主任|院长|护士长|总监|老师)"),
    "否定": re.compile(r"不支持|不能|不可|无法|没有|未[定知补齐]|暂不|仅"),
}

DEFAULT_QUESTIONS = [
    "这次会谈主要谈了什么设备或事项?",
    "有哪些明确的时间安排(交付、补齐、培训等)?",
    "哪些事项由谁负责?",
    "有没有提到价格或费用?",
    "有没有否定、做不到或待确认的承诺?",
]


def probe_audio(path: Path) -> dict:
    with wave.open(str(path), "rb") as w:
        comp = w.getcomptype()
        info = {
            "path": str(path), "size_bytes": path.stat().st_size,
            "channels": w.getnchannels(), "sample_width_bits": w.getsampwidth() * 8,
            "sample_rate": w.getframerate(),
            "duration_seconds": round(w.getnframes() / w.getframerate(), 2),
            "comptype": comp.decode() if isinstance(comp, bytes) else comp,
            "sha256_16": hashlib.sha256(path.read_bytes()).hexdigest()[:16],
        }
    info["slices_at_10s"] = int(-(-info["duration_seconds"] // 10))
    return info


def convert_to_std_wav(src: Path) -> Path:
    """P3 兜底:原文件保留,另存标准 16-bit/16k 单声道 WAV。"""
    import numpy as np

    from app.ai.local_asr import _pcm_to_float
    with wave.open(str(src), "rb") as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        frames = w.readframes(w.getnframes())
    samples = _pcm_to_float(frames, width)
    if ch > 1:
        samples = samples.reshape(-1, ch).mean(axis=1)
    if rate != 16000:
        target = int(round(samples.shape[0] * 16000 / rate))
        idx = np.arange(target) * (samples.shape[0] - 1) / max(target - 1, 1)
        samples = np.interp(idx, np.arange(samples.shape[0]), samples)
    out = src.with_suffix(".std16.wav")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
    out.write_bytes(buf.getvalue())
    return out


def entities(text: str) -> dict:
    return {k: sorted({m.group(0).strip() for m in p.finditer(text or "")})
            for k, p in ENTITY_PATTERNS.items()}


def ensure_account(session):
    from app.core.security import hash_password
    from app.models.enums import UserRole
    from app.models.org import Organization, User
    from demo.seed_demo import DEMO_PASSWORD

    org = session.query(Organization).filter_by(name="演示-真实录音会谈").first()
    if org is None:
        org = Organization(name="演示-真实录音会谈")
        session.add(org)
        session.commit()
    admin = session.query(User).filter_by(username="demo_real_admin").first()
    if admin is None:
        admin = User(org_id=org.id, username="demo_real_admin",
                     password_hash=hash_password(DEMO_PASSWORD),
                     display_name="演示-真实录音管理员", role=UserRole.org_admin)
        session.add(admin)
        session.commit()
    return org, admin


def upload_and_run(client, session, org, admin, audio_path: Path, title: str,
                   args) -> dict:
    """建会谈 → 上传原文件 → **进程内**跑完四阶段 → 取转写与墙钟耗时。

    进程内跑(而非等外部 worker)的原因:真实录音链路要用 whisper,而常驻
    worker 可能配置为 mock;让脚本自己领取任务并持有自己的 Provider 组合,
    避免两种配置互相抢任务。领取/租约/阶段代码与 worker 完全同一套。
    """
    from sqlalchemy import text as sql_text

    from app.ai.registry import build_providers
    from app.core.db import engine
    from app.models.enums import MeetingVisibility
    from app.models.meeting import Meeting
    from app.worker import stages
    from app.worker.lease import (LeaseLostError, claim_job, read_lease,
                                  requeue_or_fail, worker_id)
    from demo.seed_demo import login

    headers = login(client, admin.username)
    meeting = Meeting(
        org_id=org.id, title=title, vendor_name=args.vendor, topic=args.topic,
        visibility=MeetingVisibility("org"),
        scheduled_at=datetime(2026, 9, 1, 10, 0), created_by=admin.id,
    )
    session.add(meeting)
    session.commit()

    t0 = time.monotonic()
    resp = client.post(
        f"/meetings/{meeting.id}/recordings",
        files={"file": (audio_path.name, audio_path.read_bytes(), "audio/wav")},
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]
    print(f"[ingest] {title}: 已受理 job={job_id},进程内跑四阶段"
          f"(真实 ASR,预算 {args.timeout:.0f}s)…", flush=True)

    asr, llm, embed = build_providers()
    wid = worker_id()
    claimed = claim_job(engine, wid, ttl_seconds=900)
    if claimed != job_id:
        raise SystemExit(f"[ingest] 领取到 {claimed} 而非 {job_id}"
                         "(可能有其他 worker 在抢任务,请先停掉)")
    epoch, _ = read_lease(engine, job_id)
    ctx = stages.load_context(engine, job_id)
    try:
        stages.run_prepare(engine, ctx, epoch, 10, lambda: False)
        stages.run_transcribe(engine, ctx, epoch, asr, lambda: False)
        stages.run_minutes(engine, ctx, epoch, llm, lambda: False)
        stages.run_index(engine, ctx, epoch, embed, lambda: False)
        with engine.begin() as conn:
            conn.execute(sql_text(
                "UPDATE processing_job SET status='succeeded', current_stage=NULL,"
                " lease_expires_at=NULL, next_run_at=NULL, last_error=NULL"
                " WHERE id=:j"), {"j": job_id})
    except (LeaseLostError, Exception) as e:  # noqa: BLE001 演示脚本:统一收尾
        requeue_or_fail(engine, job_id, epoch, "ingest", f"{type(e).__name__}: {e}",
                        max_attempts=1, attempt_count=1)
        raise SystemExit(f"[ingest] 流水线失败: {type(e).__name__}: {e}\n"
                         "若为 UnsupportedAudioError,请加 --convert-wav16 重试")
    wall = round(time.monotonic() - t0, 1)

    with engine.connect() as conn:
        rows = conn.execute(sql_text(
            "SELECT ts.seq, ts.start_ms, ts.end_ms, ts.speaker, ts.text,"
            " ts.timestamp_precision, tv.id, tv.version_no, tv.source_type"
            " FROM transcript_segment ts"
            " JOIN transcript_version tv ON tv.id = ts.version_id"
            " JOIN recording r ON r.id = tv.recording_id"
            " WHERE r.meeting_id = :m ORDER BY ts.seq"), {"m": meeting.id}
        ).fetchall()
        rec_id = conn.execute(sql_text(
            "SELECT id FROM recording WHERE meeting_id=:m ORDER BY id LIMIT 1"),
            {"m": meeting.id}).fetchone()[0]
    lines = [{"seq": r[0], "start_ms": r[1], "end_ms": r[2], "speaker": r[3],
              "text": r[4], "timestamp_precision": r[5], "version_id": r[6],
              "version_no": r[7], "source_type": r[8]} for r in rows]
    over = wall > args.timeout
    print(f"[ingest] {title}: 完成,墙钟 {wall}s"
          f"{'(超出预算!)' if over else ''},转写 {len(lines)} 片"
          f"(source={lines[0]['source_type'] if lines else '-'})")
    for line in lines:
        print(f"  #{line['seq']} [{line['start_ms']//1000}s-{line['end_ms']//1000}s]"
              f" {line['text'] or '(空)'}")
    return {"meeting_id": meeting.id, "recording_id": rec_id, "job_id": job_id,
            "headers": headers, "lines": lines, "wall_seconds": wall,
            "over_budget": over}


def ask_all(client, ctx, questions) -> list:
    out = []
    for q in questions:
        t0 = time.perf_counter()
        r = client.post(f"/meetings/{ctx['meeting_id']}/ask",
                        json={"question": q}, headers=ctx["headers"])
        ms = round((time.perf_counter() - t0) * 1000, 1)
        assert r.status_code == 200, r.text
        b = r.json()
        out.append({
            "question": q, "latency_ms": ms,
            "has_sufficient_evidence": b["has_sufficient_evidence"],
            "raw_retrieved": b["raw_retrieved_count"],
            "candidates": b["relevant_candidate_count"],
            "answer": b.get("answer"),
            "citations": [{"segment_id": c["segment_id"],
                           "similarity": c["similarity"],
                           "start_ms": c["start_ms"], "end_ms": c["end_ms"],
                           "timestamp_precision": c.get("timestamp_precision"),
                           "text": c["text"]} for c in b.get("citations", [])],
        })
        print(f"[ask] {q} → {'有回答' if b.get('answer') else '证据不足'}"
              f"(候选 {b['relevant_candidate_count']},"
              f"引用 {len(b.get('citations', []))},{ms}ms)")
    return out


def compare_chains(asr_lines, corrected_pairs, chain_a, chain_b) -> dict:
    """逐句实体差异 + 逐问结果差异;归因只到"疑似",不假装因果确定。"""
    per_slice = []
    for a, c in zip(asr_lines, corrected_pairs):
        gold = c[1]
        ea, ec = entities(a["text"]), entities(gold)
        diffs = {k: {"ASR": ea[k], "校对": ec[k]} for k in ea if ea[k] != ec[k]}
        per_slice.append({"seq": a["seq"], "asr_text": a["text"],
                          "corrected_text": gold,
                          "char_equal": a["text"] == gold,
                          "entity_diffs": diffs})
    per_question = []
    for a, b in zip(chain_a, chain_b):
        same_answer = (a["answer"] or "") == (b["answer"] or "")
        same_state = (a["has_sufficient_evidence"] == b["has_sufficient_evidence"])
        diff_slices = [s["seq"] for s in per_slice if s["entity_diffs"]]
        if not same_state:
            attribution = "疑似语音识别错误影响(转写差异改变了充分性判断)"
        elif not same_answer:
            attribution = "疑似语音识别错误影响(转写差异改变了回答内容)"
        elif diff_slices:
            attribution = "两链路结果一致(识别差异未改变本次回答)"
        else:
            attribution = "两链路结果一致且转写无实体差异"
        per_question.append({
            "question": a["question"],
            "asr_answer": a["answer"], "corrected_answer": b["answer"],
            "asr_sufficient": a["has_sufficient_evidence"],
            "corrected_sufficient": b["has_sufficient_evidence"],
            "citations_asr": [c["segment_id"] for c in a["citations"]],
            "citations_corrected": [c["segment_id"] for c in b["citations"]],
            "slices_with_entity_diffs": diff_slices,
            "attribution": attribution,
        })
    return {"per_slice": per_slice, "per_question": per_question,
            "note": "归因依据是「转写实体差异」与「两链路结果是否改变」的对应关系,"
                    "属观察性结论;确定因果需更大样本与人工复核"}


def ledger(meeting_id: int) -> list:
    from sqlalchemy import text as sql_text

    from app.core.db import engine
    with engine.connect() as conn:
        rows = conn.execute(sql_text(
            "SELECT mr.stage, mr.provider, mr.model_name, mr.is_mock, mr.status,"
            " mr.cost_basis, mr.cost_estimate, mr.device, mr.prompt_version,"
            " count(*), round(avg(mr.duration_ms)), max(mr.duration_ms)"
            " FROM model_run mr"
            " LEFT JOIN processing_job j ON j.id = mr.job_id"
            " LEFT JOIN recording r ON r.id = j.recording_id"
            " WHERE r.meeting_id = :m OR (mr.job_id IS NULL AND mr.org_id ="
            "   (SELECT org_id FROM meeting WHERE id = :m))"
            " GROUP BY 1,2,3,4,5,6,7,8,9 ORDER BY 1,2"), {"m": meeting_id}
        ).fetchall()
    return [{"stage": x[0], "provider": x[1], "model": x[2], "is_mock": x[3],
             "status": x[4], "cost_basis": x[5], "cost_cents": x[6],
             "device": x[7], "prompt_version": x[8], "calls": x[9],
             "avg_ms": x[10], "max_ms": x[11]} for x in rows]


def main():
    ap = argparse.ArgumentParser(description="真实录音端到端验证(P1=A)")
    ap.add_argument("--audio", required=True)
    ap.add_argument("--corrected", help="人工校对稿 JSON(仅评测金标,不进 ASR 提示词)")
    ap.add_argument("--questions", help="问题清单 JSON(字符串数组)")
    ap.add_argument("--title", default="真实录音会谈")
    ap.add_argument("--vendor", default="真实会谈厂商")
    ap.add_argument("--topic", default="设备采购")
    ap.add_argument("--timeout", type=float, default=300.0,
                    help="单录音处理预算(秒),非性能保证")
    ap.add_argument("--convert-wav16", action="store_true",
                    help="原格式解码失败时:保留原文件,另存标准 WAV 后继续")
    args = ap.parse_args()

    from fastapi.testclient import TestClient

    from app.ai.registry import describe_providers
    from app.core.db import SessionLocal
    from app.main import app

    info = describe_providers()
    print("[ingest] 生效模型:",
          " | ".join(f"{k}={info[k]['model']}(mock={info[k]['is_mock']})"
                     for k in ("asr", "llm", "embed")))
    if info["asr"]["is_mock"]:
        raise SystemExit("[ingest] ASR 仍是 mock:请设 ASR_PROVIDER=whisper")

    src = Path(args.audio)
    if not src.exists():
        raise SystemExit(f"[ingest] 找不到录音: {src}")
    probe = probe_audio(src)
    print("[ingest] 音频实测:", json.dumps(probe, ensure_ascii=False))
    upload_path = src
    if args.convert_wav16:
        upload_path = convert_to_std_wav(src)
        print(f"[ingest] 已生成标准 WAV(原文件保留): {upload_path}")

    questions = (json.loads(Path(args.questions).read_text(encoding="utf-8"))
                 if args.questions else DEFAULT_QUESTIONS)
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stem = src.stem

    with TestClient(app) as client:
        session = SessionLocal()
        try:
            org, admin = ensure_account(session)

            # 会谈 A:原始 ASR,主展示
            ctx_a = upload_and_run(client, session, org, admin, upload_path,
                                   f"{args.title}(真实ASR·主展示)", args)
            chain_a = ask_all(client, ctx_a, questions)
            asr_file = CORPUS_DIR / f"{stem}.asr.json"
            asr_file.write_text(json.dumps(
                {"source_audio": probe, "meeting_id": ctx_a["meeting_id"],
                 "asr_model": info["asr"]["model"],
                 "glossary_note": "initial_prompt 为固定版本化通用词表,"
                                  "不含本录音原文/答案/日期/金额",
                 "timestamp_note": "切片范围,非精确语句定位",
                 "lines": ctx_a["lines"]},
                ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[ingest] ASR 原始输出(含错误,未修正)已存 {asr_file}")

            report = {
                "meta": {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "audio": probe, "providers": info,
                    "slice_seconds": 10,
                    "timestamp_note": "切片范围,非精确语句定位(P1=A)",
                    "timeout_budget_seconds": args.timeout,
                    "meeting_a": ctx_a["meeting_id"],
                },
                "chain_a_wall_seconds": ctx_a["wall_seconds"],
                "chain_asr": chain_a,
                "ledger_a": ledger(ctx_a["meeting_id"]),
            }

            # 会谈 B:同录音,人工校对稿为新版本并发布(仅当提供校对稿)
            if args.corrected:
                pairs = [(l[0], l[1]) if isinstance(l, (list, tuple)) else (None, l)
                         for l in json.loads(
                             Path(args.corrected).read_text(encoding="utf-8"))]
                ctx_b = upload_and_run(client, session, org, admin, upload_path,
                                       f"{args.title}(人工校对·评测)", args)
                from app.ai.registry import build_providers
                from app.core.db import engine
                from app.services import transcript_correction

                _a, _l, embed = build_providers()
                corr = transcript_correction.import_corrected_transcript(
                    engine, recording_id=ctx_b["recording_id"], lines=pairs,
                    created_by=admin.id, source_note="eval_gold_reference",
                )
                assert corr["version_id"] != ctx_b["lines"][0]["version_id"], \
                    "校对必须新增版本,不得覆盖 ASR 原版"
                transcript_correction.publish_minutes(
                    engine, recording_id=ctx_b["recording_id"],
                    version_id=corr["version_id"], llm=_l)
                transcript_correction.rebuild_search_index(
                    engine, recording_id=ctx_b["recording_id"],
                    version_id=corr["version_id"], embed=embed)
                print(f"[ingest] 会谈 B 校对版本 v{corr['version_id']} 已发布"
                      f"(改动 {corr['edited']}/{corr['segments']});"
                      f"会谈 A 仍为原始 ASR")
                chain_b = ask_all(client, ctx_b, questions)
                report["meeting_b"] = ctx_b["meeting_id"]
                report["correction"] = corr
                report["chain_b_wall_seconds"] = ctx_b["wall_seconds"]
                report["chain_corrected"] = chain_b
                report["ledger_b"] = ledger(ctx_b["meeting_id"])
                report["comparison"] = compare_chains(
                    ctx_a["lines"], pairs, chain_a, chain_b)

            out = OUT_DIR / f"real_audio_{stem}.json"
            from decimal import Decimal
            out.write_text(json.dumps(report, ensure_ascii=False, indent=2,
                                      default=lambda o: float(o)
                                      if isinstance(o, Decimal) else str(o)),
                           encoding="utf-8")
            print(f"[ingest] 报告已写入 {out}")
        finally:
            session.close()


if __name__ == "__main__":
    main()
