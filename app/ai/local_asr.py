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
    """分片不是可识别的 PCM 格式(当前支持 16/24/32-bit 整数 PCM)。"""


def _pcm_to_float(frames: bytes, width: int):
    """整数 PCM → float32[-1,1]。实测验证过 16/24-bit;32-bit 按同规则实现。

    24-bit 为 3 字节小端有符号:拼接后按 2^23 归一。浮点 WAV(comptype 非 NONE)
    不在支持范围 —— Python wave 模块会按整数误读,宁可报错不猜。
    """
    import numpy as np

    if width == 2:
        return np.frombuffer(frames, dtype="<i2").astype("float32") / 32768.0
    if width == 3:
        b = np.frombuffer(frames, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = np.where(v >= (1 << 23), v - (1 << 24), v)
        return v.astype("float32") / float(1 << 23)
    if width == 4:
        return (np.frombuffer(frames, dtype="<i4").astype("float32")
                / 2147483648.0)
    raise UnsupportedAudioError(
        f"分片位宽 {width*8} bit 不支持(已验证:16/24-bit 整数 PCM)"
    )


# 固定、版本化的解码偏置词表(D-54):只含简体中文与医疗设备采购的**通用**领域词,
# 不含任何单次录音的原文、答案、日期、金额,也不根据识别错误临时增删专名。
# 修改词表必须升版本号并记录,否则跨次结果不可比。
GLOSSARY_VERSION = "zh-medproc-v1"
INITIAL_PROMPT = (
    "以下是简体中文的医疗设备采购与售后会谈。常见词:监护仪、超声、呼吸机、"
    "麻醉机、内窥镜、注册证、检测报告、保修、维保、售后、交付、验收、发票、"
    "合同、耗材、试剂、护士长、科主任、院长、设备科、经理、总监。"
    "请用简体中文输出。"
)


class LocalWhisperASR(ASRProvider):
    name = "local-whisper"
    is_mock = False
    prompt_version = GLOSSARY_VERSION

    def __init__(self, settings: Settings):
        self.model_size = settings.asr_model
        self.device = settings.asr_device or "cpu"
        self.compute_type = settings.asr_compute_type
        self.language = (settings.asr_language or "").strip() or None
        self.model_name = f"faster-whisper:{self.model_size}@{self.compute_type}"
        self.runtime_device = f"local:{self.device}:{self.compute_type}"
        self.hf_endpoint = settings.hf_endpoint
        self._model = None

    # ---- 成本 ----

    def estimate_cost(self, call: str, units: dict) -> float | None:
        """0 = 外部 API 费用为零(算力成本另由 duration_ms/device 体现)。"""
        return 0.0

    def usage_for(self, call: str, units: dict, result) -> ProviderUsage | None:
        """用量留 NULL:音频按**字节/时长**计,不存在 token 口径(D-58)。
        此前把 audio_bytes 写进 tokens_in,台账里就出现"10 秒切片 = 288 万 token"
        这种与 DeepSeek 真 token 数同列不同单位的数据,任何汇总都会算错。
        音频量仍可从 transcribe_slice 的 start_ms/end_ms 与原件大小还原,
        本地算力开销由 duration_ms + device 体现(D-52)。"""
        return ProviderUsage(basis="estimated", cost_cents=0.0,
                             tokens_in=None, tokens_out=None)

    # ---- 模型加载 ----

    def _load(self):
        if self._model is not None:
            return self._model
        with _LOCK:
            key = (self.model_size, self.device, self.compute_type)
            if key not in _CACHE:
                if self.hf_endpoint:
                    # .env 的值不会自动进入 os.environ,故显式应用(已设则不覆盖)
                    os.environ.setdefault("HF_ENDPOINT", self.hf_endpoint)
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
        samples = _pcm_to_float(frames, width)
        if samples.size == 0:
            # 空分片必须在重采样前返回:np.interp 对空 xp 会直接抛错
            return samples
        if channels > 1:  # 多声道取均值(单声道混音)
            samples = samples.reshape(-1, channels).mean(axis=1)
        if rate != 16000:  # 线性重采样(48k/8k→16k,只是满足输入约定)
            target = int(round(samples.shape[0] * 16000 / rate))
            idx = (np.arange(target) * (samples.shape[0] - 1) / max(target - 1, 1))
            samples = np.interp(idx, np.arange(samples.shape[0]), samples)
        return samples.astype("float32")

    def transcribe_slice(self, audio: bytes, slice_index: int) -> ASRSlice:
        samples = self._to_float32k(audio)
        if samples.size == 0:
            return ASRSlice(speaker=None, text="", is_mock=False)
        model = self._load()
        kwargs = {"beam_size": 1, "vad_filter": True,
                  "initial_prompt": INITIAL_PROMPT}
        if self.language:
            kwargs["language"] = self.language
        segments, _info = model.transcribe(samples, **kwargs)
        text = "".join(seg.text for seg in segments).strip()
        # 时间信息一律来自切片边界(prepare),不采用 whisper 的段内时间戳
        return ASRSlice(speaker=None, text=text, is_mock=False)
