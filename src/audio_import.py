"""本地录音导入、时间识别与音频标准化。

设计边界：
- 原文件只复制到声年本地目录，不覆盖、不上传。
- 可信的媒体时间或文件名时间直接用于归档；文件系统时间需要用户确认。
- 转写工作副本统一为 16 kHz / 单声道 / 16-bit PCM WAV。
- 用内容 SHA-256 去重，避免同一段会议录音被重复转写。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


SUPPORTED_AUDIO_SUFFIXES = {
    ".wav",
    ".mp3",
    ".m4a",
    ".aac",
    ".flac",
    ".ogg",
    ".opus",
    ".wma",
    ".mp4",
    ".mov",
}


@dataclass(frozen=True)
class RecordingTimeGuess:
    path: Path
    recorded_at: dt.datetime
    source: str
    confidence: str
    needs_confirmation: bool
    duration_sec: float = 0.0

    @property
    def source_label(self) -> str:
        return {
            "media_metadata": "音频自带的录制时间",
            "filename": "文件名中的录制时间",
            "filesystem_created": "文件创建时间（复制后可能变化）",
            "filesystem_modified": "文件修改时间（可能不准确）",
        }.get(self.source, "检测到的时间")


@dataclass(frozen=True)
class AudioImportResult:
    source_path: Path
    recorded_at: dt.datetime
    wav_path: Path
    original_path: Path
    manifest_path: Path
    duration_sec: float
    duplicate: bool = False


def is_supported_audio(path: str | Path) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_AUDIO_SUFFIXES


def _plausible(value: dt.datetime) -> bool:
    now = dt.datetime.now(value.tzinfo) if value.tzinfo else dt.datetime.now()
    return dt.datetime(2000, 1, 1, tzinfo=value.tzinfo) <= value <= now + dt.timedelta(days=2)


def _local_naive(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is not None:
        value = value.astimezone().replace(tzinfo=None)
    return value.replace(microsecond=0)


def _parse_datetime_text(value: str) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    for candidate in (normalized, normalized.replace("/", "-")):
        try:
            parsed = dt.datetime.fromisoformat(candidate)
        except ValueError:
            continue
        parsed = _local_naive(parsed)
        return parsed if _plausible(parsed) else None
    match = re.search(
        r"(?P<y>20\d{2})[-_/年]?(?P<m>\d{1,2})[-_/月]?(?P<d>\d{1,2})"
        r"(?:[T _日-]+(?P<h>\d{1,2})[:：._-]?(?P<mi>\d{2})"
        r"(?:[:：._-]?(?P<s>\d{2}))?)?",
        text,
    )
    if not match:
        return None
    try:
        parsed = dt.datetime(
            int(match.group("y")),
            int(match.group("m")),
            int(match.group("d")),
            int(match.group("h") or 0),
            int(match.group("mi") or 0),
            int(match.group("s") or 0),
        )
    except ValueError:
        return None
    return parsed if _plausible(parsed) else None


def _metadata_datetime(metadata: dict[str, Any]) -> dt.datetime | None:
    lowered = {str(key).lower(): value for key, value in metadata.items()}
    for key in (
        "creation_time",
        "com.apple.quicktime.creationdate",
        "date",
        "recorded_date",
        "encoded_date",
    ):
        parsed = _parse_datetime_text(str(lowered.get(key, "")))
        if parsed is not None:
            return parsed
    return None


def _probe_with_pyav(path: Path) -> tuple[dt.datetime | None, float]:
    try:
        import av
    except ImportError:
        return None, 0.0
    with av.open(str(path)) as container:
        metadata: dict[str, Any] = dict(container.metadata or {})
        audio_streams = list(container.streams.audio)
        if audio_streams:
            metadata.update(dict(audio_streams[0].metadata or {}))
        created = _metadata_datetime(metadata)
        duration = 0.0
        if container.duration is not None:
            duration = float(container.duration / av.time_base)
        elif audio_streams and audio_streams[0].duration is not None:
            duration = float(
                audio_streams[0].duration * audio_streams[0].time_base
            )
        return created, max(0.0, duration)


def _ffmpeg_executable() -> str | None:
    configured = str(os.environ.get("VOICE_JOURNAL_FFMPEG") or "").strip()
    if configured and Path(configured).is_file():
        return configured
    candidates: list[Path] = []
    frozen_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    candidates.extend(
        [
            frozen_root / "tools" / "ffmpeg.exe",
            frozen_root / "ffmpeg.exe",
            Path(sys.executable).resolve().parent / "tools" / "ffmpeg.exe",
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return shutil.which("ffmpeg")


def _probe_with_ffmpeg(path: Path) -> tuple[dt.datetime | None, float]:
    executable = _ffmpeg_executable()
    if not executable:
        return None, 0.0
    completed = subprocess.run(
        [executable, "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        creationflags=(
            subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        ),
    )
    output = f"{completed.stdout}\n{completed.stderr}"
    created = None
    match = re.search(
        r"(?:creation_time|date)\s*:\s*([^\r\n]+)",
        output,
        flags=re.IGNORECASE,
    )
    if match:
        created = _parse_datetime_text(match.group(1))
    duration = 0.0
    match = re.search(
        r"Duration:\s*(\d{1,2}):(\d{2}):(\d{2}(?:\.\d+)?)",
        output,
    )
    if match:
        duration = (
            int(match.group(1)) * 3600
            + int(match.group(2)) * 60
            + float(match.group(3))
        )
    return created, duration


def probe_recording(path: str | Path) -> RecordingTimeGuess:
    source_path = Path(path).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"找不到录音文件：{source_path}")
    if not is_supported_audio(source_path):
        raise ValueError(f"暂不支持这种音频格式：{source_path.suffix or '无扩展名'}")

    created = None
    duration = 0.0
    try:
        created, duration = _probe_with_pyav(source_path)
    except Exception:
        created = None
    if created is None or duration <= 0:
        try:
            ffmpeg_created, ffmpeg_duration = _probe_with_ffmpeg(source_path)
        except Exception:
            ffmpeg_created, ffmpeg_duration = None, 0.0
        created = created or ffmpeg_created
        duration = duration or ffmpeg_duration
    if created is not None:
        return RecordingTimeGuess(
            source_path,
            _local_naive(created),
            "media_metadata",
            "high",
            False,
            duration,
        )

    from_name = _parse_datetime_text(source_path.stem)
    if from_name is not None:
        return RecordingTimeGuess(
            source_path,
            from_name,
            "filename",
            "medium",
            False,
            duration,
        )

    stat = source_path.stat()
    created_at = dt.datetime.fromtimestamp(stat.st_ctime).replace(microsecond=0)
    modified_at = dt.datetime.fromtimestamp(stat.st_mtime).replace(microsecond=0)
    # Windows 的 st_ctime 是创建/复制到当前磁盘的时间，只能用作待确认候选。
    if _plausible(created_at):
        return RecordingTimeGuess(
            source_path,
            created_at,
            "filesystem_created",
            "low",
            True,
            duration,
        )
    return RecordingTimeGuess(
        source_path,
        modified_at,
        "filesystem_modified",
        "low",
        True,
        duration,
    )


def probe_recordings(paths: Iterable[str | Path]) -> list[RecordingTimeGuess]:
    return [probe_recording(path) for path in paths]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_name(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", value).strip(" .")
    return cleaned[:100] or "recording"


def _read_index(root: Path) -> dict[str, dict[str, Any]]:
    path = root / "runtime" / "audio-import-index.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_index(root: Path, data: dict[str, dict[str, Any]]) -> None:
    path = root / "runtime" / "audio-import-index.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp.replace(path)


def _normalize_with_pyav(source: Path, target: Path) -> float:
    import av

    total_samples = 0
    with av.open(str(source)) as container, wave.open(str(target), "wb") as output:
        streams = list(container.streams.audio)
        if not streams:
            raise ValueError("文件中没有可识别的音轨")
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        for frame in container.decode(streams[0]):
            for converted in resampler.resample(frame):
                raw = converted.to_ndarray().astype("<i2", copy=False).tobytes()
                output.writeframesraw(raw)
                total_samples += len(raw) // 2
        for converted in resampler.resample(None):
            raw = converted.to_ndarray().astype("<i2", copy=False).tobytes()
            output.writeframesraw(raw)
            total_samples += len(raw) // 2
    return total_samples / 16000.0


def _normalize_with_ffmpeg(source: Path, target: Path) -> float:
    executable = _ffmpeg_executable()
    if not executable:
        raise RuntimeError("没有找到本地音频解码组件")
    completed = subprocess.run(
        [
            executable,
            "-nostdin",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ar",
            "16000",
            "-ac",
            "1",
            "-acodec",
            "pcm_s16le",
            str(target),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60 * 60,
        creationflags=(
            subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        ),
    )
    if completed.returncode != 0 or not target.exists():
        detail = (completed.stderr or completed.stdout or "")[-500:]
        raise RuntimeError(f"本地音频转换失败：{detail}")
    with wave.open(str(target), "rb") as handle:
        return handle.getnframes() / max(1, handle.getframerate())


def _copy_pcm_wav(source: Path, target: Path) -> float:
    """无媒体解码器时的 WAV 兜底，仅接受已经是标准 PCM 的文件。"""
    with wave.open(str(source), "rb") as handle:
        if (
            handle.getnchannels() != 1
            or handle.getsampwidth() != 2
            or handle.getframerate() != 16000
            or handle.getcomptype() != "NONE"
        ):
            raise RuntimeError("这段 WAV 需要本地音频解码组件才能转换")
        duration = handle.getnframes() / max(1, handle.getframerate())
    shutil.copy2(source, target)
    return duration


def _normalize_audio(source: Path, target: Path) -> float:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        return _normalize_with_pyav(source, target)
    except ImportError:
        pass
    except Exception as exc:
        pyav_error = exc
    else:
        pyav_error = None
    try:
        return _normalize_with_ffmpeg(source, target)
    except Exception as ffmpeg_error:
        if source.suffix.lower() == ".wav":
            return _copy_pcm_wav(source, target)
        detail = str(pyav_error) if "pyav_error" in locals() else str(ffmpeg_error)
        raise RuntimeError(
            "无法读取这段录音。请安装包含本地音频解码组件的完整声年版本，"
            f"或先转为 WAV 后重试。\n{detail}"
        ) from ffmpeg_error


def import_recording(
    guess: RecordingTimeGuess,
    root: str | Path,
    *,
    recorded_at: dt.datetime | None = None,
) -> AudioImportResult:
    source = guess.path.expanduser().resolve()
    effective_time = (recorded_at or guess.recorded_at).replace(
        tzinfo=None, microsecond=0
    )
    root_path = Path(root).expanduser().resolve()
    digest = _sha256(source)
    index = _read_index(root_path)
    previous = index.get(digest)
    if previous:
        previous_wav = root_path / str(previous.get("wav", ""))
        previous_original = root_path / str(previous.get("original", ""))
        previous_manifest = root_path / str(previous.get("manifest", ""))
        if previous_wav.exists() and previous_original.exists():
            return AudioImportResult(
                source,
                dt.datetime.fromisoformat(str(previous["recorded_at"])),
                previous_wav,
                previous_original,
                previous_manifest,
                float(previous.get("duration_sec", 0.0)),
                True,
            )

    imported_dir = (
        root_path / "raw" / effective_time.date().isoformat() / "imported"
    )
    original_dir = imported_dir / "original"
    runtime_dir = root_path / "runtime" / "audio-import-working"
    original_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)

    stamp = effective_time.strftime("%H-%M-%S")
    short_hash = digest[:10]
    original_name = f"{stamp}--{short_hash}--{_safe_name(source.name)}"
    original_target = original_dir / original_name
    wav_target = imported_dir / f"{stamp}--{short_hash}.wav"
    manifest_target = wav_target.with_suffix(".import.json")
    temp_wav = runtime_dir / f"{digest}.wav"

    shutil.copy2(source, original_target)
    try:
        duration = _normalize_audio(original_target, temp_wav)
        manifest = {
            "schema_version": 1,
            "kind": "local_audio_import",
            "sha256": digest,
            "original_name": source.name,
            "original_copy": str(original_target.relative_to(root_path)).replace(
                "\\", "/"
            ),
            "wav": str(wav_target.relative_to(root_path)).replace("\\", "/"),
            "recorded_at": effective_time.isoformat(timespec="seconds"),
            "recording_time_source": (
                guess.source if recorded_at is None else "user_confirmed"
            ),
            "recording_time_confidence": (
                guess.confidence if recorded_at is None else "confirmed"
            ),
            "duration_sec": round(duration, 3),
            # UI 不展示内部时间轴；后续“定位原音”可直接使用相对偏移。
            "relative_timeline_ms": [
                {"start_ms": 0, "end_ms": round(duration * 1000)}
            ],
            "imported_at": dt.datetime.now().isoformat(timespec="seconds"),
            "local_only": True,
        }
        temp_manifest = runtime_dir / f"{digest}.import.json"
        temp_manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        imported_dir.mkdir(parents=True, exist_ok=True)
        # 先放清单，最后原子移动 WAV；watchdog 看到 WAV 时元数据已经就绪。
        temp_manifest.replace(manifest_target)
        temp_wav.replace(wav_target)
    except Exception:
        temp_wav.unlink(missing_ok=True)
        manifest_target.unlink(missing_ok=True)
        original_target.unlink(missing_ok=True)
        raise

    relative = lambda value: str(value.relative_to(root_path)).replace("\\", "/")
    index[digest] = {
        "recorded_at": effective_time.isoformat(timespec="seconds"),
        "wav": relative(wav_target),
        "original": relative(original_target),
        "manifest": relative(manifest_target),
        "duration_sec": round(duration, 3),
    }
    _write_index(root_path, index)
    return AudioImportResult(
        source,
        effective_time,
        wav_target,
        original_target,
        manifest_target,
        duration,
        False,
    )


def result_to_dict(result: AudioImportResult) -> dict[str, Any]:
    data = asdict(result)
    for key in ("source_path", "wav_path", "original_path", "manifest_path"):
        data[key] = str(data[key])
    data["recorded_at"] = result.recorded_at.isoformat(timespec="seconds")
    return data
