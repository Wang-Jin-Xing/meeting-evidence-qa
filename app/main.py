"""应用入口。"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import auth, health, meetings, meta, minutes, qa, recordings, transcripts

app = FastAPI(title="医疗机构与厂商会谈智能纪要与证据问答系统", version="0.1.0")
app.include_router(health.router)
app.include_router(auth.router)
app.include_router(meetings.router)
app.include_router(recordings.router)
app.include_router(minutes.router)
app.include_router(qa.router)
app.include_router(transcripts.router)
app.include_router(meta.router)

# 最小演示界面(纯静态,无构建步骤);API 路由优先,静态只兜底 "/"。
WEB_DIR = Path(__file__).parent / "web"
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
