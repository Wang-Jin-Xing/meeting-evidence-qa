"""数据库引擎与会话工厂。"""
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.core.config import get_settings


class Base(DeclarativeBase):
    """所有 ORM 模型的声明基类。"""


def make_engine():
    # pool_pre_ping: 连接失效前先探测,避免长驻 worker 持有断连
    return create_engine(get_settings().database_url, pool_pre_ping=True)


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    """FastAPI 依赖:请求级会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
