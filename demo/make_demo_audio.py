"""生成"可被真实 ASR 识别"的演示音频(离线,本机 TTS)。

为什么需要它:仓库里原有的 demo/media/*.wav 是**静音占位**,时长只用来决定
切片数与时间轴;接上真实 ASR 后,静音只会得到空文本,无法验证转写链路。
本脚本用 Windows 中文语音(Microsoft Huihui Desktop)逐句合成,
每句补齐到正好 10 秒 —— 与 slice_seconds=10 对齐,于是一个切片恰好一句,
时间位置仍然只来自切片边界(不是模型猜的)。

诚实标注:这是**程序合成的语音**,不是真人会谈录音。它用于验证
"上传 → 真实 ASR → 纪要 → 检索 → 问答"链路是否成立,
不代表真实场景下的录音质量、口音、噪声与多人重叠表现。
"""
import argparse
import io
import os
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

SLICE_SECONDS = 10
RATE = 8000          # 与既有演示音频一致(8kHz 单声道 16-bit)
VOICE = "Microsoft Huihui Desktop"
RATE_ADJUST = -2     # 略放慢,提高识别准确率

_PS = r"""
Add-Type -AssemblyName System.Speech
$text = (Get-Content -Raw -Encoding UTF8 -LiteralPath $env:TTS_TEXT_FILE).Trim()
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.SelectVoice($env:TTS_VOICE)          # 名称直接传,失败会抛异常(不静默换音色)
$s.Rate = [int]$env:TTS_RATE
$s.SetOutputToWaveFile($env:TTS_OUT_FILE)
$s.Speak($text)                          # Speak 是同步的,写完即落盘
$s.Dispose()
"""


def synth_line(text: str, out_path: Path) -> None:
    """一句 → 一个 WAV(失败即抛错,不静默跳过——跳过会让切片数与行数错位)。"""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                    encoding="utf-8") as tf:
        tf.write(text)
        text_file = Path(tf.name)
    env = {**os.environ, "TTS_TEXT_FILE": str(text_file),
           "TTS_OUT_FILE": str(out_path), "TTS_VOICE": VOICE,
           "TTS_RATE": str(RATE_ADJUST)}
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-Command", _PS],
            env=env, capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0 or not out_path.exists():
            raise RuntimeError(f"TTS 失败: {r.stderr[:300] or r.stdout[:300]}")
    finally:
        text_file.unlink(missing_ok=True)


def to_slice_pcm(src: Path) -> bytes:
    """任意 TTS 输出 → 8kHz 单声道 16-bit **裸 PCM**,补齐/截断到正好 SLICE_SECONDS 秒。

    返回裸 PCM 而不是 WAV:多个带 RIFF 头的文件直接拼接会得到畸形音频
    (播放器只认第一个头,时长看起来就只有 10 秒)。整段只在最后封一次头。
    """
    import numpy as np

    with wave.open(str(src), "rb") as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        frames = w.readframes(w.getnframes())
    samples = np.frombuffer(frames, dtype="<i2").astype("float32") / 32768.0
    if ch > 1:
        samples = samples.reshape(-1, ch).mean(axis=1)
    if rate != RATE:
        target = int(round(samples.shape[0] * RATE / rate))
        idx = np.arange(target) * (samples.shape[0] - 1) / max(target - 1, 1)
        samples = np.interp(idx, np.arange(samples.shape[0]), samples)
    need = RATE * SLICE_SECONDS
    if samples.shape[0] > need:
        samples = samples[:need]                     # 超长截断(宁可少不可错位)
    else:
        samples = np.pad(samples, (0, need - samples.shape[0]))  # 尾部静音补齐
    return (samples * 32767.0).astype("<i2").tobytes()


def build(case_id: str, lines: list[tuple[str, str]], out_dir: Path) -> Path:
    import numpy as np  # noqa: F401  (提前失败:缺依赖时不要白跑 TTS)

    parts = []
    tmp = Path(tempfile.mkdtemp(prefix="tts_"))
    for i, (_speaker, text) in enumerate(lines, 1):
        wav = tmp / f"line{i:02d}.wav"
        synth_line(text, wav)
        parts.append(to_slice_pcm(wav))
        print(f"  [{case_id}] {i}/{len(lines)} 合成完成", flush=True)
    out_path = out_dir / f"{case_id}_speech.wav"
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(b"".join(parts))
    out_path.write_bytes(buf.getvalue())
    return out_path


def main():
    ap = argparse.ArgumentParser(description="合成可识别的演示语音")
    ap.add_argument("--case", default="all", choices=["all", "case_normal", "case_boundary"])
    ap.add_argument("--out", default=str(REPO_ROOT / "demo" / "media"))
    args = ap.parse_args()

    from demo.seed_demo import DEMO  # 复用同一份台词,避免两处文案漂移

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for spec in DEMO:
        ms = spec["meeting"]
        if args.case != "all" and ms["id"] != args.case:
            continue
        path = build(ms["id"], ms["transcript"], out_dir)
        with wave.open(str(path), "rb") as w:
            secs = w.getnframes() / w.getframerate()
        print(f"[tts] {path.name}: {secs:.0f}s = {int(secs // SLICE_SECONDS)} 个切片"
              f"(台词 {len(ms['transcript'])} 行)", flush=True)


if __name__ == "__main__":
    main()
