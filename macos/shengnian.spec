# -*- mode: python ; coding: utf-8 -*-
import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parent
SRC = ROOT / "src"
RES = ROOT / "assets-heavy" / "macos-bundle"
datas = [
    (str(SRC / "config.example.toml"), "."),
    (str(RES / "defaults"), "defaults"),
    (str(ROOT / "prompts"), "prompts"),
    (str(ROOT / "assets"), "assets"),
    (str(ROOT / "LICENSE"), "licenses"),
    (str(ROOT / "macos/THIRD-PARTY.md"), "licenses"),
    (str(RES / "licenses"), "licenses/dependencies"),
    (str(RES / "models.json"), "licenses"),
]
for slot in ("asr", "vad", "punc", "speaker"):
    model_dir = RES / "models" / slot
    for file in model_dir.rglob("*"):
        if file.is_file() and not any(part.startswith(".") or part == "fig" for part in file.relative_to(model_dir).parts):
            datas.append((str(file), (Path("models") / slot / file.relative_to(model_dir).parent).as_posix()))
datas += collect_data_files("funasr", include_py_files=True)
datas += collect_data_files("modelscope")
datas += collect_data_files("jieba")
for name in ("funasr", "modelscope", "yt-dlp", "webrtcvad-wheels"):
    datas += copy_metadata(name)
hidden = [p.stem for p in SRC.glob("*.py") if p.stem not in {"mac_entry"}]
hidden += collect_submodules("cards")
hidden += collect_submodules("funasr", filter=lambda name: not name.startswith("funasr.frontends.utils.dnn_wpe"))
hidden += collect_submodules("modelscope")
hidden += collect_submodules("yt_dlp")
identity = os.environ.get("MACOS_SIGNING_IDENTITY") or None
analysis = Analysis(
    [str(SRC / "mac_entry.py")], pathex=[str(SRC)],
    binaries=[(str(RES / "tools/ffmpeg"), "tools")], datas=datas,
    hiddenimports=hidden, hookspath=[str(ROOT / "macos/hooks")],
    excludes=["pytorch_wpe", "torch_complex", "tensorflow", "PyQt5", "PyQt6", "PySide2"],
    noarchive=False, optimize=0,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz, analysis.scripts, [], exclude_binaries=True, name="Shengnian",
    console=False, debug=False, strip=False, upx=False, argv_emulation=False,
    target_arch="arm64", codesign_identity=identity,
    entitlements_file=str(ROOT / "macos/entitlements.plist"),
)
collection = COLLECT(exe, analysis.binaries, analysis.datas, name="Shengnian", strip=False, upx=False)
app = BUNDLE(
    collection, name="声年.app", bundle_identifier="com.king.shengnian",
    info_plist={
        "CFBundleName": "声年", "CFBundleDisplayName": "声年",
        "CFBundleShortVersionString": "0.3.1", "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "14.0", "NSHighResolutionCapable": True,
        "NSMicrophoneUsageDescription": "声年使用麦克风在你的电脑上录音并生成本地语音笔记。",
        "NSPrincipalClass": "NSApplication",
    },
)
