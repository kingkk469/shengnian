"""Run in a separate process with disposable data, without microphones or AI."""
import datetime as dt
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest import mock

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["VOICE_JOURNAL_CONFIG"] = str(PROJECT / "src/config.example.toml")
import launcher
from common import ROOT, append_jsonl, transcript_path
from PySide6.QtWidgets import QApplication

app = QApplication([])
day = dt.date.today()
append_jsonl(transcript_path(day), {
    "start": f"{day}T09:00:00", "end": f"{day}T09:00:05", "duration_sec": 5,
    "text": "这是 Mac 历史界面的合成测试记录。", "source": "live", "wav": "raw/synthetic.wav",
})
fake_system = SimpleNamespace(platform="darwin", executable=sys.executable)
with mock.patch.object(launcher, "sys", fake_system), mock.patch.object(launcher.QTimer, "singleShot"), mock.patch.object(launcher.threading.Thread, "start"), mock.patch.object(launcher.ProcessHandle, "start", side_effect=AssertionError("不得启动录音或转写")):
    window = launcher.Launcher()
    for timer in window.findChildren(launcher.QTimer):
        timer.stop()
    window.show()
    app.processEvents()
    assert "Mac 测试版" in window.windowTitle()
    assert launcher.VENV_PY.is_file()
    history = launcher.HistoryWindow(window)
    history.show()
    if history.day_list.count():
        history.day_list.setCurrentRow(0)
    app.processEvents()
    assert history.table.rowCount() == 1, history.table.rowCount()
    assert "合成测试记录" in history.table.item(0, 5).text()
    output = ROOT / "desktop-smoke.png"
    assert window.grab().save(str(output))
    history.close()
    window.close()
print(json.dumps({"status": "passed", "history_rows": 1, "microphone_or_ai_calls": 0, "screenshot": str(output)}, ensure_ascii=False))
