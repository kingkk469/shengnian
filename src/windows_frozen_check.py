"""Validate the shipped Windows executable in a disposable data directory."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys
import time


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("basic", "models", "ui", "api-reload"), required=True)
    args = parser.parse_args(argv)
    from common import ROOT, RESOURCE_ROOT, CONFIG
    assert getattr(sys, "frozen", False) and sys.platform == "win32"
    started = time.monotonic()
    report = {"mode": args.mode, "frozen": True}
    if args.mode == "basic":
        from audio_import import _ffmpeg_executable
        import subprocess
        import recorder
        import transcriber
        import webrtcvad
        from runtime_profile import is_commercial_mode
        assert not is_commercial_mode()
        assert CONFIG["paths"]["root"] == "__AUTO__"
        assert (ROOT / "config.toml").is_file()
        assert (ROOT / "hotwords.txt").is_file()
        assert not webrtcvad.Vad(2).is_speech(b"\0" * 640, 16000)
        decoder = _ffmpeg_executable()
        result = subprocess.run([decoder, "-version"], capture_output=True, timeout=20, check=True)
        report.update(ffmpeg=result.stdout.decode("utf-8").splitlines()[0], commercial_mode=False, workers_imported=True)
    elif args.mode == "api-reload":
        assert os.environ.get("DEEPSEEK_API_KEY") == "synthetic-test-key"
        report["api_reload"] = True
    elif args.mode == "models":
        import numpy as np
        import soundfile as sf
        from audio_import import _normalize_with_ffmpeg
        from transcriber import get_model, get_sv_model, transcribe_wav, extract_embedding
        fixture = next((RESOURCE_ROOT / "models/asr").rglob("*.wav"))
        samples, rate = sf.read(fixture)
        compressed = ROOT / "runtime/public-fixture.flac"
        sf.write(compressed, samples, rate)
        normalized = ROOT / "runtime/public-fixture.wav"
        assert _normalize_with_ffmpeg(compressed, normalized) > 0
        get_model()
        text = transcribe_wav(normalized)
        assert text and len(text.strip()) >= 4, repr(text)
        get_sv_model()
        vector = extract_embedding(normalized)
        assert vector is not None and len(vector) == 192 and np.isfinite(vector).all()
        report.update(transcript=text, speaker_dimensions=len(vector), local_inference=True, compressed_audio_import=True)
    else:
        from unittest import mock
        from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QLineEdit, QMessageBox
        from PySide6.QtCore import QTimer
        import launcher
        from api_settings import show_api_dialog
        app = QApplication([])
        app.setStyleSheet(launcher.QSS)
        with mock.patch.object(launcher.QTimer, "singleShot"), mock.patch.object(launcher.threading.Thread, "start"):
            window = launcher.Launcher()
            for timer in window.findChildren(QTimer):
                timer.stop()
            window.show()
            app.processEvents()
            assert window.btn_account.text() == "API 配置"
            assert window.grab().save(str(ROOT / "runtime/windows-main.png"))
            history = launcher.HistoryWindow(window)
            history.show()
            app.processEvents()
            history.close()
            with mock.patch.object(QMessageBox, "exec"), mock.patch.object(launcher, "open_path"):
                window._on_files()
        def save_dialog():
            dialog = app.activeModalWidget()
            assert isinstance(dialog, QDialog)
            dialog.findChildren(QLineEdit)[0].setText("synthetic-test-key")
            assert dialog.grab().save(str(ROOT / "runtime/windows-api.png"))
            buttons = dialog.findChild(QDialogButtonBox)
            buttons.button(QDialogButtonBox.StandardButton.Save).click()
        QTimer.singleShot(250, save_dialog)
        assert show_api_dialog(window, ROOT) == QDialog.DialogCode.Accepted
        assert json.loads((ROOT / "runtime/api-keys.json").read_text(encoding="utf-8"))["DEEPSEEK_API_KEY"] == "synthetic-test-key"
        window.close()
        report.update(main_window=True, history_window=True, api_dialog_saved=True, microphone_or_ai_calls=0, qt_platform=app.platformName())
    report.update(status="passed", elapsed_sec=round(time.monotonic() - started, 2))
    (ROOT / "runtime" / f"windows-self-test-{args.mode}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
