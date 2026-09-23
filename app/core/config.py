"""应用配置:仅从环境变量 / .env 读取,密钥不写入代码。"""
from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 数据库(仅 PostgreSQL,无 SQLite 降级)
    database_url: str = "postgresql+psycopg://mes:mes_dev_only@localhost:5432/meeting_evidence"
    # 独立测试库:仅供 tests/db_guard.py 使用,应用运行时不读取。
    # 无默认值 = 必须显式配置,测试据此拒绝在应用库上执行 TRUNCATE。
    test_database_url: str | None = None

    # API 服务
    app_host: str = "127.0.0.1"
    app_port: int = 8000

    # 模型服务:mock 为默认,真实适配器按 Provider 名注册(app/ai/registry.py)
    asr_provider: str = "mock"
    llm_provider: str = "mock"
    embed_provider: str = "mock"

    # DeepSeek(OpenAI 兼容 Chat Completions)。密钥只经环境变量注入,不写入代码。
    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    deepseek_timeout_seconds: float = 90.0
    deepseek_max_tokens: int = 1600
    # 单价表(货币 / 1K tokens)。留空 = 成本口径 unknown(只记 tokens 用量),
    # 不按 0 记账;填入后成本口径变 estimated。
    llm_price_per_1k_in: float | None = None
    llm_price_per_1k_out: float | None = None

    # JWT(本地开发默认值;生产/演示请通过 .env 覆盖)
    jwt_secret: str = "dev-only-secret-change-me"
    jwt_expire_hours: int = 12

    # 存储(上传原件与阶段产物)
    storage_dir: str = "storage"
    max_upload_mb: int = 200

    # worker 行为(测试通过环境变量覆盖)。
    # 修复 P1.2 遗留:worker_* 字段此前无 MEET_ 前缀别名,测试注入的
    # MEET_WORKER_* 环境变量从未生效(碰巧无测试依赖);现显式支持
    # 两种变量名,与 mock 故障注入(MEET_MOCK_*)的命名风格统一。
    worker_poll_seconds: float = Field(
        default=1.0,
        validation_alias=AliasChoices(
            "worker_poll_seconds", "MEET_WORKER_POLL_SECONDS"
        ),
    )
    worker_lease_ttl_seconds: int = Field(
        default=120,
        validation_alias=AliasChoices(
            "worker_lease_ttl_seconds", "MEET_WORKER_LEASE_TTL_SECONDS"
        ),
    )
    worker_max_attempts: int = Field(
        default=3,
        validation_alias=AliasChoices(
            "worker_max_attempts", "MEET_WORKER_MAX_ATTEMPTS"
        ),
    )
    # 单次外部调用超时(秒):超时抛结果未知(unknown),防止任务永久卡住;
    # 真实 Provider 应自带网络超时,本层为最后防线
    provider_call_timeout_seconds: float = Field(
        default=60.0,
        validation_alias=AliasChoices(
            "provider_call_timeout_seconds", "MEET_WORKER_PROVIDER_TIMEOUT_SECONDS"
        ),
    )
    # 单阶段执行期限(秒):阶段内所有调用共享该期限,超期放弃本次执行
    # 并回队重试(心跳随之停止,防止心跳活着但任务无进展);0 = 不限
    stage_deadline_seconds: float = Field(
        default=300.0,
        validation_alias=AliasChoices(
            "stage_deadline_seconds", "MEET_WORKER_STAGE_DEADLINE_SECONDS"
        ),
    )
    # 重试退避:失败后延迟 next_run_at = now + min(attempt * base, cap) 秒;
    # base=0 禁用退避(立即重试,兼容旧行为)
    retry_backoff_seconds: float = Field(
        default=5.0,
        validation_alias=AliasChoices(
            "retry_backoff_seconds", "MEET_WORKER_RETRY_BACKOFF_SECONDS"
        ),
    )
    retry_backoff_max_seconds: float = Field(
        default=60.0,
        validation_alias=AliasChoices(
            "retry_backoff_max_seconds", "MEET_WORKER_RETRY_BACKOFF_MAX_SECONDS"
        ),
    )
    # 本地模型(无按次账单;模型文件首次使用时下载并按镜像走)
    embed_model: str = "BAAI/bge-m3"          # 输出 1024 维,与向量列一致
    embed_device: str = "cpu"
    embed_batch_size: int = 32
    asr_model: str = "small"                  # faster-whisper 规格
    asr_device: str = "cpu"
    asr_compute_type: str = "int8"
    asr_language: str = "zh"                  # 留空 = 自动检测
    # 模型权重下载镜像:huggingface.co 直连在本机超时。
    # 注意 .env 的值不会自动进入 os.environ,故由 Provider 显式应用。
    hf_endpoint: str = "https://hf-mirror.com"

    # 音频切片时长(秒):切片边界即片段级时间信息来源
    slice_seconds: int = 10

    # 检索问答(同步调用:问题向量化与 LLM 回答在请求内完成,
    # mock 为本地确定性计算;"零模型调用"仅约束上传受理路径)
    qa_top_k: int = 5
    # 证据充分性阈值:候选余弦相似度 >= 阈值才可作为证据来源
    # (mock 向量为字符 bigram,阈值按实测校准,见 docs/DECISIONS.md)
    qa_evidence_threshold: float = 0.15


@lru_cache
def get_settings() -> Settings:
    return Settings()
