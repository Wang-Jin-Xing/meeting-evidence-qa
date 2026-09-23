"""本地 ASR:faster-whisper(CTranslate2)。

集成约定(重要):worker 送给 Provider 的分片是**自描述 WAV 容器**
(带 RIFF 头),而不是裸 PCM 字节 —— 采样率/声道/位宽必须由音频本身体现,
不能靠约定猜测(猜错采样率会让识别结果变成噪音)。见 stages._read_slice_audio。

能力边界(如实声明):
- 按 10 秒切片调用,**不做说话人分离**,speaker 一律为 None;
  负责人信息因此只能来自纪要模型对文本内容的判断,缺失时按空值策略转人工确认。
- 静音/无语音片段经 VAD 过滤后返回空文本(不编造内容)。
- 成本:本地推理无按次账单 → 0 且口径 estimated(不等于没有算力/电耗)。
"""
import io
import os
import threading
import wave

from app.ai.providers import ASRProvider, ASRSlice, ProviderUsage
from app.core.config import Settings

_CACHE: dict[tuple, object] = {}
_LOCK = threading.Lock()


class UnsupportedAudioError(RuntimeError):
    """分片不是可识别的 PCM 格式(当前只支持 16-bit 整数 PCM)。"""


class LocalWhisperASR(ASRProvider):
    name = "local-whisper"
    is_mock = False

    def __init__(self, settings: Settings):
        self.model_size = settings.asr_model
        self.device = settings.asr_device or "cpu"
        self.compute_type = settings.asr_compute_type
        self.language = (settings.asr_language or "").strip() or None
        self.model_name = f"faster-whisper:{self.model_size}@{self.compute_type}"
        self._model = None

    # ---- 成本 ----

    def estimate_cost(self, call: str, units: dict) -> float | None:
        return 0.0

    def usage_for(self, call: str, units: dict, result) -> ProviderUsage | None:
        return ProviderUsage(basis="estimated", cost_cents=0.0,
                             tokens_in=units.get("audio_bytes"), tokens_out=None)

    # ---- 模型加载 ----

    def _load(self):
        if self._model is not None:
            return self._model
        with _LOCK:
            key = (self.model_size, self.device, self.compute_type)
            if key not in _CACHE:
                os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
                from faster_whisper import WhisperModel

                _CACHE[key] = WhisperModel(
                    self.model_size, device=self.device,
                    compute_type=self.compute_type,
                )
            self._model = _CACHE[key]
        return self._model

    # ---- 解码与识别 ----

    @staticmethod
    def _to_float32k(audio: bytes):
        """自描述 WAV → float32 单声道 16kHz(whisper 的输入要求)。"""
        import numpy as np

        with wave.open(io.BytesIO(audio), "rb") as w:
            rate, channels, width = (w.getframerate(), w.getnchannels(),
                                     w.getsampwidth())
            frames = w.readframes(w.getnframes())
        if width != 2:
            raise UnsupportedAudioError(
                f"分片位宽 {width*8} bit,当前只支持 16-bit PCM(请用标准 WAV 录音)"
            )
        samples = np.frombuffer(frames, dtype="<i2").astype("float32") / 32768.0
        if samples.size == 0:
            # 空分片必须在重采样前返回:np.interp 对空 xp 会直接抛错
            return samples
        if channels > 1:  # 多声道取均值(单声道混音)
            samples = samples.reshape(-1, channels).mean(axis=1)
        if rate != 16000:  # 线性重采样(8k→16k 不增加信息,只是满足输入约定)
            target = int(round(samples.shape[0] * 16000 / rate))
            idx = (np.arange(target) * (samples.shape[0] - 1) / max(target - 1, 1))
            samples = np.interp(idx, np.arange(samples.shape[0]), samples)
        return samples.astype("float32")

    def transcribe_slice(self, audio: bytes, slice_index: int) -> ASRSlice:
        samples = self._to_float32k(audio)
        if samples.size == 0:
            return ASRSlice(speaker=None, text="", is_mock=False)
        model = self._load()
        kwargs = {"beam_size": 1, "vad_filter": True}
        if self.language:
            kwargs["language"] = self.language
        segments, _info = model.transcribe(samples, **kwargs)
        text = "".join(seg.text for seg in segments).strip()
        # 时间信息一律来自切片边界(prepare),不采用 whisper 的段内时间戳
        return ASRSlice(speaker=None, text=text, is_mock=False)
