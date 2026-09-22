"""Provider 工厂:配置驱动,不可用即显式报错(禁止静默回退 mock)。

这里测的是"选择逻辑",不是模型效果。fake 实现只在测试内注册,
用于证明三类不可用情形各自报错——真实 Provider 适配器接入时沿用同一套契约。
"""
import pytest

from app.ai.mock_providers import MockASR, MockEmbed, MockLLM
from app.ai.providers import ASRProvider, EmbedProvider, ProviderUsage
from app.ai.registry import (
    REGISTRY,
    ProviderCredentialError,
    ProviderDimensionError,
    UnknownProviderError,
    ProviderSpec,
    build_providers,
    describe_providers,
)
from app.core.config import Settings


class _FakeRealASR(ASRProvider):
    """协议齐备的"真实"替身:声明非 mock,需要凭证。"""
    name = "fake-real-asr"
    model_name = "fake-real-v1"
    is_mock = False

    def transcribe_slice(self, audio, slice_index):
        raise NotImplementedError


class _WrongDimEmbed(EmbedProvider):
    name = "fake-embed"
    model_name = "fake-embed-1536"
    is_mock = False
    embedding_dim = 1536  # 与向量列 1024 不符

    def embed_batch(self, texts):
        raise NotImplementedError


def _settings(**over) -> Settings:
    base = {"asr_provider": "mock", "llm_provider": "mock", "embed_provider": "mock"}
    base.update(over)
    return Settings(**base)


def test_default_settings_use_mock_and_are_labelled_mock():
    asr, llm, embed = build_providers(_settings())
    assert (type(asr), type(llm), type(embed)) == (MockASR, MockLLM, MockEmbed)
    assert asr.is_mock and llm.is_mock and embed.is_mock


def test_unknown_provider_name_raises_and_does_not_fall_back(monkeypatch):
    monkeypatch.delenv("ASR_API_KEY", raising=False)
    with pytest.raises(UnknownProviderError) as ei:
        build_providers(_settings(asr_provider="some-vendor-asr"))
    msg = str(ei.value)
    assert "some-vendor-asr" in msg and "未注册" in msg
    assert "不会回退 mock" in msg


def test_registered_provider_missing_credential_raises(monkeypatch):
    REGISTRY["asr"]["fake-real"] = ProviderSpec(
        lambda _s: _FakeRealASR(), requires=("asr_api_key",), note="测试替身"
    )
    monkeypatch.delenv("ASR_API_KEY", raising=False)
    with pytest.raises(ProviderCredentialError) as ei:
        build_providers(_settings(asr_provider="fake-real"))
    assert "asr_api_key" in str(ei.value) and "不回退 mock" in str(ei.value)
    REGISTRY["asr"].pop("fake-real")


def test_registered_provider_with_credential_is_used(monkeypatch):
    """凭证齐备时确实换成非 mock 实现(describe 不得继续报 mock)。"""
    REGISTRY["asr"]["fake-real"] = ProviderSpec(
        lambda _s: _FakeRealASR(), requires=("asr_api_key",)
    )
    monkeypatch.setenv("ASR_API_KEY", "not-a-real-key")
    asr, _llm, _embed = build_providers(_settings(asr_provider="fake-real"))
    info = describe_providers((asr, _llm, _embed))
    assert info["asr"]["model"] == "fake-real-v1"
    assert info["asr"]["is_mock"] is False
    assert info["all_mock"] is False
    REGISTRY["asr"].pop("fake-real")


def test_embedding_dimension_mismatch_raises(monkeypatch):
    """向量列按 EMBEDDING_DIM=1024 建立,维度不符必须报错并提示迁移(D-03)。"""
    REGISTRY["embed"]["fake-1536"] = ProviderSpec(lambda _s: _WrongDimEmbed())
    with pytest.raises(ProviderDimensionError) as ei:
        build_providers(_settings(embed_provider="fake-1536"))
    msg = str(ei.value)
    assert "1536" in msg and "1024" in msg and "D-03" in msg
    REGISTRY["embed"].pop("fake-1536")


def test_protocol_mismatch_raises():
    """注册了不符合角色的实现属编程错误,必须报错而不是运行时炸。"""
    REGISTRY["asr"]["broken"] = ProviderSpec(lambda _s: MockLLM())
    with pytest.raises(Exception) as ei:
        build_providers(_settings(asr_provider="broken"))
    assert type(ei.value).__name__ == "ProviderConfigError"
    REGISTRY["asr"].pop("broken")


def test_provider_usage_defaults_to_unknown():
    """ProviderUsage 默认口径是 unknown(不得默认成 0 成本)。"""
    usage = ProviderUsage()
    assert usage.basis == "unknown" and usage.cost_cents is None
