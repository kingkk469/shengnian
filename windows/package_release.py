"""Package the app, public source and corresponding dependency source."""
from pathlib import Path
import argparse
import hashlib
import os
import shutil
import subprocess
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("--root", type=Path, required=True)
args = parser.parse_args()
root = args.root.resolve()
app = root / "dist/声年"
assert (root / "verification/RESULTS.json").is_file(), "Verify the executable first"
sources = app / "legal/sources"
sources.mkdir(parents=True, exist_ok=True)
source_root = Path(os.environ["VOICE_JOURNAL_LGPL_SOURCE_ROOT"])
for name in ("VoiceJournal-LGPL-Sources-6.11.1.zip", "VoiceJournal-FFmpeg-LGPL-Sources-20260728.zip"):
    destination = sources / name
    assert not destination.exists()
    shutil.copy2(source_root / name, destination)
source_zip = sources / "shengnian-0.3.2-source.zip"
assert not source_zip.exists()
with zipfile.ZipFile(source_zip, "w", zipfile.ZIP_DEFLATED) as archive:
    for directory in ("src", "tests", "prompts", "assets", "windows"):
        for file in (PROJECT / directory).rglob("*"):
            relative = file.relative_to(PROJECT)
            if not file.is_file() or any(p in {"__pycache__", "evidence", ".pytest_cache"} for p in relative.parts) or file.suffix == ".log" or file.name == "config.toml":
                continue
            archive.write(file, relative.as_posix())
    for name in ("README.md", "LICENSE", "SECURITY.md", "CONTRIBUTING.md", "pyproject.toml", "requirements.txt", "hotwords.example.txt", "speakers.example.json"):
        archive.write(PROJECT / name, name)
shutil.copy2(PROJECT / "windows/请先读我.txt", app / "请先读我.txt")
shutil.copy2(PROJECT / "LICENSE", app / "LICENSE")
release = root / "发给朋友"
release.mkdir(exist_ok=True)
target = release / "声年-Windows免费版-0.3.2-x64.zip"
assert not target.exists()
subprocess.run([r"C:\Program Files\7-Zip\7z.exe", "a", "-tzip", "-mx=1", "-mmt=4", str(target), "声年"], cwd=app.parent, check=True)
subprocess.run([r"C:\Program Files\7-Zip\7z.exe", "t", str(target)], check=True)
digest = hashlib.file_digest(target.open("rb"), "sha256").hexdigest()
(release / "SHA256SUMS.txt").write_text(f"{digest}  {target.name}\n", encoding="utf-8")
shutil.copy2(PROJECT / "windows/请先读我.txt", release / "使用说明.txt")
print(f"完成：{target}\n字节：{target.stat().st_size}\nSHA256：{digest}")
