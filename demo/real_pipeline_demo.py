"""真实全链路演示:上传语音 → 本地 whisper 转写 → DeepSeek 纪要
→ 本地 bge-m3 索引 → 证据问答。**不使用人工转写导入**。

前置:worker 必须以同一套配置在跑,例如
    ASR_PROVIDER=whisper EMBED_PROVIDER=bge-m3 LLM_PROVIDER=deepseek python -m app.worker

音频来源:demo/make_demo_audio.py 用本机 TTS 合成的中文语音(非真人录音)。
脚本会打印每一片的 ASR 结果与原文台词的差异,便于如实评估识别质量;
不隐藏错字。
"""
import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from demo.seed_demo import (  # noqa: E402  (复用台词与账号,避免两处漂移)
    DEMO, DEMO_PASSWORD, audio_bytes, login,
)

MEDIA = REPO_ROOT / "demo" / "media"


def wait_job(job_id: int, timeout: float = 900) -> str:
    from sqlalchemy import text

    from app.core.db import engine

    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT status, current_stage, last_error FROM processing_job"
                     " WHERE id = :j"), {"j": job_id}
            ).fetchone()
        last = row[0]
        if last in ("succeeded", "failed"):
            if last == "failed":
                print(f"[pipeline] 任务失败: {row[2]}")
            return last
        time.sleep(2)
    raise SystemExit(f"[pipeline] 等待任务超时,最后状态 {last}")


def run_case(client, session, spec, verbose=True):
    from app.models.enums import MeetingVisibility
    from app.models.meeting import Meeting
    from app.models.org import Organization, User
    from app.worker.lease import read_lease  # noqa: F401  (确保模块已加载)

    org = session.query(Organization).filter_by(name=spec["org"]).first()
    admin = session.query(User).filter_by(username=spec["admin"]).first()
    if not (org and admin):
        raise SystemExit("[pipeline] 请先运行 demo/seed_demo.py 建立机构与账号")
    ms = spec["meeting"]
    headers = login(client, spec["admin"])

    meeting = Meeting(
        org_id=org.id, title=f"[真实全链路] {ms['title']}",
        vendor_name=ms["vendor_name"], topic=ms["topic"],
        visibility=MeetingVisibility("org"),
        scheduled_at=datetime(2026, 9, 1, 10, 0), created_by=admin.id,
    )
    session.add(meeting)
    session.commit()

    audio_path = MEDIA / f"{ms['id']}_speech.wav"
    if not audio_path.exists():
        raise SystemExit(f"[pipeline] 缺少语音文件 {audio_path.name},"
                         "请先运行 python demo/make_demo_audio.py")
    resp = client.post(
        f"/meetings/{meeting.id}/recordings",
        files={"file": (audio_path.name, audio_path.read_bytes(), "audio/wav")},
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]
    print(f"\n[pipeline] {ms['id']}: 已上传 {audio_path.name},"
          f"等待 worker(job {job_id})…", flush=True)
    status = wait_job(job_id)
    if status != "succeeded":
        return None

    # 转写结果 vs 原台词:如实展示 ASR 质量
    from sqlalchemy import text as sql_text

    from app.core.db import engine
    with engine.connect() as conn:
        rows = conn.execute(sql_text(
            "SELECT ts.seq, ts.start_ms, ts.end_ms, ts.text"
            " FROM transcript_segment ts"
            " JOIN transcript_version tv ON tv.id = ts.version_id"
            " JOIN recording r ON r.id = tv.recording_id"
            " WHERE r.meeting_id = :m ORDER BY ts.seq"), {"m": meeting.id}
        ).fetchall()
        prov = conn.execute(sql_text(
            "SELECT stage, provider, model_name, is_mock, count(*),"
            " cost_basis, cost_estimate FROM model_run"
            " WHERE job_id = :j GROUP BY stage, provider, model_name, is_mock,"
            " cost_basis, cost_estimate ORDER BY stage"), {"j": job_id}
        ).fetchall()
        ver = conn.execute(sql_text(
            "SELECT tv.source_type, tv.version_no FROM transcript_version tv"
            " JOIN recording r ON r.active_transcript_version_id = tv.id"
            " WHERE r.meeting_id = :m"), {"m": meeting.id}).fetchone()

    print(f"[pipeline] 转写版本: {ver[0]} v{ver[1]}(真实 ASR 产物,"
          "无人工导入)  片段数:", len(rows))
    if verbose:
        for (seq, s_ms, e_ms, asr_text), (_spk, gold) in zip(rows, ms["transcript"]):
            flag = "" if asr_text.replace(" ", "") == gold.replace(" ", "") else "  ← 与台词不一致"
            print(f"  #{seq} [{s_ms // 1000}s-{e_ms // 1000}s] ASR: {asr_text or '(空)'}{flag}")
    print("[pipeline] 模型调用留痕(stage/provider/model/is_mock/次数/成本):")
    for r in prov:
        print(f"  {r[0]:11s} {r[1]:14s} {r[2]:32s} mock={r[3]} x{r[4]} "
              f"{r[5]}={r[6]}")

    for question in ms["questions"]:
        qa = client.post(f"/meetings/{meeting.id}/ask",
                         json={"question": question}, headers=headers)
        assert qa.status_code == 200, qa.text
        r = qa.json()
        if r["has_sufficient_evidence"]:
            cites = ", ".join(f"SEG-{c['segment_id']}@{c['start_ms'] // 1000}s"
                              f"(sim {round(c['similarity'], 3)})"
                              for c in r["citations"])
            print(f"[ask] {question}\n      答: {r['answer']}\n      引用: {cites}")
        else:
            print(f"[ask] {question}\n      → 证据不足(raw={r['raw_retrieved_count']},"
                  f"候选={r['relevant_candidate_count']})")
    return meeting.id


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", default="all",
                    choices=["all", "case_normal", "case_boundary"])
    args = ap.parse_args()

    from fastapi.testclient import TestClient

    from app.core.db import SessionLocal
    from app.main import app

    with TestClient(app) as client:
        session = SessionLocal()
        try:
            from app.ai.registry import describe_providers
            info = describe_providers()
            print("[pipeline] 生效模型:",
                  " | ".join(f"{k}={info[k]['model']}(mock={info[k]['is_mock']})"
                             for k in ("asr", "llm", "embed")))
            for spec in DEMO:
                if args.case != "all" and spec["meeting"]["id"] != args.case:
                    continue
                run_case(client, session, spec)
        finally:
            session.close()


if __name__ == "__main__":
    main()
