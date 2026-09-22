"""演示数据重建脚本(可重复执行,同时作为真实模型闭环的端到端验证)。

用法(仓库根目录):
    python demo/seed_demo.py --reset                 # mock 组合
    LLM_PROVIDER=deepseek python demo/seed_demo.py --reset   # 真实 LLM 组合

流程(与界面操作同一条代码路径,不另开旁路):
  建机构/账号 → 建会谈 → 上传录音(占位静音 WAV,切片给出时间轴)
  → prepare 切片 → 导入人工转写(ASR 服务商不可用时的权威来源)
  → 生成纪要(配置指定的 LLM)→ 建检索索引 → 逐问验证引用。

诚实标注:音频为程序生成的静音占位;转写来源为人工导入;
ASR/Embedding 服务商不可用时仍为 mock,输出中逐项打印。
"""
import argparse
import io
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("PYTHONUTF8", "1")

DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD", "demo-pass-123")
SLICE_SECONDS = 10
AUDIO_SECONDS = 120  # 2 分钟 → 12 个切片,与转写行数一致

# ---- 两组虚构演示数据(正常案例 / 边界案例) ----
DEMO = [
    {
        "org": "演示-市第一人民医院",
        "admin": "demo_admin_a",
        "staff": "demo_staff_a",
        "meeting": {
            "id": "case_normal",
            "title": "X200监护仪采购与技术洽谈(演示:正常案例)",
            "vendor_name": "虚构厂商甲",
            "topic": "监护仪采购",
            "visibility": "org",
            "transcript": [
                ["王主任(机构)", "今天确定X200监护仪的采购数量和交付时间"],
                ["李经理(厂商)", "X200的注册证和检测报告我们在5月20日之前补齐"],
                ["王主任(机构)", "采购数量定为12台,合同本周内签署"],
                ["李经理(厂商)", "第一批6台5月10日送到,第二批6台5月30日前完成"],
                ["张护士长(机构)", "临床培训需要安排我们科室的护士参加"],
                ["李经理(厂商)", "培训共3天,由我们负责,讲师名单7天内确定"],
                ["王主任(机构)", "培训对接人定为张护士长,日期由厂商提前一周通知"],
                ["李经理(厂商)", "耗材价格清单我们在5月15日前提供"],
                ["张护士长(机构)", "售后响应时间要写进合同,请明确"],
                ["李经理(厂商)", "报修后2天内上门,整机保修2年"],
                ["王主任(机构)", "付款分两期,验收合格后付尾款"],
                ["李经理(厂商)", "没问题,发票在验收后7天内开具,我们按这个推进"],
            ],
            "questions": [
                "注册证什么时候补齐?",
                "培训由谁负责?",
                "第一批设备什么时候送到?",
                "明年的科研经费预算是多少?",
            ],
        },
    },
    {
        "org": "演示-康泰儿童医院",
        "admin": "demo_admin_c",
        "staff": "demo_staff_c",
        "meeting": {
            "id": "case_boundary",
            "title": "Y500超声设备采购初步沟通(演示:边界案例)",
            "vendor_name": "虚构厂商乙",
            "topic": "超声设备采购",
            "visibility": "restricted",
            "transcript": [
                ["刘院长(机构)", "今天讨论Y500超声设备的采购意向和旧设备处理"],
                ["陈总监(厂商)", "Y500标准配置的报价是每台120万元"],
                ["刘院长(机构)", "Y300的报价是多少?上次是不是谈过"],
                ["陈总监(厂商)", "这次我们只报了Y500的价格,Y300没有报价"],
                ["刘院长(机构)", "那Y500的注册证什么时候补齐"],
                ["陈总监(厂商)", "注册证我们正在准备,具体时间还没定"],
                ["刘院长(机构)", "售后呢,报修后多久上门"],
                ["陈总监(厂商)", "报修后尽快上门,具体时限我们回去确认后答复"],
                ["赵主任(机构)", "旧设备Y300能否折价回收"],
                ["陈总监(厂商)", "Y300不支持折价回收,只能按废品处理"],
                ["刘院长(机构)", "交付时间和培训安排请先给个明确日期"],
                ["陈总监(厂商)", "这个要等合同签署后再定,现在没有具体日期"],
            ],
            "questions": [
                "Y300的报价是多少?",
                "Y500的注册证什么时候补齐?",
                "报修后多久上门?",
                "Y300可以折价回收吗?",
            ],
        },
    },
]

# 外部机构账号:只用于验证跨机构访问被拒(不拥有任何会谈)
OUTSIDER = {"org": "演示-外部医疗机构", "admin": "demo_outsider"}

BUSINESS_TABLES = [
    "query_log", "embedding_chunk", "segment_mapping", "transcript_segment",
    "transcript_version", "minutes_version", "model_run", "job_stage",
    "processing_job", "recording", "meeting_access", "meeting",
    "user", "organization",
]


MEDIA_DIR = REPO_ROOT / "demo" / "media"


def audio_bytes(case_id: str, seconds: float, rate: int = 8000) -> bytes:
    """优先用 demo/media/<case_id>.wav(随仓库保存的演示录音);
    缺失时生成同时长静音占位(切片数即时间轴,与音频内容无关)。"""
    path = MEDIA_DIR / f"{case_id}.wav"
    if path.exists():
        return path.read_bytes()
    return wav_bytes(seconds, rate)


def wav_bytes(seconds: float, rate: int = 8000) -> bytes:
    """静音 WAV 占位(真实录音待补;时长决定切片数即时间轴)。"""
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


def reset_database(db_name_check=True):
    from sqlalchemy import text

    from app.core.db import engine

    with engine.connect() as conn:
        current = conn.execute(text("SELECT current_database()")).scalar()
    print(f"[seed] 目标数据库: {current}")
    if db_name_check and current.endswith("_test"):
        raise SystemExit("[seed] 拒绝在测试库上重建演示数据")
    quoted = ", ".join(f'"{t}"' for t in BUSINESS_TABLES)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {quoted} RESTART IDENTITY CASCADE"))
    print("[seed] 业务表已清空(--reset)")


def ensure_org_and_users(session):
    from app.core.security import hash_password
    from app.models.enums import UserRole
    from app.models.org import Organization, User

    created = {}
    for spec in DEMO:
        org = session.query(Organization).filter_by(name=spec["org"]).first()
        if org is None:
            org = Organization(name=spec["org"])
            session.add(org)
            session.commit()
        users = [("admin", UserRole.org_admin), ("staff", UserRole.staff)]
        if spec is DEMO[0]:
            other = session.query(Organization).filter_by(name=OUTSIDER["org"]).first()
            if other is None:
                other = Organization(name=OUTSIDER["org"])
                session.add(other)
                session.commit()
            created[OUTSIDER["org"]] = other
            users.append(("outsider", UserRole.org_admin))
        for key, role in users:
            username = OUTSIDER["admin"] if key == "outsider" else spec[key]
            owner = created[OUTSIDER["org"]] if key == "outsider" else org
            if not session.query(User).filter_by(username=username).first():
                session.add(User(
                    org_id=owner.id, username=username,
                    password_hash=hash_password(DEMO_PASSWORD),
                    display_name=f"演示-{username}", role=role,
                ))
                session.commit()
        created[spec["org"]] = org
    return created  # type: ignore[return-value]


def login(client, username):
    resp = client.post("/auth/login", json={"username": username,
                                           "password": DEMO_PASSWORD})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def claim_and_load(job_id: int):
    """领取任务并装载上下文(与 tests/conftest 同名辅助的独立副本)。

    故意不 import tests.conftest:那个模块在导入时就会把进程切到独立测试库,
    演示脚本必须跑在 DATABASE_URL 指向的应用库上。
    """
    from app.core.db import engine
    from app.worker.lease import claim_job, read_lease, worker_id
    from app.worker.stages import load_context

    wid = worker_id()
    claimed = claim_job(engine, wid, ttl_seconds=300)
    if claimed != job_id:
        raise SystemExit(f"[seed] 预期领取 job {job_id},实际 {claimed}"
                         "(可能有其他 worker 在跑,请先停止)")
    epoch, _ = read_lease(engine, job_id)
    return epoch, load_context(engine, job_id)


def process_case(client, session, orgs, spec):
    from app.models.enums import MeetingVisibility
    from app.models.meeting import Meeting
    from app.models.org import User
    from app.worker import stages
    from app.core.db import engine

    org = orgs[spec["org"]]
    admin = session.query(User).filter_by(username=spec["admin"]).first()
    headers = login(client, spec["admin"])
    ms = spec["meeting"]

    meeting = Meeting(
        org_id=org.id, title=ms["title"], vendor_name=ms["vendor_name"],
        topic=ms["topic"], visibility=MeetingVisibility(ms["visibility"]),
        scheduled_at=datetime(2026, 9, 1, 10, 0), created_by=admin.id,
    )
    session.add(meeting)
    session.commit()

    resp = client.post(
        f"/meetings/{meeting.id}/recordings",
        files={"file": (f"{ms['id']}.wav",
                        audio_bytes(ms["id"], AUDIO_SECONDS), "audio/wav")},
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]
    print(f"[seed] {ms['id']}: 录音已受理 job={job_id}")

    # 只跑 prepare(切片即时间轴);转写来源用人工导入
    epoch, ctx = claim_and_load(job_id)
    stages.run_prepare(engine, ctx, epoch, SLICE_SECONDS, lambda: False)

    lines = [{"speaker": s, "text": t} for s, t in ms["transcript"]]
    resp = client.post(
        f"/meetings/{meeting.id}/transcript",
        json={"lines": lines}, headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    p = body["providers"]
    print(f"[seed] {ms['id']}: 转写来源={body['source_type']} "
          f"片段={body['segments']} 纪要条目={body['minutes']['items']} "
          f"索引={body['index']['chunks']}块")
    print(f"[seed]   生效模型: ASR={p['asr']['model']}(mock={p['asr']['is_mock']}) "
          f"LLM={p['llm']['model']}(mock={p['llm']['is_mock']}) "
          f"Embed={p['embed']['model']}(mock={p['embed']['is_mock']})")

    for question in ms["questions"]:
        qa = client.post(f"/meetings/{meeting.id}/ask",
                         json={"question": question}, headers=headers)
        assert qa.status_code == 200, qa.text
        r = qa.json()
        if r["has_sufficient_evidence"]:
            cites = ", ".join(
                f"SEG-{c['segment_id']}@{c['start_ms'] // 1000}s" for c in r["citations"]
            )
            print(f"[ask] {question}\n      答: {r['answer']}\n"
                  f"      引用: {cites} (is_mock={r['is_mock']})")
        else:
            print(f"[ask] {question}\n      → 证据不足,未生成回答 "
                  f"(候选={r['relevant_candidate_count']}, is_mock={r['is_mock']})")
    return {"meeting_id": meeting.id, "import": body}


def show_access_denied(client, session):
    """权限演示:跨机构一律 404;restricted 会谈对同机构非白名单用户也 404。"""
    from app.models.meeting import Meeting
    from app.models.org import User

    checks = [
        (OUTSIDER["admin"], DEMO[0]["meeting"]["title"], "跨机构"),
        (DEMO[1]["staff"], DEMO[1]["meeting"]["title"], "同机构非白名单(restricted)"),
    ]
    for username, title, label in checks:
        user = session.query(User).filter_by(username=username).first()
        meeting = session.query(Meeting).filter_by(title=title).first()
        if not (user and meeting):
            print(f"[权限] 跳过 {label}:账号或会谈不存在")
            continue
        headers = login(client, username)
        r1 = client.get(f"/meetings/{meeting.id}", headers=headers)
        r2 = client.post(f"/meetings/{meeting.id}/ask",
                        json={"question": "注册证什么时候补齐?"}, headers=headers)
        print(f"[权限] {label}:详情 {r1.status_code} / 提问 {r2.status_code}"
              f"(期望均 404;无权与不存在不区分)")


def main():
    ap = argparse.ArgumentParser(description="重建演示数据并验证处理闭环")
    ap.add_argument("--reset", action="store_true", help="先清空业务表")
    args = ap.parse_args()

    if args.reset:
        reset_database()

    from fastapi.testclient import TestClient

    from app.core.db import SessionLocal
    from app.main import app

    results = []
    with TestClient(app) as client:
        session = SessionLocal()
        try:
            orgs = ensure_org_and_users(session)
            for spec in DEMO:
                results.append(process_case(client, session, orgs, spec))
            show_access_denied(client, session)
        finally:
            session.close()

    print("\n[seed] 演示账号(密码见 DEMO_PASSWORD 环境变量或脚本默认值):")
    for spec in DEMO:
        print(f"  {spec['admin']}(管理员) / {spec['staff']}(普通用户)")
    print(f"  {OUTSIDER['admin']}(外部机构,用于越权演示)")
    print("[seed] 会谈 id:", [r["meeting_id"] for r in results])


if __name__ == "__main__":
    main()
