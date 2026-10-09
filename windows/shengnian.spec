# -*- mode: python ; coding: utf-8 -*-
import os
import shutil
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parent
SRC = ROOT / "src"
# Build with only Python and Windows on PATH. Host document/media runtimes can
# otherwise contribute incompatible ICU/OpenSSL DLLs to this application.
os.environ["PATH"] = os.pathsep.join([
    str(Path(sys.executable).parent), sys.base_prefix,
    str(Path(sys.base_prefix) / "DLLs"),
    str(Path(os.environ["SystemRoot"]) / "System32"),
    os.environ["SystemRoot"],
])
MODEL_ROOT = Path(os.environ["VOICE_JOURNAL_MODEL_ROOT"])
FFMPEG = Path(os.environ["VOICE_JOURNAL_FFMPEG_ROOT"])
slots = {
    "asr": "speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
    "vad": "speech_fsmn_vad_zh-cn-16k-common-pytorch",
    "punc": "punc_ct-transformer_cn-en-common-vocab471067-large",
    "speaker": "speech_campplus_sv_zh-cn_16k-common",
}
datas = [
    (str(SRC / "config.example.toml"), "."),
    (str(ROOT / "windows/defaults"), "defaults"),
    (str(ROOT / "prompts"), "prompts"),
    (str(ROOT / "assets"), "assets"),
    (str(ROOT / "LICENSE"), "licenses"),
    (str(FFMPEG / "LICENSE.txt"), "tools"),
]
for slot, name in slots.items():
    directory = MODEL_ROOT / name
    marker = "campplus_cn_common.bin" if slot == "speaker" else "model.pt"
    assert (directory / marker).is_file(), directory
    for file in directory.rglob("*"):
        relative = file.relative_to(directory)
        if file.is_file() and not any(p.startswith(".") or p in {"fig", "example"} for p in relative.parts):
            datas.append((str(file), (Path("models") / slot / relative.parent).as_posix()))
for package in ("funasr", "modelscope", "jieba"):
    datas += collect_data_files(package, include_py_files=package == "funasr")
for package in ("funasr", "modelscope", "yt-dlp", "webrtcvad-wheels"):
    datas += copy_metadata(package)
hidden = [p.stem for p in SRC.glob("*.py") if p.stem not in {"mac_entry", "windows_entry", "mac_frozen_check"}]
hidden += collect_submodules("cards")
hidden += collect_submodules("funasr", filter=lambda name: not name.startswith("funasr.frontends.utils.dnn_wpe"))
hidden += collect_submodules("modelscope")
hidden += collect_submodules("yt_dlp")
a = Analysis(
    [str(SRC / "windows_entry.py")], pathex=[str(SRC)], binaries=[], datas=datas,
    hiddenimports=hidden, hookspath=[str(ROOT / "macos/hooks")],
    excludes=["rookiepy", "pytorch_wpe", "torch_complex", "tensorflow", "PyQt5", "PyQt6", "PySide2"],
    noarchive=False, optimize=0,
)
assert not any("codex-primary-runtime" in source for _, source, _ in a.binaries), "Unrelated host DLL entered bundle"
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="声年", console=False, debug=False, strip=False, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="声年")
destination = Path(DISTPATH) / "声年" / "_internal/tools"
destination.mkdir(parents=True, exist_ok=True)
for file in (FFMPEG / "bin").iterdir():
    if file.name == "ffmpeg.exe" or file.suffix.lower() == ".dll":
        shutil.copy2(file, destination / file.name)
assert (destination / "ffmpeg.exe").is_file()
