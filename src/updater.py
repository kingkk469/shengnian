from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Callable

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


UPDATE_PUBLIC_KEY_B64 = "WQfMwCPM0i2NrfAFT4RvKHzeI7JJd2CfrFE6OlplWt4="


def _version_key(value: str) -> tuple:
    main, _, suffix = value.strip().lower().partition("-")
    nums = tuple(int(part) if part.isdigit() else 0 for part in main.split("."))
    if not suffix:
        return nums + (2, 0)
    label, _, number = suffix.partition(".")
    rank = 1 if label == "rc" else 0
    return nums + (rank, int(number) if number.isdigit() else 0)


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def verify_manifest(manifest: dict) -> dict:
    payload = manifest.get("payload")
    signature = manifest.get("signature")
    if not isinstance(payload, dict) or not isinstance(signature, str):
        raise ValueError("更新清单格式错误")
    key = Ed25519PublicKey.from_public_bytes(base64.b64decode(UPDATE_PUBLIC_KEY_B64))
    key.verify(base64.b64decode(signature), _canonical(payload))
    required = {"version", "url", "sha256", "size", "published_at"}
    if not required.issubset(payload):
        raise ValueError("更新清单字段不完整")
    if not str(payload["url"]).startswith("https://"):
        raise ValueError("更新地址必须使用 HTTPS")
    if len(str(payload["sha256"])) != 64:
        raise ValueError("更新 SHA-256 无效")
    return payload


def check_for_update(
    manifest_url: str,
    current_version: str,
    timeout: int = 10,
    *,
    suppress_errors: bool = True,
) -> dict | None:
    if not manifest_url.startswith("https://"):
        if not suppress_errors:
            raise ValueError("更新清单地址必须使用 HTTPS")
        return None
    request = urllib.request.Request(manifest_url, headers={"Accept": "application/json", "User-Agent": "VoiceJournal-Updater/1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            manifest = json.loads(response.read(256_000))
    except Exception:
        if not suppress_errors:
            raise
        return None
    payload = verify_manifest(manifest)
    return payload if _version_key(str(payload["version"])) > _version_key(current_version) else None


def _hash_existing(path: Path) -> tuple[hashlib._Hash, int]:
    digest = hashlib.sha256()
    size = 0
    if path.exists():
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
    return digest, size


def download_and_launch(
    payload: dict,
    progress: Callable[[int, int], None] | None = None,
    *,
    launch: bool = True,
    retries: int = 3,
) -> Path:
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "VoiceJournal" / "Updates"
    local.mkdir(parents=True, exist_ok=True)
    name = f"VoiceJournalSetup-{payload['version']}-win-x64.exe"
    target = (local / name).resolve()
    if local.resolve() not in target.parents:
        raise ValueError("更新文件路径越界")
    partial = target.with_suffix(".part")
    expected_size = int(payload["size"])
    if partial.exists() and partial.stat().st_size > expected_size:
        partial.unlink(missing_ok=True)

    last_error: Exception | None = None
    for attempt in range(max(1, retries)):
        digest, size = _hash_existing(partial)
        headers = {"User-Agent": "VoiceJournal-Updater/1"}
        if size:
            headers["Range"] = f"bytes={size}-"
        request = urllib.request.Request(str(payload["url"]), headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                response_code = getattr(response, "status", None) or response.getcode()
                if size and response_code != 206:
                    # 服务器不支持断点续传时，从头开始，避免把完整响应追加到旧文件后面。
                    partial.unlink(missing_ok=True)
                    digest = hashlib.sha256()
                    size = 0
                    mode = "wb"
                else:
                    mode = "ab" if size else "wb"
                with partial.open(mode) as handle:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        handle.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                        if progress:
                            progress(size, expected_size)
            if size == expected_size:
                last_error = None
                break
            last_error = IOError(f"更新下载不完整：{size}/{expected_size}")
        except Exception as exc:
            last_error = exc
        if attempt + 1 < max(1, retries):
            time.sleep(min(2 ** attempt, 4))
    if last_error is not None:
        raise last_error

    digest, size = _hash_existing(partial)
    if size != expected_size or digest.hexdigest().lower() != str(payload["sha256"]).lower():
        partial.unlink(missing_ok=True)
        raise ValueError("更新安装包完整性校验失败")
    partial.replace(target)
    if launch:
        subprocess.Popen([str(target)], close_fds=True)
    return target
