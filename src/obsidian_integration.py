"""Safe, optional Obsidian integration for the local notes directory.

Shengnian never depends on Obsidian to store data.  This module only
helps a user open the same local Markdown directory in Obsidian.  It does not
modify Obsidian's private configuration or install third-party software.
"""
from __future__ import annotations

import json
import os
import sys
import subprocess
import webbrowser
from pathlib import Path
from urllib.parse import quote


OBSIDIAN_DOWNLOAD_URL = "https://obsidian.md/download"
WELCOME_NOTE_NAME = "欢迎使用声年.md"


def ensure_welcome_note(notes_root: Path) -> Path:
    """Create a small, non-destructive landing note and return its path."""
    root = Path(notes_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    note = root / WELCOME_NOTE_NAME
    if not note.exists():
        note.write_text(
            "# 欢迎使用声年\n\n"
            "这个文件夹是你的本地知识库。声年生成的总结、复盘、"
            "待办和内容会继续保存在这里。\n\n"
            "- 每日总结：当前目录下按日期命名的 Markdown 文件\n"
            "- 项目、待办和简报：`第二大脑` 文件夹\n"
            "- 即使不使用 Obsidian，也可以用任意文本编辑器打开这些文件\n\n"
            "> Obsidian 只是可选的第三方查看和管理工具，不负责声年的数据同步。\n",
            encoding="utf-8",
        )
    return note


def build_open_uri(path: Path) -> str:
    """Build an officially supported, fully encoded Obsidian open URI."""
    absolute = str(Path(path).expanduser().resolve())
    return f"obsidian://open?path={quote(absolute, safe='')}"


def _obsidian_config_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "obsidian" / "obsidian.json"
    appdata = os.environ.get("APPDATA", "").strip()
    if appdata:
        return Path(appdata) / "obsidian" / "obsidian.json"
    return Path.home() / "AppData" / "Roaming" / "obsidian" / "obsidian.json"


def registered_vault_paths(config_path: Path | None = None) -> list[Path]:
    """Read known vault paths without changing Obsidian configuration."""
    path = Path(config_path) if config_path else _obsidian_config_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return []
    vaults = payload.get("vaults", {}) if isinstance(payload, dict) else {}
    result: list[Path] = []
    for value in vaults.values() if isinstance(vaults, dict) else []:
        raw = value.get("path", "") if isinstance(value, dict) else ""
        if not raw:
            continue
        try:
            result.append(Path(raw).expanduser().resolve())
        except OSError:
            continue
    return result


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))


def vault_is_registered(notes_root: Path, config_path: Path | None = None) -> bool:
    target = Path(notes_root).expanduser().resolve()
    return any(_same_path(target, candidate) for candidate in registered_vault_paths(config_path))


def obsidian_uri_registered() -> bool:
    """Return whether Windows knows how to handle ``obsidian://`` links."""
    if sys.platform == "darwin":
        return any(path.is_dir() for path in (
            Path("/Applications/Obsidian.app"), Path.home() / "Applications/Obsidian.app",
        ))
    if sys.platform != "win32":
        return False
    try:
        import winreg

        locations = (
            (winreg.HKEY_CURRENT_USER, r"Software\Classes\obsidian\shell\open\command"),
            (winreg.HKEY_CLASSES_ROOT, r"obsidian\shell\open\command"),
        )
        for hive, key_name in locations:
            try:
                with winreg.OpenKey(hive, key_name):
                    return True
            except OSError:
                continue
    except (ImportError, OSError):
        return False
    return False


def launch_uri(uri: str) -> bool:
    """Launch a custom URI using the operating system's registered handler."""
    try:
        if sys.platform == "win32":
            os.startfile(uri)  # type: ignore[attr-defined]
            return True
        if sys.platform == "darwin":
            return subprocess.run(["/usr/bin/open", uri], timeout=10).returncode == 0
        return bool(webbrowser.open(uri))
    except (OSError, subprocess.TimeoutExpired):
        return False


def open_download_page() -> bool:
    return bool(webbrowser.open(OBSIDIAN_DOWNLOAD_URL))
