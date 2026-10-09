from pathlib import Path
import hashlib
import json
import subprocess

PROJECT = Path(__file__).resolve().parents[1]
OUT = PROJECT / "assets-heavy/windows-0.3.2/clean/github-release"
payload = OUT / "Shengnian-0.3.2-Windows-x64.exe"
assert payload.is_file()
def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()
assert sha(payload) == "0bd9a8f3ebb406ae401d54e0b6ac0b2eee708554870b3c37a15baba2234ffb80"
parts = []
reuse = (OUT / "asset-manifest.json").is_file()
prior = json.loads((OUT / "asset-manifest.json").read_text(encoding="utf-8")) if reuse else {}
with payload.open("rb") as source:
    for number in (1, 2):
        part = OUT / f"Shengnian-0.3.2-Windows-payload.{number:03d}"
        remaining = 1500 * 1024 * 1024 if number == 1 else payload.stat().st_size
        if part.exists():
            assert part.name in prior and sha(part) == prior[part.name]["sha256"]
            source.seek(part.stat().st_size, 1)
        else:
            with part.open("xb") as target:
                while remaining:
                    block = source.read(min(remaining, 1024 * 1024))
                    if not block: break
                    target.write(block)
                    remaining -= len(block)
        assert 0 < part.stat().st_size < 2**31
        parts.append(part)
    assert not source.read(1)
code = (PROJECT / "windows/OnlineInstaller.cs").read_text(encoding="utf-8")
for number, part in enumerate(parts, 1):
    code = code.replace(f"__PART_{number}_HASH__", sha(part)).replace(f"__PART_{number}_SIZE__", str(part.stat().st_size) + "L")
code = code.replace("__APP_HASH__", sha(OUT / "payload/声年/声年.exe"))
compiled = OUT / "OnlineInstaller.compiled.cs"
compiled.write_text(code, encoding="utf-8-sig")
installer = OUT / "Shengnian-0.3.2-Windows-Setup.exe"
subprocess.run([r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe", "/nologo", "/codepage:65001", "/target:winexe", "/optimize+", "/out:" + str(installer), "/win32manifest:" + str(PROJECT / "windows/installer.manifest"), "/reference:System.dll", "/reference:System.Core.dll", "/reference:System.Drawing.dll", "/reference:System.Windows.Forms.dll", str(compiled)], check=True)
assets = [installer, *parts, OUT / "7z2602-src.7z"]
source_dir = OUT.parent / "dist/声年/legal/sources"
assets += [source_dir / name for name in ("shengnian-0.3.2-source.zip", "VoiceJournal-LGPL-Sources-6.11.1.zip", "VoiceJournal-FFmpeg-LGPL-Sources-20260728.zip")]
manifest = {path.name: {"path": str(path.resolve()), "size": path.stat().st_size, "sha256": sha(path)} for path in assets}
(OUT / "SHA256SUMS.txt").write_text("".join(f"{item['sha256']}  {name}\n" for name, item in manifest.items()), encoding="utf-8")
manifest["SHA256SUMS.txt"] = {"path": str((OUT / "SHA256SUMS.txt").resolve()), "size": (OUT / "SHA256SUMS.txt").stat().st_size, "sha256": sha(OUT / "SHA256SUMS.txt")}
(OUT / "asset-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
import time
test_root = OUT / ("installer-check-" + str(time.time_ns()))
result = subprocess.run([str(installer), "--offline-install", str(OUT.resolve()), str((test_root / "cache").resolve()), str((test_root / "app").resolve())], timeout=180)
assert result.returncode == 0, f"Installer extraction failed: {result.returncode}"
assert sha(test_root / "app/声年/声年.exe") == sha(OUT / "payload/声年/声年.exe")
print(json.dumps({"installer_bytes": installer.stat().st_size, "offline_install": "passed", "part_bytes": [p.stat().st_size for p in parts]}, ensure_ascii=False), flush=True)
