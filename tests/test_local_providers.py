"""本地 Provider 适配器单测(不加载真实模型权重)。

模型推理本身由 E2E 脚本验证;这里锁定的是**集成契约**:
自描述 WAV 解码、重采样、维度校验、成本口径与失败分类。
"""
import io
import os
import re
import wave

import pytest

from app.ai.local_asr import (GLOSSARY_VERSION, INITIAL_PROMPT,
                            LocalWhisperASR, UnsupportedAudioError)
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
    # 音频没有 token 口径:不得把字节数写进 tokens_in,否则与远程 LLM 的真 token
    # 同列不同单位,台账一汇总就错(D-58)。留 NULL = "本次调用无 token 用量"。
    assert usage.tokens_in is None and usage.tokens_out is None


def test_embed_cost_is_local_zero_and_reports_no_token_usage():
    embed = LocalBgeM3Embed(Settings())
    assert embed.estimate_cost("embed_batch", {"texts": 3, "chars": 42}) == 0.0
    usage = embed.usage_for("embed_batch", {"texts": 3, "chars": 42}, None)
    assert usage.basis == "estimated" and usage.cost_cents == 0.0
    assert usage.tokens_in is None, "批次数不是 token 数,不得冒充实测量"


def test_apply_hf_env_sets_both_and_keeps_existing(monkeypatch):
    """.env 的值不进 os.environ,必须由 Provider 显式应用;已设的变量优先级更高。

    回归的是静默回落:漏掉 HF_HOME 时权重会悄悄下到 C 盘默认目录(本机曾因此
    在系统盘堆了 4.8GB),而不是报错。
    """
    from app.ai.providers import apply_hf_env

    monkeypatch.delenv("HF_ENDPOINT", raising=False)
    monkeypatch.delenv("HF_HOME", raising=False)
    apply_hf_env("https://mirror.example", "D:/model-cache/huggingface")
    assert os.environ["HF_ENDPOINT"] == "https://mirror.example"
    assert os.environ["HF_HOME"] == "D:/model-cache/huggingface"

    monkeypatch.setenv("HF_HOME", "E:/already-set")
    apply_hf_env("https://mirror.example", "D:/model-cache/huggingface")
    assert os.environ["HF_HOME"] == "E:/already-set", "终端显式设置必须优先于 .env"

    monkeypatch.delenv("HF_HOME")
    apply_hf_env("https://mirror.example", "")
    assert "HF_HOME" not in os.environ, "留空 = 不干预,沿用库默认位置"


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


# ---------- 真实录音格式(24-bit/48k/立体声,实测样本) ----------

def _wav(pcm: bytes, rate: int, ch: int, width: int) -> bytes:
    import io as _io
    import wave as _wave

    buf = _io.BytesIO()
    with _wave.open(buf, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def test_decode_24bit_stereo_48k_to_16k_mono():
    """实测录音为 24-bit/48k/立体声:解码后必须是 16k 单声道 float。"""
    import numpy as np

    n = 48000  # 1 秒
    # 24-bit:0.5 满幅 ≈ 2^22;小端 3 字节
    val = 1 << 22
    b = bytes([val & 0xFF, (val >> 8) & 0xFF, (val >> 16) & 0xFF]) * (n * 2)
    wav = _wav(b, 48000, 2, 3)
    out = LocalWhisperASR._to_float32k(wav)
    assert out.shape[0] == 16000, out.shape
    assert abs(float(out[0]) - 0.5) < 1e-3, float(out[0])


def test_decode_rejects_unverified_width():
    import numpy as np

    wav = _wav(b"\x01" * 8000, 8000, 1, 1)  # 8-bit 未验证
    with pytest.raises(UnsupportedAudioError):
        LocalWhisperASR._to_float32k(wav)


def test_glossary_versioned_and_free_of_recording_specifics():
    """词表护栏(D-54):版本化、且不含日期/金额/长句原文(防金标泄漏进提示词)。"""
    from app.ai.local_asr import GLOSSARY_VERSION, INITIAL_PROMPT

    assert GLOSSARY_VERSION
    assert not re.search(r"\d{1,2}月\d{1,2}[日号]|\d{4}-\d{2}-\d{2}", INITIAL_PROMPT)
    assert not re.search(r"\d+(?:\.\d+)?\s*(?:万|元|块)", INITIAL_PROMPT)
    assert len(INITIAL_PROMPT) < 300, "词表应是短词表,不是原文"


def test_transcribe_passes_glossary_and_records_version():
    asr = LocalWhisperASR(Settings())
    fake = _FakeWhisper()
    asr._model = fake
    asr.transcribe_slice(pcm_wav(seconds=1, rate=16000), 0)
    assert fake.seen["kwargs"]["initial_prompt"] == INITIAL_PROMPT
    assert asr.prompt_version == GLOSSARY_VERSION
    assert asr.runtime_device.startswith("local:")
