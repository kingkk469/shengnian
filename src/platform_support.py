"""Small OS boundary shared by the desktop UI and background workers.

This module has no GUI, audio or model imports, so installation checks can use
it before the large optional dependencies are installed.
"""
from __future__ import annotations

import atexit
import json
import os
import signal
import subprocess
import sys
from pathlib import Path


def default_data_root() -> Path:
    override = os.environ.get("VOICE_JOURNAL_DATA_ROOT", "").strip()
    if override:
        return Path(override).expanduser()
    legacy = os.environ.get("VOICE_JOURNAL_LOCAL_APPDATA", "").strip()
    if legacy:
        base = Path(legacy)
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "VoiceJournal" / "Data"


def source_python(resource_root: Path) -> Path:
    candidate = resource_root / ".venv" / (
        "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    )
    # Do not resolve the venv symlink on POSIX: doing so bypasses the venv.
    return candidate if candidate.is_file() else Path(sys.executable)


def open_path(path: str | Path) -> None:
    absolute = str(Path(path).expanduser().resolve())
    if sys.platform == "win32":
        os.startfile(absolute)
    else:
        command = "/usr/bin/open" if sys.platform == "darwin" else "xdg-open"
        subprocess.run([command, absolute], check=True, timeout=10)


def configure_qt_environment() -> None:
    if sys.platform == "win32":
        os.environ.setdefault("QT_QPA_PLATFORM", "windows:fontengine=freetype")


def microphone_permission_hint() -> str:
    if sys.platform == "darwin":
        if getattr(sys, "frozen", False):
            return "请到系统设置 → 隐私与安全性 → 麦克风，允许声年访问，然后重启声年"
        return "请到系统设置 → 隐私与安全性 → 麦克风，允许启动声年的终端访问，然后重启声年"
    return "请检查系统麦克风权限和输入设备"


def load_local_api_keys(root: Path) -> None:
    """Mac Finder/login launches do not inherit shell profile variables.

Read data, never execute shell config. Explicit environment values win.
"""
    if sys.platform != "darwin":
        return
    path = root / "runtime" / "api-keys.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(payload, dict):
        return
    for key in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "SNAPANY_API_KEY"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            os.environ.setdefault(key, value.strip())


def model_device_kwargs(config: dict) -> dict:
    device = str(config.get("device") or "auto").strip()
    if device != "auto":
        return {"device": device}
    # Establish correctness on CPU before making any MPS performance promise.
    return {"device": "cpu"} if sys.platform == "darwin" else {}


def pid_exists(pid: int) -> bool:
    if pid <= 1:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False


def _process_birth(pid: int) -> str:
    try:
        result = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True, text=True, timeout=3,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


class RoleLock:
    """POSIX advisory lock, released by the kernel even after a crash.

    Never unlink the lock file: another process may already hold its inode.
    Windows retains its existing worker mutex/process management.
    """
    def __init__(self, root: Path, role: str):
        if role not in {"launcher", "recorder", "transcriber"}:
            raise ValueError(f"Unsupported worker role: {role}")
        self.path = root / "runtime" / f"{role}.lock"
        self.handle = None

    def acquire(self) -> bool:
        if sys.platform == "win32":
            return True
        import fcntl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return False
        except BaseException:
            handle.close()
            raise
        self.handle = handle
        handle.seek(0)
        handle.truncate()
        json.dump({"pid": os.getpid(), "birth": _process_birth(os.getpid())}, handle)
        handle.flush()
        atexit.register(self.release)
        return True

    def release(self) -> None:
        if self.handle is not None:
            self.handle.close()
            self.handle = None
            atexit.unregister(self.release)


def locked_role_pid(root: Path, role: str) -> int | None:
    """Only adopt a currently locked worker with matching process start time."""
    if sys.platform == "win32":
        return None
    import fcntl
    path = RoleLock(root, role).path
    try:
        with path.open("r+", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                payload = json.load(handle)
            else:
                return None  # Stale PID file; nobody holds the lock.
        pid = int(payload["pid"])
        birth = payload.get("birth", "")
        if pid > 1 and pid_exists(pid) and birth and birth == _process_birth(pid):
            return pid
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def install_worker_signals() -> None:
    if sys.platform != "win32":
        def stop(_signum, _frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, stop)


_sleep_process = None


def prevent_macos_sleep(on: bool) -> None:
    """Hold idle-sleep assertion while recording; display may still turn off."""
    global _sleep_process
    if sys.platform != "darwin":
        return
    if on and (_sleep_process is None or _sleep_process.poll() is not None):
        _sleep_process = subprocess.Popen(
            ["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    elif not on and _sleep_process is not None:
        _sleep_process.terminate()
        try:
            _sleep_process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _sleep_process.kill()
            _sleep_process.wait(timeout=3)
        _sleep_process = None
