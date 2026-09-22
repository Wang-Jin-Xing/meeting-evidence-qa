"""健康检查。

区分两类检查:
- GET /health       进程存活检查(liveness):不触碰任何外部依赖,进程能响应即 200。
- GET /health/ready 就绪检查(readiness):实际连接数据库并执行 SELECT 1,
  用于启动门禁与依赖故障暴露。数据库不可用时返回 503 而非崩溃。
"""
from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.db import get_db

router = APIRouter(tags=["health"])


@router.get("/health")
def liveness():
    """进程存活:仅证明 API 进程可响应。"""
    return {"status": "ok", "check": "liveness"}


@router.get("/health/ready")
def readiness(response: Response, db: Session = Depends(get_db)):
    """就绪检查:数据库可达且可查询。"""
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "check": "readiness", "database": "down"}
    return {"status": "ok", "check": "readiness", "database": "up"}
