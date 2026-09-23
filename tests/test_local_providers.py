"""本地 Provider 适配器单测(不加载真实模型权重)。

模型推理本身由 E2E 脚本验证;这里锁定的是**集成契约**:
自描述 WAV 解码、重采样、维度校验、成本口径与失败分类。
"""
import io
import wave

import pytest

from app.ai.local_asr import LocalWhisperASR, UnsupportedAudioError
from app.ai.local_embed import EmbedDimensionMismatch, LocalBgeM3Embed
from app.ai.providers import ASRSlice, ProviderUsage
from app.ai.registry import REGISTRY, build, build_providers
from app.core.config import Settings


def pcm_wav(seconds=1.0, rate=8000, channels=1, width=2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        n = int(rate * seconds) * channels
        w.writeframes((b"\x00\x01" * n)[: int(n) * width])
    return buf.getvalue()


class _FakeSeg:
    def __init__(self, text): self.text = text


class _FakeWhisper:
    def __init__(self, text="监护仪采购与交付安排"):
        self.text = text
        self.seen = {}

    def transcribe(self, samples, **kwargs):
        self.seen = {"n": int(samples.shape[0]), "kwargs": kwargs}
        return iter([_FakeSeg(self.text)]), object()


class _FakeEncoder:
    def __init__(self, dim=1024):
        self.dim = dim
        self.calls = []

    def encode(self, texts, **kwargs):
        self.calls.append((list(texts), kwargs))
        return [[0.01] * self.dim for _ in texts]


# ---------- ASR ----------

def test_asr_decodes_self_describing_wav_and_resamples():
    """8kHz 单声道 → whisper 需要 16kHz:重采样后长度应翻倍,且参数正确。"""
    asr = LocalWhisperASR(Settings())
    fake = _FakeWhisper()
    asr._model = fake
    out = asr.transcribe_slice(pcm_wav(seconds=2, rate=8000), 0)
    assert isinstance(out, ASRSlice) and out.is_mock is False
    assert out.text == "监护仪采购与交付安排"
    assert fake.seen["n"] == 32000            # 2s × 16000
    assert fake.seen["kwargs"]["language"] == "zh"
    assert fake.seen["kwargs"]["vad_filter"] is True


def test_asr_downmixes_stereo():
    asr = LocalWhisperASR(Settings())
    fake = _FakeWhisper()
    asr._model = fake
    asr.transcribe_slice(pcm_wav(seconds=1, rate=16000, channels=2), 0)
    assert fake.seen["n"] == 16000  # 双声道混为单声道,不是 32000


def test_asr_rejects_non_pcm16():
    asr = LocalWhisperASR(Settings())
    with pytest.raises(UnsupportedAudioError):
        asr.transcribe_slice(pcm_wav(seconds=0.1, width=1), 0)


def test_asr_empty_slice_returns_empty_text_without_loading_model():
    asr = LocalWhisperASR(Settings())
    out = asr.transcribe_slice(pcm_wav(seconds=0.0, rate=8000), 3)
    assert out.text == "" and out.speaker is None  # 不加载权重,也不编造文本


def test_asr_cost_is_local_zero_not_unknown():
    asr = LocalWhisperASR(Settings())
    assert asr.estimate_cost("transcribe_slice", {"audio_bytes": 100}) == 0.0
    usage = asr.usage_for("transcribe_slice", {"audio_bytes": 100}, None)
    assert usage.basis == "estimated" and usage.cost_cents == 0.0
    assert usage.tokens_in == 100


# ---------- Embedding ----------

def test_embed_returns_unit_vectors_of_declared_dim():
    embed = LocalBgeM3Embed(Settings())
    embed._model = _FakeEncoder(dim=1024)
    vecs = embed.embed_batch(["注册证什么时候补齐", "培训由谁负责"])
    assert len(vecs) == 2 and all(len(v) == 1024 for v in vecs)
    _, kwargs = embed._model.calls[0]
    assert kwargs["normalize_embeddings"] is True  # 检索用余弦距离


def test_embed_rejects_dimension_mismatch_instead_of_writing_bad_rows():
    """向量列固定 1024 维;模型输出不符必须报错(D-03)。"""
    embed = LocalBgeM3Embed(Settings())
    embed._model = _FakeEncoder(dim=768)
    with pytest.raises(EmbedDimensionMismatch) as ei:
        embed.embed_batch(["一句话"])
    assert "768" in str(ei.value) and "1024" in str(ei.value)


def test_embed_replaces_blank_text_so_model_does_not_crash():
    embed = LocalBgeM3Embed(Settings())
    embed._model = _FakeEncoder()
    embed.embed_batch(["", "   ", "有内容"])
    sent = embed._model.calls[0][0]
    # 空串换成空格占位:传给模型的文本不得为空字符串(部分实现会直接报错)
    assert all(len(s) >= 1 for s in sent), sent
    assert sent[0] == " " and sent[2] == "有内容", sent


# ---------- 工厂接线 ----------

def test_local_providers_registered_and_not_mock():
    s = Settings(asr_provider="whisper", llm_provider="mock", embed_provider="bge-m3")
    asr, _llm, embed = build_providers(s)
    assert isinstance(asr, LocalWhisperASR) and isinstance(embed, LocalBgeM3Embed)
    assert asr.is_mock is False and embed.is_mock is False
    assert embed.embedding_dim == 1024  # 与向量列一致,工厂维度校验通过


def test_local_providers_build_no_model_at_construction_time():
    """构造不得加载权重(否则导入即占数 GB 内存、测试变慢)。"""
    asr = build("asr", "whisper", Settings())
    embed = build("embed", "bge-m3", Settings())
    assert asr._model is None and embed._model is None


def test_registry_lists_local_and_mock_side_by_side():
    assert set(REGISTRY["asr"]) == {"mock", "whisper"}
    assert set(REGISTRY["embed"]) == {"mock", "bge-m3"}
