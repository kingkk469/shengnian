"""Checks run on the Mac build host against the actual frozen executable."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("basic", "models", "ui", "lock-probe"), required=True)
    args = parser.parse_args(argv)
    from common import ROOT, RESOURCE_ROOT, CONFIG
    from platform_support import RoleLock, locked_role_pid
    if args.mode == "lock-probe":
        lock = RoleLock(ROOT, "transcriber")
        acquired = lock.acquire()
        lock.release()
        raise SystemExit(0 if acquired else 3)
    started = time.monotonic()
    report = {"mode": args.mode, "architecture": platform.machine(), "frozen": bool(getattr(sys, "frozen", False))}
    assert report["frozen"], "This check must run against the packaged app"
    assert sys.platform == "darwin" and platform.machine() == "arm64"
    if args.mode == "basic":
        from audio_import import _ffmpeg_executable
        import webrtcvad
        import sounddevice
        import launcher
        import api_settings
        import yt_dlp
        assert CONFIG["paths"]["root"] == "__AUTO__"
        assert (ROOT / "config.toml").is_file()
        assert (ROOT / "hotwords.txt").is_file()
        decoder = _ffmpeg_executable()
        assert decoder and Path(decoder).is_file()
        result = subprocess.run([decoder, "-version"], capture_output=True, text=True, timeout=20, check=True)
        report["ffmpeg"] = result.stdout.splitlines()[0]
        downloader = subprocess.run(
            [sys.executable, "--role", "yt-dlp", "--version"],
            capture_output=True, text=True, timeout=60, check=True,
        )
        assert yt_dlp.version.__version__ in downloader.stdout, downloader.stdout
        report["yt_dlp"] = downloader.stdout.strip()
        assert not webrtcvad.Vad(2).is_speech(b"\0" * 640, 16000)
        lock = RoleLock(ROOT, "transcriber")
        assert lock.acquire()
        try:
            assert locked_role_pid(ROOT, "transcriber") == os.getpid()
            child = subprocess.run([sys.executable, "--role", "mac-self-test", "--mode", "lock-probe"], timeout=60)
            assert child.returncode == 3, child.returncode
        finally:
            lock.release()
        assert locked_role_pid(ROOT, "transcriber") is None
        report.update(worker_lock=True, vad=True, bundled_ffmpeg=True, physical_microphone_tested=False)
    elif args.mode == "models":
        import numpy as np
        from transcriber import get_model, get_sv_model, transcribe_wav, extract_embedding
        fixtures = sorted((RESOURCE_ROOT / "models/asr").rglob("*.wav"))
        assert fixtures, "No public model test fixture was bundled"
        fixture = fixtures[0]
        # Exercise the shipped decoder and resampler on a compressed fixture.
        import soundfile as sf
        from audio_import import _normalize_with_ffmpeg
        samples, rate = sf.read(fixture)
        compressed = ROOT / "runtime/public-fixture.flac"
        sf.write(compressed, samples, rate)
        normalized = ROOT / "runtime/public-fixture.wav"
        assert _normalize_with_ffmpeg(compressed, normalized) > 0
        decoded = sf.info(normalized)
        assert decoded.samplerate == 16000 and decoded.channels == 1
        get_model()
        text = transcribe_wav(normalized)
        assert text and len(text.strip()) >= 4, repr(text)
        get_sv_model()
        embedding = extract_embedding(fixture)
        assert embedding is not None and len(embedding) == 192
        assert np.isfinite(embedding).all()
        report.update(public_fixture=fixture.name, transcript=text, speaker_dimensions=len(embedding), local_inference=True, compressed_audio_import=True)
    else:
        from unittest import mock
        from PySide6.QtWidgets import QApplication
        import launcher
        app = QApplication([])
        assert app.platformName() == "cocoa", app.platformName()
        app.setStyleSheet(launcher.scale_stylesheet_font_sizes(launcher.QSS, launcher.load_font_scale(ROOT)))
        with mock.patch.object(launcher.QTimer, "singleShot"), mock.patch.object(launcher.threading.Thread, "start"):
            window = launcher.Launcher()
            for timer in window.findChildren(launcher.QTimer):
                timer.stop()
            window.show()
            app.processEvents()
            history = launcher.HistoryWindow(window)
            history.show()
            app.processEvents()
            screenshot = ROOT / "runtime/mac-app.png"
            assert window.grab().save(str(screenshot))
            report.update(qt_platform=app.platformName(), main_window=True, history_window=True, screenshot=str(screenshot))
            history.close()
            window.close()
    report.update(status="passed", elapsed_sec=round(time.monotonic() - started, 2))
    target = ROOT / "runtime" / f"mac-self-test-{args.mode}.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)
