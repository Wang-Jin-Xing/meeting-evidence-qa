"""应用配置:仅从环境变量 / .env 读取,密钥不写入代码。"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 数据库(仅 PostgreSQL,无 SQLite 降级)
    database_url: str = "postgresql+psycopg://mes:mes_dev_only@localhost:5432/meeting_evidence"

    # API 服务
    app_host: str = "127.0.0.1"
    app_port: int = 8000

    # 模型服务:mock 为默认,真实适配器留待后续阶段接入
    asr_provider: str = "mock"
    llm_provider: str = "mock"
    embed_provider: str = "mock"

    # JWT(本地开发默认值;生产/演示请通过 .env 覆盖)
    jwt_secret: str = "dev-only-secret-change-me"
    jwt_expire_hours: int = 12

    # 存储(上传原件与阶段产物)
    storage_dir: str = "storage"
    max_upload_mb: int = 200


@lru_cache
def get_settings() -> Settings:
    return Settings()
