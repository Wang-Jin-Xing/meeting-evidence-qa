"""应用入口。"""
from fastapi import FastAPI

from app.api import health

app = FastAPI(title="医疗机构与厂商会谈智能纪要与证据问答系统", version="0.1.0")
app.include_router(health.router)
