"""Prepare a single self-extracting Windows release under GitHub's asset limit."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess

PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT / "assets-heavy/windows-0.3.2/clean"
ORIGINAL = ROOT / "dist/声年"
OUT = ROOT / "github-release"
assert (ROOT / "verification/RESULTS.json").is_file()
OUT.mkdir(exist_ok=False)
stage = OUT / "payload/声年"
for source in ORIGINAL.rglob("*"):
    relative = source.relative_to(ORIGINAL)
    if not source.is_file() or relative.parts[:2] == ("legal", "sources"):
        continue
    target = stage / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    # Editable release-specific notices get their own copies.
    if relative.parts[0] == "legal" or source.suffix.lower() in {".txt", ".md"}:
        shutil.copy2(source, target)
    else:
        os.link(source, target)
release_url = "https://github.com/kingkk469/shengnian/releases/tag/v0.3.2-windows"
source_offer = (
    "# 对应源码\n\n声年以 MIT 协议免费开源。GitHub 发布页同时提供本版本应用源码及以下第三方对应源码附件：\n\n"
    "- VoiceJournal-LGPL-Sources-6.11.1.zip：Qt/PySide6/Shiboken6 对应源码。\n"
    "- VoiceJournal-FFmpeg-LGPL-Sources-20260728.zip：FFmpeg 及相关依赖的对应源码。\n\n"
    f"获取地址：{release_url}\n\n应用及上述组件的许可原文保存在本目录。对应源码不需要下载就能运行程序，但再分发时须保留源码获取方式及相应许可。\n"
)
(stage / "legal/SOURCE-OFFER.md").write_text(source_offer, encoding="utf-8")
relinking = stage / "legal/RELINKING.md"
relinking.write_text(relinking.read_text(encoding="utf-8").replace("完整对应源码放在 `legal/sources/`。", f"完整对应源码可从 {release_url} 的附件下载。"), encoding="utf-8")
guide = (PROJECT / "windows/请先读我.txt").read_text(encoding="utf-8")
guide = guide.replace("1. 把整个 ZIP 解压到一个普通文件夹，例如 D:\\声年。不要直接在压缩包里运行，也不要只拿走 exe；_internal 文件夹是必需的。", "1. 双击下载的 Shengnian-0.3.2-Windows-x64.exe，选择一个普通文件夹解压。解压后保留整个“声年”文件夹；_internal 文件夹是必需的。")
guide = guide.replace("转发时请发送完整 ZIP。包内 legal/sources 为软件及第三方组件源码，按对应开源协议保留。", f"下载及转发入口：{release_url}\n软件及第三方组件对应源码在该发布页的独立附件中，按对应开源协议保留。")
(stage / "请先读我.txt").write_text(guide, encoding="utf-8")
(OUT / "INSTALL-Windows.txt").write_text(guide, encoding="utf-8")
archive = OUT / "Shengnian-0.3.2-Windows-x64.7z"
seven = Path(r"C:\Program Files\7-Zip\7z.exe")
subprocess.run([str(seven), "a", "-t7z", "-mx=5", "-ms=on", "-mmt=4", str(archive), "声年"], cwd=stage.parent, check=True)
executable = OUT / "Shengnian-0.3.2-Windows-x64.exe"
with executable.open("xb") as target:
    for part in (seven.parent / "7z.sfx", archive):
        with part.open("rb") as stream:
            shutil.copyfileobj(stream, target, 1024 * 1024)
assert executable.stat().st_size < 2**31, "GitHub asset size limit exceeded"
subprocess.run([str(seven), "t", str(executable)], check=True)
assets = [executable, OUT / "INSTALL-Windows.txt",
    ORIGINAL / "legal/sources/shengnian-0.3.2-source.zip",
    ORIGINAL / "legal/sources/VoiceJournal-LGPL-Sources-6.11.1.zip",
    ORIGINAL / "legal/sources/VoiceJournal-FFmpeg-LGPL-Sources-20260728.zip"]
manifest = {}
for path in assets:
    with path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    manifest[path.name] = {"path": str(path.resolve()), "size": path.stat().st_size, "sha256": digest}
(OUT / "SHA256SUMS.txt").write_text("".join(f"{item['sha256']}  {name}\n" for name, item in manifest.items()), encoding="utf-8")
with (OUT / "SHA256SUMS.txt").open("rb") as handle:
    digest = hashlib.file_digest(handle, "sha256").hexdigest()
manifest["SHA256SUMS.txt"] = {"path": str((OUT / "SHA256SUMS.txt").resolve()), "size": (OUT / "SHA256SUMS.txt").stat().st_size, "sha256": digest}
(OUT / "asset-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"installer_bytes": executable.stat().st_size, "installer_sha256": manifest[executable.name]["sha256"]}, ensure_ascii=False), flush=True)
