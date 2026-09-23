"""Provider 工厂:按配置选择实现,**绝不静默回退到 mock**。

配置项 `ASR_PROVIDER` / `LLM_PROVIDER` / `EMBED_PROVIDER` 决定各槽位用哪个实现。
三类不可用情形必须区分并显式抛出(静默回退 mock 会让"真实基线"实为 mock 结果):

1. 未注册的名字 → UnknownProviderError(拼错或适配器尚未接入);
2. 已注册但缺凭证/必需配置 → ProviderCredentialError;
3. Embedding 维度与向量列不符 → ProviderDimensionError(需迁移重建,D-03)。

真实适配器接入方式:实现 app/ai/providers.py 的三个协议,在 `REGISTRY` 对应
角色下注册 ProviderSpec(factory, requires=(凭证配置名,))。requires 缺失即报错。
"""
from collections.abc import Callable
from dataclasses import dataclass, field

from app.ai.deepseek_provider import DeepSeekLLM
from app.ai.local_asr import LocalWhisperASR
from app.ai.local_embed import LocalBgeM3Embed
from app.ai.mock_providers import MockASR, MockEmbed, MockLLM
from app.ai.providers import ASRProvider, EmbedProvider, LLMProvider
from app.core.config import Settings, get_settings
from app.models.transcript import EMBEDDING_DIM


class ProviderConfigError(RuntimeError):
    """Provider 配置不可用——调用方必须中止,不得改用 mock。"""


class UnknownProviderError(ProviderConfigError):
    """配置中的 Provider 名未注册(拼错或尚未接入)。"""


class ProviderCredentialError(ProviderConfigError):
    """Provider 已注册但必需配置/凭证缺失。"""


class ProviderDimensionError(ProviderConfigError):
    """Embedding 维度与向量列不一致,需迁移重建(D-03)。"""


@dataclass(frozen=True)
class ProviderSpec:
    """一个 Provider 实现:构造方式 + 必需配置(凭证)清单。"""

    factory: Callable[[Settings], object]
    requires: tuple[str, ...] = field(default_factory=tuple)
    note: str = ""


_ROLES = {"asr": ASRProvider, "llm": LLMProvider, "embed": EmbedProvider}

REGISTRY: dict[str, dict[str, ProviderSpec]] = {
    "asr": {
        "mock": ProviderSpec(lambda _s: MockASR(), note="本地确定性,无真实费用"),
        "whisper": ProviderSpec(
            lambda s: LocalWhisperASR(s), note="本地 faster-whisper;无按次账单,"
            "不做说话人分离(speaker 为空)",
        ),
    },
    "llm": {
        "mock": ProviderSpec(lambda _s: MockLLM(), note="本地规则式,无真实费用"),
        "deepseek": ProviderSpec(
            lambda s: DeepSeekLLM(s), requires=("deepseek_api_key",),
            note="OpenAI 兼容;tokens 为服务商返回口径,金额按单价表估算",
        ),
    },
    "embed": {
        "mock": ProviderSpec(lambda _s: MockEmbed(), note="字符 bigram 哈希向量"),
        "bge-m3": ProviderSpec(
            lambda s: LocalBgeM3Embed(s), note="本地 BAAI/bge-m3,1024 维,"
            "无按次账单",
        ),
    },
}


def available(role: str) -> list[str]:
    return sorted(REGISTRY.get(role, {}))


def _require_present(key: str, settings: Settings) -> bool:
    """必需配置是否存在(Settings 字段或同名环境变量)。凭证不写代码,只查存在性。"""
    import os

    value = getattr(settings, key, None) or os.environ.get(key.upper())
    return bool(str(value).strip()) if value is not None else False


def build(role: str, name: str, settings: Settings):
    if role not in _ROLES:  # 编程错误,与配置错误区分
        raise KeyError(f"未知 Provider 角色: {role}")
    spec = REGISTRY.get(role, {}).get(name)
    if spec is None:
        raise UnknownProviderError(
            f"{role}_provider={name!r} 未注册;当前可用:{available(role)}。"
            "未知取值不会回退 mock。"
        )
    missing = [k for k in spec.requires if not _require_present(k, settings)]
    if missing:
        raise ProviderCredentialError(
            f"{role}_provider={name!r} 缺少必需配置:{', '.join(missing)}。"
            "凭证只经环境变量/.env 注入;缺失即报错,不回退 mock。"
        )
    provider = spec.factory(settings)
    if not isinstance(provider, _ROLES[role]):
        raise ProviderConfigError(
            f"{role}_provider={name!r} 的实现未继承 {_ROLES[role].__name__},协议不符。"
        )
    if role == "embed" and provider.embedding_dim != EMBEDDING_DIM:
        raise ProviderDimensionError(
            f"embed_provider={name!r} 声明维度 {provider.embedding_dim!r}"
            f" != 向量列维度 {EMBEDDING_DIM}。"
            "需新增迁移重建 embedding_chunk(pgvector 列维度固定,见 D-03)。"
        )
    return provider


def build_providers(settings: Settings | None = None
                    ) -> tuple[ASRProvider, LLMProvider, EmbedProvider]:
    """按配置构造三个 Provider;任一不可用即抛错,不返回 mock 兜底。"""
    s = settings or get_settings()
    return (
        build("asr", s.asr_provider, s),
        build("llm", s.llm_provider, s),
        build("embed", s.embed_provider, s),
    )


def describe_providers(providers=None) -> dict:
    """评测报告/审计的自描述:实际生效的模型与是否 mock(不得硬编码 mock)。"""
    asr, llm, embed = providers or build_providers()
    info = {
        "asr": {"name": asr.name, "model": asr.model_name, "is_mock": asr.is_mock},
        "llm": {"name": llm.name, "model": llm.model_name, "is_mock": llm.is_mock,
                "prompt_version": llm.prompt_version},
        "embed": {"name": embed.name, "model": embed.model_name,
                  "is_mock": embed.is_mock, "dim": embed.embedding_dim},
    }
    info["all_mock"] = all(info[k]["is_mock"] for k in ("asr", "llm", "embed"))
    return info
