"""应用入口。"""
from fastapi import FastAPI

from app.api import auth, health, meetings, minutes, qa, recordings

app = FastAPI(title="医疗机构与厂商会谈智能纪要与证据问答系统", version="0.1.0")
app.include_router(health.router)
app.include_router(auth.router)
app.include_router(meetings.router)
app.include_router(recordings.router)
app.include_router(minutes.router)
app.include_router(qa.router)
