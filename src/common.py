"""共享工具:路径、日志、jsonl、配置、脱敏。"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import time
from datetime import datetime, date

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # 兜底
    import tomli as tomllib  # type: ignore
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Iterator

from runtime_profile import is_commercial_mode
from platform_support import default_data_root, load_local_api_keys


_HERE = Path(__file__).resolve().parent
_FROZEN_ROOT = Path(getattr(sys, "_MEIPASS", _HERE.parent)).resolve()
RESOURCE_ROOT = _FROZEN_ROOT if getattr(sys, "frozen", False) else _HERE.parent


def load_config() -> dict:
    config_name = "config.commercial.toml" if is_commercial_mode() else "config.toml"
    config_path = _FROZEN_ROOT / config_name if getattr(sys, "frozen", False) else _HERE / config_name
    override = os.environ.get("VOICE_JOURNAL_CONFIG", "").strip()
    if getattr(sys, "frozen", False) and not is_commercial_mode() and not override:
        local_config = default_data_root() / "config.toml"
        if not local_config.exists():
            local_config.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(_FROZEN_ROOT / "config.example.toml", local_config)
        config_path = local_config
    if override and not is_commercial_mode():
        # An explicit missing file is an error, never a silent fallback.
        with Path(override).expanduser().open("rb") as handle:
            return tomllib.load(handle)
    if not config_path.exists() and not is_commercial_mode():
        config_path = _HERE / "config.example.toml"
    with open(config_path, "rb") as f:
        return tomllib.load(f)


CONFIG = load_config()
_configured_root = str(CONFIG["paths"].get("root", "")).strip()
if is_commercial_mode() or os.environ.get("VOICE_JOURNAL_DATA_ROOT", "").strip() or _configured_root in {"", "__AUTO__"}:
    ROOT = default_data_root()
else:
    ROOT = Path(_configured_root)
ROOT = ROOT.expanduser().resolve()
load_local_api_keys(ROOT)


def _ensure_data_layout() -> None:
    for name in ("raw", "transcripts", "notes", "logs", "runtime", "meetings"):
        (ROOT / name).mkdir(parents=True, exist_ok=True)
    if not is_commercial_mode() and not getattr(sys, "frozen", False):
        return
    defaults = RESOURCE_ROOT / "defaults"
    if not defaults.exists():
        defaults = RESOURCE_ROOT / "packaging" / "defaults"
    for name in ("hotwords.txt", "corrections.json"):
        target = ROOT / name
        source = defaults / name
        if not target.exists() and source.exists():
            shutil.copy2(source, target)


_ensure_data_layout()


class WindowsSafeTimedRotatingFileHandler(TimedRotatingFileHandler):
    """Avoid noisy rollover failures when another app process owns the log.

    Voice Journal deliberately runs the launcher, recorder and transcriber as
    separate Windows processes.  Windows does not permit renaming an open log
    file, so a midnight rollover may temporarily fail even though appending is
    still safe.  Keep the current file for one more interval and retry later
    instead of emitting a logging traceback into the user session.
    """

    def doRollover(self) -> None:  # noqa: N802 - logging API spelling
        try:
            super().doRollover()
        except PermissionError:
            self.rolloverAt = int(time.time()) + max(60, int(self.interval))


def day_dir(kind: str, day: date | None = None) -> Path:
    """raw/transcripts/notes/logs 子目录,按天分组。"""
    day = day or date.today()
    d = ROOT / kind / day.isoformat()
    d.mkdir(parents=True, exist_ok=True)
    return d


def transcript_path(day: date | None = None) -> Path:
    day = day or date.today()
    p = ROOT / "transcripts" / f"{day.isoformat()}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def note_path(day: date | None = None) -> Path:
    day = day or date.today()
    p = ROOT / "notes" / f"{day.isoformat()}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def configured_obsidian_vault() -> Path | None:
    """Resolve the user's optional Obsidian vault without hard-coding it.

    Commercial builds keep machine-specific paths in
    ``runtime/local-paths.json``.  Development builds may still provide an
    explicit ``[obsidian].vault`` value in ``config.toml``.  Prefer the
    explicit config and then fall back to the onboarding value.
    """
    configured = str(CONFIG.get("obsidian", {}).get("vault", "") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()

    local_paths = ROOT / "runtime" / "local-paths.json"
    try:
        payload = json.loads(local_paths.read_text(encoding="utf-8"))
        configured = str(payload.get("obsidian_vault") or "").strip()
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        configured = ""
    return Path(configured).expanduser().resolve() if configured else None


def configured_owner_name() -> str:
    """返回当前用户在声纹库中的名称。"""
    value = str(CONFIG.get("speaker", {}).get("owner_name", "我") or "我").strip()
    return value or "我"


def knowledge_dir() -> Path:
    """Return the writable knowledge directory used by summaries and todos.

    An explicitly configured Obsidian vault keeps the legacy layout.  The
    commercial package otherwise uses the local notes directory as a complete
    fallback, so core features never depend on a third-party application.
    """
    vault = configured_obsidian_vault()
    p = (vault if vault else (ROOT / "notes")) / "第二大脑"
    p.mkdir(parents=True, exist_ok=True)
    return p.resolve()


def pause_flag() -> Path:
    return ROOT / ".paused"


def runtime_dir() -> Path:
    p = ROOT / "runtime"
    p.mkdir(parents=True, exist_ok=True)
    return p


def speakers_path() -> Path:
    return runtime_dir() / "speakers.json"


def load_speakers() -> list[dict]:
    """声纹库:[{id, name, embedding: list[float], created_at, samples: int}]"""
    p = speakers_path()
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def save_speakers(speakers: list[dict]) -> None:
    p = speakers_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(speakers, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def recorder_status_path() -> Path:
    return runtime_dir() / "recorder-status.json"


def write_recorder_status(payload: dict) -> None:
    """recorder 周期性写;launcher 读。原子替换避免读到半截。"""
    p = recorder_status_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def read_recorder_status() -> dict | None:
    p = recorder_status_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def setup_logger(name: str) -> logging.Logger:
    """日志:同时输出到 stdout 和 logs/<name>-YYYY-MM-DD.log(按天滚动,留 30 天)。"""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    log_dir = ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fh = WindowsSafeTimedRotatingFileHandler(
        log_dir / f"{name}.log",
        when="midnight",
        backupCount=30,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> Iterator[dict]:
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def write_jsonl(path: Path, records: list[dict]) -> None:
    """整体重写 jsonl(配合 delete 使用)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(path)


def list_history_days() -> list[date]:
    """有 jsonl 的日期,新到旧。"""
    base = ROOT / "transcripts"
    if not base.exists():
        return []
    out: list[date] = []
    for p in base.glob("*.jsonl"):
        try:
            out.append(date.fromisoformat(p.stem))
        except ValueError:
            continue
    return sorted(out, reverse=True)


def undo_recent(minutes: int, delete_wav: bool = True) -> tuple[int, int]:
    """删除"最近 N 分钟内"的所有转写段(可选连 WAV)。返回 (段数, WAV 数)。
    判断标准:start 字段在 now-minutes 之内。
    """
    cutoff = datetime.now() - __import__("datetime").timedelta(minutes=minutes)
    today = date.today()
    yesterday = today - __import__("datetime").timedelta(days=1)
    total_seg = 0
    total_wav = 0
    # 只动今天和昨天(跨午夜场景),其他日期不动
    for day in [today, yesterday]:
        path = transcript_path(day)
        if not path.exists():
            continue
        records = list(read_jsonl(path))
        keep = []
        for r in records:
            try:
                ts = datetime.fromisoformat(r.get("start", ""))
            except (ValueError, TypeError):
                keep.append(r)
                continue
            if ts >= cutoff:
                total_seg += 1
                if delete_wav and r.get("wav"):
                    wav = ROOT / r["wav"].replace("\\", "/")
                    try:
                        if wav.exists():
                            wav.unlink()
                            total_wav += 1
                    except OSError:
                        pass
            else:
                keep.append(r)
        write_jsonl(path, keep)
    return total_seg, total_wav


def delete_segments(day: date, indexes: list[int], delete_wav: bool) -> tuple[int, int]:
    """删 jsonl 里第 indexes(0-based)行;可选连 WAV 删。返回 (删段数, 删 WAV 数)。"""
    path = transcript_path(day)
    records = list(read_jsonl(path))
    keep: list[dict] = []
    removed_wavs = 0
    drop_set = set(i for i in indexes if 0 <= i < len(records))
    for i, r in enumerate(records):
        if i in drop_set:
            if delete_wav and r.get("wav"):
                wav = ROOT / r["wav"].replace("\\", "/")
                try:
                    if wav.exists():
                        wav.unlink()
                        removed_wavs += 1
                except OSError as exc:
                    raise OSError(f"无法删除音频文件：{wav}") from exc
            continue
        keep.append(r)
    write_jsonl(path, keep)
    return len(drop_set), removed_wavs


def iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_ID_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_BANK_RE = re.compile(r"(?<!\d)\d{16,19}(?!\d)")


def redact(text: str) -> str:
    """简单脱敏:手机号、身份证、银行卡。顺序:身份证 → 银行卡 → 手机号(避免短前缀误吞)。"""
    text = _ID_RE.sub("[身份证]", text)
    text = _BANK_RE.sub("[银行卡号]", text)
    text = _PHONE_RE.sub("[手机号]", text)
    return text
