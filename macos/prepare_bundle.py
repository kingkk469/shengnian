"""Fetch public runtime resources on the Mac builder, never copy user data."""
from __future__ import annotations
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

PROJECT = Path(__file__).resolve().parents[1]
BUILD_RESOURCES = PROJECT / "assets-heavy" / "macos-bundle"
MODELS = {
    "asr": "iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
    "vad": "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
    "punc": "iic/punc_ct-transformer_cn-en-common-vocab471067-large",
    "speaker": "iic/speech_campplus_sv_zh-cn_16k-common",
}


def main():
    if sys.platform != "darwin":
        raise SystemExit("Run on the Mac builder")
    from modelscope import snapshot_download
    import imageio_ffmpeg
    BUILD_RESOURCES.mkdir(parents=True, exist_ok=True)
    defaults = BUILD_RESOURCES / "defaults"
    defaults.mkdir(exist_ok=True)
    shutil.copy2(PROJECT / "hotwords.example.txt", defaults / "hotwords.txt")
    (defaults / "corrections.json").write_text("{}\n", encoding="utf-8")
    license_dir = BUILD_RESOURCES / "licenses"
    license_dir.mkdir(exist_ok=True)
    for slot, model_id in MODELS.items():
        print(f"Downloading public model: {model_id}", flush=True)
        target = BUILD_RESOURCES / "models" / slot
        snapshot_download(model_id=model_id, local_dir=str(target))
        marker = "campplus_cn_common.bin" if slot == "speaker" else "model.pt"
        assert (target / marker).is_file(), target
    tools = BUILD_RESOURCES / "tools"
    tools.mkdir(exist_ok=True)
    ffmpeg = tools / "ffmpeg"
    shutil.copy2(imageio_ffmpeg.get_ffmpeg_exe(), ffmpeg)
    ffmpeg.chmod(0o755)
    version = subprocess.run([str(ffmpeg), "-version"], capture_output=True, text=True, check=True).stdout
    (license_dir / "ffmpeg-build.txt").write_text(version, encoding="utf-8")
    # Include installed distributions' license files and a version manifest.
    manifest = []
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name", "unknown")
        manifest.append({"name": name, "version": distribution.version})
        for entry in distribution.files or []:
            filename = Path(str(entry)).name.lower()
            if filename.startswith(("license", "copying", "notice")) and ".dist-info" in str(entry):
                source = Path(distribution.locate_file(entry))
                if source.is_file():
                    dest = license_dir / name / Path(str(entry)).name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, dest)
    (license_dir / "python-packages.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (BUILD_RESOURCES / "models.json").write_text(json.dumps(MODELS, indent=2), encoding="utf-8")
    print("Public model and decoder resources are ready", flush=True)


if __name__ == "__main__":
    main()
