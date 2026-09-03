"""Build the audio decoder from pinned upstream source on Apple Silicon."""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import urllib.request

PROJECT = Path(__file__).resolve().parents[1]
VERSION = "9.0.1"
SHA256 = "cf38e0e28c7e5605942c4a77755349b0145804a397af37eb1fb4c77cb237f635"
URL = f"https://ffmpeg.org/releases/ffmpeg-{VERSION}.tar.xz"


def main():
    if sys.platform != "darwin":
        raise SystemExit("Build FFmpeg on macOS")
    work = PROJECT / "assets-heavy/ffmpeg-source"
    work.mkdir(parents=True, exist_ok=True)
    archive = work / f"ffmpeg-{VERSION}.tar.xz"
    if not archive.exists():
        urllib.request.urlretrieve(URL, archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
        raise RuntimeError("FFmpeg source checksum mismatch")
    with tarfile.open(archive) as source:
        source.extractall(work, filter="data")
    folder = work / f"ffmpeg-{VERSION}"
    options = [
        "./configure", "--arch=arm64", "--target-os=darwin", "--cc=clang",
        "--disable-autodetect", "--disable-gpl", "--disable-nonfree",
        "--disable-shared", "--enable-static", "--disable-doc", "--disable-debug",
        "--disable-network", "--disable-ffplay", "--disable-ffprobe", "--disable-devices",
        "--disable-videotoolbox", "--disable-audiotoolbox", "--enable-zlib",
        "--disable-encoders", "--enable-encoder=pcm_s16le",
        "--disable-decoders",
        "--enable-decoder=aac,aac_fixed,mp3,mp3float,mp3adu,mp3adufloat,mp3on4,mp3on4float,alac,flac,opus,vorbis,wmav1,wmav2,wmapro,wmavoice,pcm_s16le,pcm_s24le,pcm_s32le,pcm_f32le,pcm_f64le,pcm_s16be,pcm_s24be,pcm_s32be,pcm_f32be,pcm_f64be,pcm_u8,pcm_alaw,pcm_mulaw",
        "--disable-filters", "--enable-filter=aresample,aformat,anull,atrim,abuffer,abuffersink",
    ]
    subprocess.run(options, cwd=folder, check=True)
    subprocess.run(["make", f"-j{min(4, os.cpu_count() or 2)}", "ffmpeg"], cwd=folder, check=True)
    resources = PROJECT / "assets-heavy/macos-bundle"
    tools = resources / "tools"
    licenses = resources / "licenses/ffmpeg"
    tools.mkdir(parents=True, exist_ok=True)
    licenses.mkdir(parents=True, exist_ok=True)
    shutil.copy2(folder / "ffmpeg", tools / "ffmpeg")
    (tools / "ffmpeg").chmod(0o755)
    version = subprocess.run([str(tools / "ffmpeg"), "-version"], capture_output=True, text=True, check=True).stdout
    if "--enable-gpl" in version or "--enable-nonfree" in version:
        raise RuntimeError("Unexpected FFmpeg license configuration")
    (licenses / "ffmpeg-build.txt").write_text(version, encoding="utf-8")
    shutil.copy2(archive, licenses / archive.name)
    shutil.copy2(Path(__file__), licenses / "build_ffmpeg.py")
    for name in ("COPYING.LGPLv2.1", "COPYING.LGPLv3", "LICENSE.md"):
        if (folder / name).is_file():
            shutil.copy2(folder / name, licenses / name)


if __name__ == "__main__":
    main()
