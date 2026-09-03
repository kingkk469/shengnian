from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import common  # noqa: E402
import launcher  # noqa: E402


class _FakeIndex:
    def __init__(self, row: int):
        self._row = row

    def row(self) -> int:
        return self._row


class _FakeItem:
    def __init__(self, text: str):
        self._text = text

    def text(self) -> str:
        return self._text


class _FakeTable:
    def selectedIndexes(self):
        return [_FakeIndex(1), _FakeIndex(1)]

    def item(self, row: int, column: int):
        values = {
            (1, 1): "08:27:08",
            (1, 5): "需要删除的测试记录",
        }
        return _FakeItem(values[(row, column)])


class _FakeLabel:
    def __init__(self):
        self.text = ""

    def setText(self, text: str):
        self.text = text


class _FakeMessageBox:
    YesRole = object()
    NoRole = object()
    RejectRole = object()
    selected_button = 0

    def __init__(self, parent=None):
        self.buttons = []
        self._clicked = None

    def setWindowTitle(self, title: str):
        pass

    def setText(self, text: str):
        pass

    def addButton(self, text: str, role):
        button = object()
        self.buttons.append(button)
        return button

    def exec(self):
        self._clicked = self.buttons[self.selected_button]
        # Qt 6.11 returns 2/3/4 for these three custom buttons.  The old
        # implementation incorrectly treated the first button's 2 as cancel.
        return 2 + self.selected_button

    def clickedButton(self):
        return self._clicked

    @staticmethod
    def information(*args, **kwargs):
        pass

    @staticmethod
    def warning(*args, **kwargs):
        pass


@pytest.mark.parametrize("separator", ["/", "\\"])
def test_delete_segments_removes_text_and_corresponding_wav(tmp_path, monkeypatch, separator):
    monkeypatch.setattr(common, "ROOT", tmp_path)
    day = dt.date(2026, 7, 28)
    raw_dir = tmp_path / "raw" / day.isoformat()
    raw_dir.mkdir(parents=True)
    keep_wav = raw_dir / "keep.wav"
    delete_wav = raw_dir / "delete.wav"
    keep_wav.write_bytes(b"keep")
    delete_wav.write_bytes(b"delete")
    records = [
        {"text": "保留", "wav": f"raw/{day.isoformat()}/keep.wav"},
        {"text": "删除", "wav": separator.join(["raw", day.isoformat(), "delete.wav"])},
    ]
    common.write_jsonl(common.transcript_path(day), records)

    deleted_segments, deleted_wavs = common.delete_segments(
        day,
        [1],
        delete_wav=True,
    )

    assert (deleted_segments, deleted_wavs) == (1, 1)
    assert list(common.read_jsonl(common.transcript_path(day))) == [records[0]]
    assert keep_wav.exists()
    assert not delete_wav.exists()


def test_delete_segments_keeps_text_when_wav_cannot_be_deleted(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "ROOT", tmp_path)
    day = dt.date(2026, 7, 28)
    raw_dir = tmp_path / "raw" / day.isoformat()
    raw_dir.mkdir(parents=True)
    wav = raw_dir / "busy.wav"
    wav.write_bytes(b"busy")
    records = [{"text": "必须保留到音频成功删除", "wav": f"raw/{day.isoformat()}/busy.wav"}]
    common.write_jsonl(common.transcript_path(day), records)

    with (
        patch.object(Path, "unlink", side_effect=PermissionError("in use")),
        pytest.raises(OSError, match="无法删除音频文件"),
    ):
        common.delete_segments(day, [0], delete_wav=True)

    assert list(common.read_jsonl(common.transcript_path(day))) == records
    assert wav.exists()


def test_history_delete_uses_clicked_button_instead_of_exec_return_code():
    fake_window = SimpleNamespace(
        current_day=dt.date(2026, 7, 28),
        table=_FakeTable(),
        bottom_label=_FakeLabel(),
        _load_day=lambda day: None,
    )
    _FakeMessageBox.selected_button = 0

    with (
        patch.object(launcher, "QMessageBox", _FakeMessageBox),
        patch.object(launcher, "delete_segments", return_value=(1, 1)) as delete,
    ):
        launcher.HistoryWindow._delete_selected(fake_window)

    delete.assert_called_once_with(
        dt.date(2026, 7, 28),
        [1],
        delete_wav=True,
    )
    assert fake_window.bottom_label.text == "已删 1 段 · 1 个 WAV"


def test_history_delete_cancel_keeps_records():
    fake_window = SimpleNamespace(
        current_day=dt.date(2026, 7, 28),
        table=_FakeTable(),
        bottom_label=_FakeLabel(),
        _load_day=lambda day: None,
    )
    _FakeMessageBox.selected_button = 2

    with (
        patch.object(launcher, "QMessageBox", _FakeMessageBox),
        patch.object(launcher, "delete_segments") as delete,
    ):
        launcher.HistoryWindow._delete_selected(fake_window)

    delete.assert_not_called()
