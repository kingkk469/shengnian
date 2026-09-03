"""Build a source-only beta archive from public project files, excluding data."""
from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
import stat
import subprocess
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
ARCHIVE_ROOT = "shengnian-mac-beta1"
FILENAME = "shengnian-mac-beta1-20260903.zip"
FORBIDDEN_PARTS = {".git", ".venv", "__pycache__", ".pytest_cache", ".test-data", "assets-heavy", "release", "runtime", "raw", "transcripts", "notes", "logs", "meetings", "models"}
FORBIDDEN_NAMES = {"config.toml", "hotwords.txt", "speakers.json", "corrections.json", "api-keys.json", "secrets.json", "start-launcher.bat"}


def public_paths() -> list[Path]:
    output = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=PROJECT,
    )
    paths = []
    for raw in sorted(set(output.decode("utf-8").split("\0")) - {""}):
        relative = PurePosixPath(raw)
        if FORBIDDEN_PARTS.intersection(relative.parts) or relative.name in FORBIDDEN_NAMES:
            continue
        if relative.name.startswith(".env") or relative.suffix in {".key", ".pem", ".wav", ".mp3", ".m4a", ".pyc"}:
            continue
        if "cookies" in relative.name.lower():
            continue
        source = PROJECT / raw
        if source.is_symlink() or not source.resolve().is_relative_to(PROJECT):
            raise RuntimeError(f"拒绝打包目录外的链接：{raw}")
        if source.is_file():
            paths.append(source)
    return paths


def main():
    release = PROJECT / "release"
    release.mkdir(exist_ok=True)
    target = release / FILENAME
    paths = public_paths()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source in paths:
            relative = source.relative_to(PROJECT).as_posix()
            entry = zipfile.ZipInfo(f"{ARCHIVE_ROOT}/{relative}")
            entry.create_system = 3
            executable = source.suffix in {".sh", ".command"}
            entry.external_attr = (stat.S_IFREG | (0o755 if executable else 0o644)) << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            content = source.read_bytes()
            if executable:
                content = content.replace(b"\r\n", b"\n")
            archive.writestr(entry, content)
    with zipfile.ZipFile(target) as archive:
        assert archive.testzip() is None
        names = archive.namelist()
        for required in ("setup-macos.command", "start-macos.command", "macos/README.md", "macos/VALIDATION.md", "src/platform_support.py", "src/config.example.toml"):
            assert f"{ARCHIVE_ROOT}/{required}" in names
        for name in names:
            relative = PurePosixPath(name).relative_to(ARCHIVE_ROOT)
            assert not FORBIDDEN_PARTS.intersection(relative.parts)
            assert relative.name not in FORBIDDEN_NAMES
        for info in archive.infolist():
            if info.filename.endswith((".sh", ".command")):
                assert b"\r\n" not in archive.read(info)
                assert (info.external_attr >> 16) & 0o111
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix(".zip.sha256").write_text(f"{digest}  {target.name}\n", encoding="utf-8")
    print(f"源码包：{target}\n文件数：{len(paths)}\n大小：{target.stat().st_size:,} bytes\nSHA256：{digest}")


if __name__ == "__main__":
    main()
