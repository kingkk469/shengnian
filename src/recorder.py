"""DJI Mic 常驻录音器。

- 找名字含 'Wireless Microphone RX' 的输入设备
- 16 kHz 单声道,20 ms 帧
- WebRTC VAD 切片,静音 800 ms 切一段,单段最长 60s
- 每段写 raw/YYYY-MM-DD/HH-MM-SS-{seq}.wav
- 拔线自动重连(每 5s 重试)
- D:\\voice-journal\\.paused 存在时丢弃输入帧
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import math
import struct
import sys
import time
import wave
from pathlib import Path

import ctypes
import numpy as np
import sounddevice as sd
import webrtcvad

from common import CONFIG, ROOT, day_dir, iso_now, pause_flag, setup_logger, write_recorder_status
from platform_support import RoleLock, install_worker_signals, microphone_permission_hint, prevent_macos_sleep


# Windows ES_* 标志,告诉系统"我在工作请别休眠"
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_AWAYMODE_REQUIRED = 0x00000040


def prevent_sleep(on: bool) -> None:
    if sys.platform == "darwin":
        try:
            prevent_macos_sleep(on)
        except OSError as exc:
            log.warning("无法设置录音防休眠: %s", exc)
        return
    if sys.platform != "win32":
        return
    try:
        if on:
            ctypes.windll.kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED
            )
        else:
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    except Exception:
        pass

log = setup_logger("recorder")

SR = int(CONFIG["audio"]["sample_rate"])  # 16000
CH = int(CONFIG["audio"]["channels"])      # 1
FRAME_MS = 20
FRAME_LEN = SR * FRAME_MS // 1000          # 320 samples
SILENCE_MS = int(CONFIG["audio"]["silence_split_ms"])
SILENCE_FRAMES = SILENCE_MS // FRAME_MS
MAX_SEG_FRAMES = int(CONFIG["audio"]["max_segment_sec"] * 1000) // FRAME_MS
MIN_SEG_FRAMES = int(CONFIG["audio"]["min_segment_sec"] * 1000) // FRAME_MS
DEVICE_KW = [k.lower() for k in CONFIG["audio"]["device_name_keywords"]]
FALLBACK_KW = [k.lower() for k in CONFIG["audio"].get("fallback_devices", [])]
VAD = webrtcvad.Vad(int(CONFIG["audio"]["vad_mode"]))


def _write_state(state: str, **extra) -> None:
    """把启动、等待、录音和错误状态都写给 launcher，避免界面永久卡在“启动中”。"""
    payload = {
        "ts": time.time(),
        "state": state,
        "has_device": None,
        "device_name": "",
        "paused": is_paused(),
    }
    payload.update(extra)
    write_recorder_status(payload)


def _candidate_sample_rates(device: dict | None = None) -> list[int]:
    """按产品格式、设备原生格式和 Windows 常见格式生成候选采样率。"""
    rates = [SR]
    if device:
        try:
            native = int(round(float(device.get("default_samplerate") or 0)))
        except (TypeError, ValueError):
            native = 0
        if native > 0:
            rates.append(native)
    rates.extend((48000, 44100, 32000))
    return list(dict.fromkeys(rate for rate in rates if rate > 0))


def _supported_capture_rates(device_index: int, device: dict | None = None) -> list[int]:
    """返回设备可以声明打开的采样率；真正启动仍由 `_open_input_stream` 验证。"""
    supported = []
    for sample_rate in _candidate_sample_rates(device):
        try:
            sd.check_input_settings(
                device=device_index,
                samplerate=sample_rate,
                channels=CH,
                dtype="int16",
            )
            supported.append(sample_rate)
        except Exception as exc:
            log.warning(
                "录音设备 idx=%d 不支持 %d Hz: %s",
                device_index,
                sample_rate,
                exc,
            )
    return supported


def _device_supports_capture(device_index: int, device: dict | None = None) -> bool:
    """确认候选设备至少支持一种可转换为 16 kHz 的录音格式。"""
    return bool(_supported_capture_rates(device_index, device))


def _first_supported(
    candidates: list[tuple[int, dict]],
    excluded_indices: set[int] | None = None,
) -> tuple[int, str] | None:
    excluded_indices = excluded_indices or set()
    seen: set[int] = set()
    for index, device in candidates:
        if index in seen or index in excluded_indices:
            continue
        seen.add(index)
        if _device_supports_capture(index, device):
            return index, str(device.get("name") or f"输入设备 {index}")
    return None


def _load_preferred() -> dict:
    """读 launcher 写的手动指定麦。{mode:auto} 或 {mode:manual, name:设备全名}。"""
    try:
        import json as _j
        from pathlib import Path as _P
        p = ROOT / "runtime" / "preferred_device.json"
        if p.exists():
            return _j.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"mode": "auto"}


def find_device(excluded_indices: set[int] | None = None) -> tuple[int, str, bool] | None:
    """扫描可用输入设备，按优先级返回 (index, name, is_primary)。

    优先级：手动指定(若该设备在) > 主设备(DJI) > fallback_devices 顺序 > None
    is_primary=True 表示匹配到主设备，False 表示使用备用设备。
    """
    try:
        devices = sd.query_devices()
    except Exception as e:
        log.warning("query_devices 失败: %s", e)
        return None

    input_devs = [(i, d) for i, d in enumerate(devices)
                  if d.get("max_input_channels", 0) > 0]

    try:
        default_input = int(sd.default.device[0])
    except (TypeError, ValueError, IndexError):
        default_input = -1

    def default_first(items: list[tuple[int, dict]]) -> list[tuple[int, dict]]:
        return sorted(items, key=lambda item: 0 if item[0] == default_input else 1)

    # 0. 手动指定优先(用户在 launcher 选了某个麦)。同名设备可能属于不同 Host API，
    # 优先选系统默认且实际支持 16 kHz 的那一个。
    pref = _load_preferred()
    if pref.get("mode") == "manual" and pref.get("name"):
        manual = default_first([
            (i, d) for i, d in input_devs if d.get("name") == pref["name"]
        ])
        selected = _first_supported(manual, excluded_indices)
        if selected:
            i, name = selected
            is_p = any(k in name.lower() for k in DEVICE_KW)
            return i, name, is_p

    # 1. 先找主设备
    primary = default_first([
        (i, d) for i, d in input_devs
        if any(k in (d.get("name") or "").lower() for k in DEVICE_KW)
    ])
    selected = _first_supported(primary, excluded_indices)
    if selected:
        return selected[0], selected[1], True

    # 2. 按 fallback 优先级顺序找备用
    for kw in FALLBACK_KW:
        fallback = default_first([
            (i, d) for i, d in input_devs if kw in (d.get("name") or "").lower()
        ])
        selected = _first_supported(fallback, excluded_indices)
        if selected:
            return selected[0], selected[1], False

    # 3. 使用系统默认输入设备（包括 Mac 内置麦克风）。
    default_candidates = [(i, d) for i, d in input_devs if i == default_input]
    selected = _first_supported(default_candidates, excluded_indices)
    if selected:
        return selected[0], selected[1], False
    if input_devs and not DEVICE_KW and not FALLBACK_KW:
        selected = _first_supported(default_first(input_devs), excluded_indices)
        if selected:
            return selected[0], selected[1], False

    return None


_RECORDER_MUTEX_HANDLE = None


def _acquire_recorder_mutex() -> bool:
    """用 Windows 命名互斥锁保证单实例，不再模糊匹配并杀死其他进程。"""
    global _RECORDER_MUTEX_HANDLE
    if sys.platform != "win32":
        return True
    try:
        handle = ctypes.windll.kernel32.CreateMutexW(
            None, False, "Local\\KingVoiceJournalRecorder"
        )
        if not handle:
            log.warning("无法创建 recorder 单实例锁，继续启动")
            return True
        already_exists = ctypes.windll.kernel32.GetLastError() == 183
        if already_exists:
            ctypes.windll.kernel32.CloseHandle(handle)
            return False
        _RECORDER_MUTEX_HANDLE = handle
        return True
    except Exception as exc:
        log.warning("创建 recorder 单实例锁失败，继续启动: %s", exc)
        return True


def _device_still_present(device_name: str) -> bool:
    """检查指定名称的录音设备是否还在系统里（用于检测 DJI 是否被拔/没电）。"""
    try:
        devices = sd.query_devices()
        for d in devices:
            if d.get("max_input_channels", 0) > 0:
                if d.get("name") == device_name:
                    return True
        return False
    except Exception as e:
        log.warning("设备健康检查失败: %s", e)
        return True   # 查询失败时假设还在，避免误切换


def write_wav(path: Path, frames: list[bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(CH)
        w.setsampwidth(2)  # int16
        w.setframerate(SR)
        w.writeframes(b"".join(frames))


def is_paused() -> bool:
    return pause_flag().exists()


def _device_info(device_index: int) -> dict:
    try:
        info = sd.query_devices(device_index)
        if isinstance(info, dict):
            return info
    except (TypeError, ValueError):
        pass
    try:
        devices = sd.query_devices()
        return dict(devices[device_index])
    except Exception:
        return {}


def _resample_frame(frame_bytes: bytes, capture_frames: int, capture_sr: int) -> bytes:
    """把任意常见输入采样率的 20 ms 单声道帧转换成 16 kHz / 320 samples。"""
    samples = np.frombuffer(frame_bytes, dtype=np.int16)
    if capture_frames > 0 and samples.size > capture_frames:
        samples = samples[:capture_frames]
    if samples.size == FRAME_LEN and capture_sr == SR:
        return samples.tobytes()
    if samples.size == 0:
        return bytes(FRAME_LEN * 2)
    if samples.size == 1:
        return np.full(FRAME_LEN, samples[0], dtype=np.int16).tobytes()
    source_points = np.arange(samples.size, dtype=np.float64)
    target_points = np.linspace(0, samples.size - 1, FRAME_LEN, dtype=np.float64)
    converted = np.interp(target_points, source_points, samples).astype(np.int16)
    return converted.tobytes()


def _open_input_stream(device_index: int, callback):
    """实际启动输入流；16 kHz 失败时自动尝试设备原生及常见采样率。"""
    device = _device_info(device_index)
    rates = _supported_capture_rates(device_index, device)
    if not rates:
        rates = _candidate_sample_rates(device)
    errors = []
    for capture_sr in rates:
        blocksize = max(1, int(round(capture_sr * FRAME_MS / 1000)))

        def stream_callback(indata, frames, time_info, status, _rate=capture_sr):
            callback(indata, frames, time_info, status, _rate)

        stream = None
        try:
            stream = sd.InputStream(
                device=device_index,
                samplerate=capture_sr,
                channels=CH,
                dtype="int16",
                blocksize=blocksize,
                callback=stream_callback,
            )
            stream.start()
            log.info(
                "录音流已打开 device_idx=%d capture_sr=%d target_sr=%d blocksize=%d",
                device_index,
                capture_sr,
                SR,
                blocksize,
            )
            return stream, capture_sr
        except Exception as exc:
            errors.append(f"{capture_sr} Hz: {exc}")
            log.warning(
                "打开录音设备 idx=%d @ %d Hz 失败: %s",
                device_index,
                capture_sr,
                exc,
            )
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
    detail = "；".join(errors[-4:]) or "没有可用格式"
    raise RuntimeError(f"无法打开麦克风（已尝试多种采样率）：{detail}")


def _friendly_recorder_error(exc: Exception | str) -> str:
    detail = str(exc)
    low = detail.lower()
    if "sample rate" in low or "samplerate" in low or "format" in low:
        return "麦克风格式不兼容，正在尝试其他输入模式"
    if (
        "device unavailable" in low
        or "unanticipated host error" in low
        or "access" in low
        or "permission" in low
    ):
        return "麦克风被占用或系统权限未开启，请关闭会议软件并检查权限"
    if "no default input" in low or "invalid device" in low:
        return "默认麦克风不可用，请点击“麦克风”切换设备"
    return "麦克风打开失败，请点击“麦克风”切换设备"


def run_once(device_index: int, device_name: str, duration: int | None = None) -> None:
    """一次录音 session;遇到设备错误抛出由外层重试。

    把所有可变状态放到 state dict 里,避免 nonlocal + sounddevice cffi callback
    的闭包陷阱(否则 callback 里读未赋值的 seg 会 UnboundLocalError)。
    """
    log.info("开始录音 device=%s (idx=%d) sr=%d", device_name, device_index, SR)
    state = {
        "seg": [],              # type: list[bytes]
        "voiced": 0,
        "silence": 0,
        "in_segment": False,
        "seq": 0,
        "last_status_log": time.time(),
        "last_voice_at": time.time(),
        "last_frame_at": 0.0,
        "last_signal_at": time.time(),
        "level_rms": 0,
        "input_frames": 0,
        "voice_detected": False,
    }
    ring: collections.deque[bytes] = collections.deque(maxlen=10)  # pre-roll 200ms

    def flush() -> None:
        seg = state["seg"]
        if len(seg) >= MIN_SEG_FRAMES:
            today = dt.date.today()
            ts = dt.datetime.now()
            name = f"{ts.strftime('%H-%M-%S')}-{state['seq']:04d}.wav"
            state["seq"] += 1
            path = day_dir("raw", today) / name
            write_wav(path, list(seg))
            log.info("写入 %s (%d frames, %.1fs)", path.name, len(seg), len(seg) * FRAME_MS / 1000)
        state["seg"] = []
        state["voiced"] = 0
        state["silence"] = 0
        state["in_segment"] = False

    def callback(indata, frames, time_info, status, capture_sr):
        try:
            if status:
                log.warning("sd status: %s", status)
            if is_paused():
                return
            frame_bytes = _resample_frame(indata.tobytes(), frames, capture_sr)
            now = time.time()
            state["last_frame_at"] = now
            state["input_frames"] += 1
            # 每 100 ms 计算一次输入电平。这样用户能区分“设备已连接”和“真的有声音”。
            if state["input_frames"] % 5 == 0:
                samples = memoryview(frame_bytes).cast("h")
                if samples:
                    rms = int(math.sqrt(sum(int(v) * int(v) for v in samples) / len(samples)))
                    state["level_rms"] = rms
                    if rms >= 25:
                        state["last_signal_at"] = now
            is_speech = VAD.is_speech(frame_bytes, SR)
            ring.append(frame_bytes)

            if state["in_segment"]:
                state["seg"].append(frame_bytes)
                if is_speech:
                    state["silence"] = 0
                    state["voiced"] += 1
                    state["last_voice_at"] = now
                    state["voice_detected"] = True
                else:
                    state["silence"] += 1
                    if state["silence"] >= SILENCE_FRAMES:
                        flush()
                if len(state["seg"]) >= MAX_SEG_FRAMES:
                    flush()
            else:
                if is_speech:
                    state["seg"] = list(ring)
                    state["in_segment"] = True
                    state["voiced"] = 1
                    state["silence"] = 0
                    state["last_voice_at"] = now
                    state["voice_detected"] = True

            if now - state["last_status_log"] >= 60:
                state["last_status_log"] = now
                idle = now - state["last_voice_at"]
                if idle > 60:
                    log.info("过去 60s 未检测到语音 (静音 %.0fs)", idle)
        except Exception as e:
            # callback 抛异常 sounddevice 会弹 Python-CFFI error 弹窗,这里吞掉记日志
            log.exception("callback 异常: %s", e)

    started = time.time()
    _write_state(
        "opening_device",
        has_device=True,
        device_name=device_name,
        is_primary=any(k in device_name.lower() for k in DEVICE_KW),
    )
    prevent_sleep(True)
    stream = None
    try:
        stream, capture_sr = _open_input_stream(device_index, callback)
        last_heartbeat = 0.0
        last_device_check = time.time()
        while True:
            sd.sleep(500)
            # 每秒心跳一次
            now = time.time()
            if now - last_heartbeat >= 1.0:
                _dn_lower = device_name.lower()
                write_recorder_status({
                    "ts": now,
                    "state": "recording",
                    "has_device": True,
                    "device_name": device_name,
                    "is_primary": any(k in _dn_lower for k in DEVICE_KW),
                    "capture_sample_rate": capture_sr,
                    "target_sample_rate": SR,
                    "last_voice_at": state["last_voice_at"],
                    "idle_sec": now - state["last_voice_at"],
                    "in_segment": state["in_segment"],
                    "total_segments": state["seq"],
                    "paused": is_paused(),
                    "input_active": now - state["last_frame_at"] < 2.0,
                    "level_rms": state["level_rms"],
                    "signal_idle_sec": now - state["last_signal_at"],
                    "voice_detected": state["voice_detected"],
                })
                last_heartbeat = now

            # ── 设备健康检查（每 2 秒）──
            if now - last_device_check >= 2:
                last_device_check = now
                if sys.platform == "darwin" and now - (state["last_frame_at"] or started) > 8:
                    flush()
                    raise RuntimeError("录音流长时间未收到数据，重新连接麦克风")
                if not _device_still_present(device_name):
                    flush()
                    raise RuntimeError(
                        f"设备 '{device_name}' 从系统消失（可能拔出/没电），切换"
                    )
                _want = find_device()
                if _want and _want[1] != device_name:
                    flush()
                    raise RuntimeError(f"切换目标麦: {_want[1]}")

            if duration is not None and now - started >= duration:
                log.info("达到 --duration %ds,优雅退出并 flush", duration)
                flush()
                return
    except KeyboardInterrupt:
        log.info("收到 Ctrl+C,flush 当前段")
        flush()
        raise
    finally:
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        prevent_sleep(False)


def meter_mode(seconds: int) -> None:
    """调试模式:打印每 0.5 秒的音量 RMS 和 VAD 命中比例,不写盘。"""
    import numpy as np

    dev = find_device()
    if dev is None:
        log.error("找不到任何可用录音设备")
        return
    idx, name, is_primary = dev
    log.info("调试模式 device=%s idx=%d 持续 %ds (主设备=%s)", name, idx, seconds, is_primary)

    bucket = {"rms": [], "voiced": 0, "total": 0}
    start = time.time()
    last_print = start

    def cb(indata, frames, time_info, status):
        nonlocal last_print
        if status:
            log.warning("status=%s", status)
        arr = np.frombuffer(indata.tobytes(), dtype=np.int16).astype(np.float32)
        rms = float(np.sqrt((arr * arr).mean()))
        bucket["rms"].append(rms)
        try:
            is_speech = VAD.is_speech(indata.tobytes(), SR)
        except Exception:
            is_speech = False
        bucket["total"] += 1
        if is_speech:
            bucket["voiced"] += 1
        now = time.time()
        if now - last_print >= 0.5:
            avg_rms = sum(bucket["rms"]) / max(1, len(bucket["rms"]))
            ratio = bucket["voiced"] / max(1, bucket["total"])
            bar = "#" * min(40, int(avg_rms / 50))
            print(f"  rms={avg_rms:7.1f}  voiced={ratio*100:5.1f}%  {bar}", flush=True)
            bucket["rms"].clear()
            bucket["voiced"] = 0
            bucket["total"] = 0
            last_print = now

    with sd.InputStream(device=idx, samplerate=SR, channels=CH, dtype="int16",
                        blocksize=FRAME_LEN, callback=cb):
        sd.sleep(seconds * 1000)
    log.info("调试模式结束")


def _run() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list-devices", action="store_true", help="列出所有输入设备并退出")
    parser.add_argument("--meter", type=int, metavar="SEC",
                        help="调试:跑 SEC 秒,打印音量和 VAD 命中,不写盘")
    parser.add_argument("--duration", type=int, metavar="SEC",
                        help="只跑 SEC 秒后优雅退出(测试用,正式服务别加)")
    args = parser.parse_args()

    if args.list_devices:
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_input_channels", 0) > 0:
                print(f"{i:3d}  {d['name']}  ch_in={d['max_input_channels']}  sr={d['default_samplerate']}")
        return

    if args.meter:
        meter_mode(args.meter)
        return

    log.info("recorder 启动,pause flag=%s", pause_flag())
    if not _acquire_recorder_mutex():
        log.info("已有 recorder 实例在运行，本次启动直接退出")
        return
    _write_state("starting", message="正在检查麦克风")

    # 切换延迟：找不到设备时等 2s 再试；录音中断时等 1s 重连
    no_device_backoff = 2
    reconnect_backoff = 1
    _last_was_primary = None   # 记录上次用的是不是主设备,用于回切提示
    failed_until: dict[int, float] = {}
    while True:
        now = time.time()
        failed_until = {
            index: until for index, until in failed_until.items() if until > now
        }
        dev = find_device(set(failed_until))
        if dev is None:
            log.warning(
                "未找到可立即打开的录音设备(主设备关键字=%s, 暂时跳过=%s),%ds 后重试",
                DEVICE_KW,
                sorted(failed_until),
                no_device_backoff,
            )
            _write_state(
                "waiting_device",
                has_device=False,
                error="没有找到可用麦克风。" + microphone_permission_hint(),
                user_error="没有找到可用麦克风，请检查系统权限或点击“麦克风”切换",
                retry_in_sec=no_device_backoff,
            )
            time.sleep(no_device_backoff)
            continue

        idx, name, is_primary = dev

        # 状态变化时打印提示（不用 emoji，避免 Windows GBK 终端编码报错）
        if _last_was_primary is None:
            if is_primary:
                log.info("[主设备] 使用: %s", name)
            else:
                log.warning("[备用设备] 主设备未找到，降级使用: %s", name)
        elif is_primary and not _last_was_primary:
            log.info("[主设备回归] 切回: %s", name)
        elif not is_primary and _last_was_primary:
            log.warning("[备用设备] 主设备断开，切换到: %s", name)

        _last_was_primary = is_primary

        try:
            run_once(idx, name, duration=args.duration)
            if args.duration:
                return
        except KeyboardInterrupt:
            log.info("收到 Ctrl+C,退出")
            return
        except Exception as e:
            log.warning("录音中断: %s,%ds 后重连", e, reconnect_backoff)
            failed_until[idx] = time.time() + 15
            _write_state(
                "error",
                has_device=_device_still_present(name),
                device_name=name,
                is_primary=is_primary,
                error=str(e)[:500],
                user_error=_friendly_recorder_error(e),
                failed_device_index=idx,
                retry_in_sec=max(reconnect_backoff, 2),
            )
            _last_was_primary = None   # 重置状态,下次重新检测
            time.sleep(reconnect_backoff)


def main() -> None:
    # Listing devices does not own the recorder; every recording mode does.
    if "--list-devices" in sys.argv:
        _run()
        return
    lock = RoleLock(ROOT, "recorder")
    if not lock.acquire():
        log.info("已有 recorder 实例在运行，本次启动直接退出")
        return
    install_worker_signals()
    try:
        _run()
    except KeyboardInterrupt:
        log.info("录音服务已停止")
    finally:
        prevent_sleep(False)
        lock.release()


if __name__ == "__main__":
    main()
