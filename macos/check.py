"""Local Mac checks. Model downloads/recording only happen with explicit flags."""
from __future__ import annotations

import argparse
import datetime as dt
import importlib
import platform
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(description="声年 Mac 环境检查（默认不录音、不下载模型、不调用 AI）")
    parser.add_argument("--load-model", action="store_true", help="加载/首次下载 ASR 与声纹模型")
    parser.add_argument("--record-seconds", type=int, default=0, help="明确录制 1–60 秒测试音频，保存到本机诊断目录")
    parser.add_argument("--transcribe", type=Path, help="用本地模型转写指定 WAV（首次可能下载模型）")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("该检查面向 Mac 实机；Windows 使用 pytest 做回归验证")
    if args.record_seconds and not 1 <= args.record_seconds <= 60:
        parser.error("录音时长必须为 1–60 秒")

    failures = []
    def check(name, func):
        try:
            result = func()
            print(f"[通过] {name}" + (f"：{result}" if result is not None else ""))
            return result
        except Exception as exc:
            failures.append(name)
            print(f"[失败] {name}：{type(exc).__name__}: {exc}")
            return None

    print(f"macOS {platform.mac_ver()[0]} / {platform.machine()} / Python {platform.python_version()}")
    if platform.machine() != "arm64" or sys.version_info[:2] != (3, 12):
        failures.append("需要原生 arm64 Python 3.12")
    for module in ("PySide6.QtWidgets", "sounddevice", "webrtcvad", "watchdog.observers", "openai", "torch", "torchaudio", "funasr"):
        check(module, lambda module=module: (importlib.import_module(module), "已导入")[1])

    from common import ROOT
    from audio_import import _ffmpeg_executable
    from platform_support import microphone_permission_hint, locked_role_pid
    print(f"数据目录：{ROOT}")
    ffmpeg = _ffmpeg_executable()
    print(f"FFmpeg：{ffmpeg or '未找到（导入压缩音频需要）'}")
    if not ffmpeg:
        failures.append("FFmpeg")

    def devices():
        import sounddevice as sd
        inputs = [d for d in sd.query_devices() if d["max_input_channels"] > 0]
        if not inputs:
            raise RuntimeError(microphone_permission_hint())
        return "、".join(d["name"] for d in inputs)
    check("输入设备", devices)

    from ai_gateway import provider_api_key
    print("DeepSeek API：" + ("已配置（未发请求）" if provider_api_key("DEEPSEEK_API_KEY") else "未配置；不影响本地录音转写"))
    if args.load_model:
        from transcriber import get_model, get_sv_model
        check("加载本地 ASR 模型（CPU）", lambda: (get_model(), "成功")[1])
        check("加载本地声纹模型（CPU）", lambda: (get_sv_model(), "成功")[1])

    if args.record_seconds:
        def record():
            import sounddevice as sd
            import numpy as np
            import wave
            import recorder
            if locked_role_pid(ROOT, "recorder"):
                raise RuntimeError("请先停止声年的常驻录音，再做麦克风诊断")
            selected = recorder.find_device()
            if not selected:
                raise RuntimeError(microphone_permission_hint())
            index, name, _ = selected
            rates = recorder._supported_capture_rates(index, sd.query_devices(index))
            if not rates:
                raise RuntimeError("设备采样率不可用")
            rate = rates[0]
            print(f"正在录制 {args.record_seconds} 秒，请讲话。设备：{name}")
            data = sd.rec(int(args.record_seconds * rate), samplerate=rate, channels=1, dtype="int16", device=index)
            sd.wait()
            directory = ROOT / "runtime" / "macos-check"
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / f"microphone-{dt.datetime.now():%Y%m%d-%H%M%S-%f}.wav"
            with wave.open(str(target), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(rate)
                wav.writeframes(data.tobytes())
            peak = int(np.abs(data.astype(np.int32)).max())
            if peak == 0:
                raise RuntimeError(f"音频全静音。{microphone_permission_hint()}；测试文件：{target}")
            return f"{target}（峰值 {peak}）"
        check("麦克风实际录音", record)

    if args.transcribe:
        def transcribe():
            from transcriber import transcribe_wav
            source = args.transcribe.expanduser().resolve(strict=True)
            started = time.monotonic()
            text = transcribe_wav(source)
            if not text:
                raise RuntimeError("未识别到文字；请使用有清晰讲话的 WAV")
            return f"耗时 {time.monotonic() - started:.1f}s\n{text}"
        check("本地转写", transcribe)
    print("检查完成。" + (f"需要处理：{', '.join(failures)}" if failures else "基础检查通过；录音、休眠恢复和长时间使用请继续按说明验收。"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
