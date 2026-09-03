"""Publish the existing verified DMG without rebuilding or dropping its models."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
REPO = "kingkk469/shengnian"
TAG = "v0.3.1-macos-beta.1"
BUILD_SHA = "43464c93b24679beb612e1db29e7832658125775"
BUILD_RUN = 33759616541
SOURCE = ROOT / "release/cloud-source"
OUT = ROOT / "release/github-macos-beta1"
RECORD = ROOT / "release/publication"
PARTS = ["shengnian-macos-arm64.dmg", "shengnian-macos-arm64.002.dmgpart"]


def run(*args: str | Path, capture: bool = False) -> str:
    command = [str(arg) for arg in args]
    print("+", " ".join(command), flush=True)
    result = subprocess.run(command, check=True, text=True, encoding="utf-8",
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout if capture else ""


def api(path: str) -> dict:
    return json.loads(run("gh", "api", f"repos/{REPO}/{path}", capture=True))


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def prepare() -> None:
    require(sys.platform == "darwin", "Native disk image verification requires macOS")
    build = api(f"actions/runs/{BUILD_RUN}")
    require(build["conclusion"] == "success" and build["head_sha"] == BUILD_SHA,
            "Source build is not the expected successful build")
    OUT.mkdir(parents=True, exist_ok=False)
    RECORD.mkdir(parents=True, exist_ok=True)
    dmg = SOURCE / "installer/shengnian-macos-arm64.dmg"
    expected = dmg.with_suffix(".dmg.sha256").read_text(encoding="utf-8").split()[0]
    require(bool(re.fullmatch(r"[0-9a-f]{64}", expected)), "Invalid original checksum")
    require(sha256(dmg) == expected, "Original DMG checksum mismatch")
    run("hdiutil", "verify", dmg)
    run("hdiutil", "segment", "-segmentSize", "1500m", "-o",
        OUT / "shengnian-macos-arm64", dmg)
    actual_parts = sorted(path.name for path in OUT.iterdir())
    print("Generated disk image files:", actual_parts, flush=True)
    require(actual_parts == sorted(PARTS), "Unexpected native disk image segment names")
    for name in PARTS:
        require(0 < (OUT / name).stat().st_size < 2**31, "Release asset size exceeds limit")
    run("hdiutil", "verify", OUT / PARTS[0])
    mount = ROOT / ".test-data/release-mount"
    mount.mkdir(parents=True, exist_ok=False)
    run("hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", mount, OUT / PARTS[0])
    try:
        app = mount / "声年.app"
        require(os.access(app / "Contents/MacOS/Shengnian", os.X_OK), "Missing app executable")
        require((mount / "安装说明.txt").is_file(), "Missing installer instructions")
        require(os.readlink(mount / "Applications") == "/Applications", "Invalid Applications shortcut")
        run("codesign", "--verify", "--deep", "--strict", "--verbose=2", app)
    finally:
        run("hdiutil", "detach", mount)
    verification = {
        "status": "passed", "build_sha": BUILD_SHA, "build_run": BUILD_RUN,
        "original_dmg_sha256": expected, "original_dmg_bytes": dmg.stat().st_size,
        "native_segmented_dmg": True, "segmented_dmg_verified": True,
        "segmented_dmg_mounted": True, "app_signature_verified": True,
        "apple_notarized": False, "physical_microphone_tested": False,
    }
    save(RECORD / "release-verification.json", verification)
    evidence = SOURCE / "evidence"
    reports = sorted(evidence.rglob("mac-self-test-*.json"))
    require(len(reports) == 3, "Missing original app verification reports")
    for report in reports:
        require(json.loads(report.read_text(encoding="utf-8"))["status"] == "passed",
                f"Original check did not pass: {report.name}")
    screenshots = list(evidence.rglob("mac-app.png"))
    require(len(screenshots) == 1, "Missing verified Mac screenshot")
    shutil.copy2(screenshots[0], OUT / "shengnian-mac.png")
    with zipfile.ZipFile(OUT / "macos-verification.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for report in reports:
            archive.write(report, report.name)
        archive.write(screenshots[0], "mac-app.png")
        archive.write(RECORD / "release-verification.json", "release-verification.json")
    instructions = (
        "声年 Mac 安装说明（苹果芯片，macOS 14+）\n\n"
        "请下载 shengnian-macos-arm64.dmg 和 shengnian-macos-arm64.002.dmgpart，\n"
        "放到同一个文件夹，保留原文件名。两卷缺一不可。\n"
        "双击 shengnian-macos-arm64.dmg，把声年拖到 Applications（应用程序）。\n"
        "Mac 会自动读取第二卷，无需手动合并或使用终端。\n\n"
        "应用已内置 Python、FFmpeg 和四个语音模型。\n"
        "首次录音请允许麦克风访问；AI 总结在 API 配置中填写自己的 DeepSeek Key。\n"
        "本版尚未经过 Apple 公证。若首次打开被拦截，请在系统设置 → 隐私与安全性中按提示确认打开。\n"
        "真实麦克风、热插拔、睡眠和长时间录音尚未做硬件验收。\n\n"
        f"下载页：https://github.com/{REPO}/releases/tag/{TAG}\n"
    )
    (OUT / "INSTALL-macos.txt").write_text(instructions, encoding="utf-8")
    checksums = "".join(f"{sha256(path)}  {path.name}\n" for path in sorted(OUT.iterdir()))
    (OUT / "SHA256SUMS.txt").write_text(checksums, encoding="utf-8")
    manifest = {path.name: {"size": path.stat().st_size, "sha256": sha256(path)}
                for path in sorted(OUT.iterdir())}
    save(RECORD / "asset-manifest.json", manifest)
    notes = (ROOT / "macos/RELEASE_NOTES.md").read_text(encoding="utf-8")
    notes += "\n## 安装文件校验\n\n| 文件 | 字节 | SHA256 |\n|---|---:|---|\n"
    for name in PARTS:
        info = manifest[name]
        notes += f"| `{name}` | {info['size']} | `{info['sha256']}` |\n"
    (RECORD / "release-notes.md").write_text(notes, encoding="utf-8")
    print(json.dumps(verification, ensure_ascii=False), flush=True)


def check_assets(release: dict, manifest: dict) -> None:
    assets = {asset["name"]: asset for asset in release["assets"]}
    require(set(assets) == set(manifest), "Uploaded asset list differs from verified manifest")
    for name, expected in manifest.items():
        actual = assets[name]
        require(actual["state"] == "uploaded" and actual["size"] == expected["size"],
                f"Incomplete uploaded asset: {name}")
        require(actual.get("digest") == "sha256:" + expected["sha256"],
                f"Uploaded asset checksum mismatch: {name}")


def publish() -> None:
    manifest = json.loads((RECORD / "asset-manifest.json").read_text(encoding="utf-8"))
    for name, expected in manifest.items():
        require(sha256(OUT / name) == expected["sha256"], "Asset changed after verification")
    # Creation fails safely if this release already exists; never replace another release or asset.
    run("gh", "release", "create", TAG, "--repo", REPO, "--target", BUILD_SHA,
        "--draft", "--prerelease", "--title", "声年 Mac 独立应用 · 测试版 1",
        "--notes-file", RECORD / "release-notes.md")
    for name in manifest:
        run("gh", "release", "upload", TAG, OUT / name, "--repo", REPO)
    draft = api(f"releases/tags/{TAG}")
    require(draft["draft"] and draft["prerelease"], "Unexpected release state before publication")
    check_assets(draft, manifest)
    require(draft["target_commitish"] == BUILD_SHA, "Draft targets unexpected source")
    run("gh", "release", "edit", TAG, "--repo", REPO, "--draft=false")
    published = api(f"releases/tags/{TAG}")
    require(not published["draft"] and published["prerelease"], "Release was not published")
    require(published["body"].strip() == (RECORD / "release-notes.md").read_text(encoding="utf-8").strip(),
            "Release notes changed during publication")
    check_assets(published, manifest)
    ref = api(f"git/ref/tags/{TAG}")
    require(ref["object"]["sha"] == BUILD_SHA, "Release tag points to unexpected source")
    save(RECORD / "published-release.json", published)
    print("Published and verified:", published["html_url"], flush=True)


if __name__ == "__main__":
    os.chdir(ROOT)
    {"prepare": prepare, "publish": publish}[sys.argv[1]]()
