from __future__ import annotations

import datetime as dt
import json
import sys
import wave
from pathlib import Path


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from audio_import import (  # noqa: E402
    RecordingTimeGuess,
    import_recording,
    is_supported_audio,
    probe_recording,
)


def write_silent_wav(path: Path, seconds: float = 0.1) -> None:
    frames = int(16000 * seconds)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\x00\x00" * frames)


def test_supported_audio_extensions_are_case_insensitive() -> None:
    assert is_supported_audio("meeting.M4A")
    assert is_supported_audio("meeting.mp3")
    assert is_supported_audio("meeting.wav")
    assert not is_supported_audio("meeting.md")


def test_filename_datetime_is_reliable_enough_for_automatic_routing(
    tmp_path: Path,
) -> None:
    source = tmp_path / "recording_20260722_143500.wav"
    write_silent_wav(source)

    guess = probe_recording(source)

    assert guess.recorded_at == dt.datetime(2026, 7, 22, 14, 35)
    assert guess.source == "filename"
    assert guess.confidence == "medium"
    assert guess.needs_confirmation is False


def test_filesystem_time_requires_user_confirmation(tmp_path: Path) -> None:
    source = tmp_path / "meeting.wav"
    write_silent_wav(source)

    guess = probe_recording(source)

    assert guess.source in {"filesystem_created", "filesystem_modified"}
    assert guess.confidence == "low"
    assert guess.needs_confirmation is True


def test_import_preserves_original_routes_by_day_and_deduplicates(
    tmp_path: Path,
) -> None:
    source = tmp_path / "meeting.wav"
    write_silent_wav(source, seconds=0.2)
    root = tmp_path / "data"
    recorded_at = dt.datetime(2026, 7, 18, 9, 15, 30)
    guess = RecordingTimeGuess(
        source,
        recorded_at,
        "media_metadata",
        "high",
        False,
        0.2,
    )

    first = import_recording(guess, root)
    second = import_recording(guess, root)

    assert first.duplicate is False
    assert second.duplicate is True
    assert first.wav_path.parent == root / "raw" / "2026-07-18" / "imported"
    assert first.original_path.exists()
    assert source.exists()
    assert first.wav_path.exists()
    assert first.manifest_path.exists()

    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    assert manifest["local_only"] is True
    assert manifest["recorded_at"] == "2026-07-18T09:15:30"
    assert manifest["recording_time_source"] == "media_metadata"
    assert manifest["relative_timeline_ms"][0]["start_ms"] == 0
    assert manifest["relative_timeline_ms"][0]["end_ms"] >= 190


def test_user_confirmed_time_replaces_unreliable_filesystem_time(
    tmp_path: Path,
) -> None:
    source = tmp_path / "meeting.wav"
    write_silent_wav(source)
    guess = RecordingTimeGuess(
        source,
        dt.datetime(2026, 7, 29, 8, 0),
        "filesystem_created",
        "low",
        True,
        0.1,
    )

    result = import_recording(
        guess,
        tmp_path / "data",
        recorded_at=dt.datetime(2026, 7, 20, 21, 5),
    )
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert result.wav_path.parts[-3] == "2026-07-20"
    assert manifest["recording_time_source"] == "user_confirmed"
    assert manifest["recording_time_confidence"] == "confirmed"


def test_transcriber_uses_imported_recording_time_and_keeps_offsets(
    tmp_path: Path, monkeypatch
) -> None:
    import transcriber

    wav_path = tmp_path / "raw" / "2026-07-18" / "imported" / "meeting.wav"
    wav_path.parent.mkdir(parents=True)
    write_silent_wav(wav_path)
    wav_path.with_suffix(".import.json").write_text(
        json.dumps(
            {
                "recorded_at": "2026-07-18T09:15:30",
                "duration_sec": 0.1,
                "original_name": "手机会议.m4a",
                "original_copy": "raw/2026-07-18/imported/original/手机会议.m4a",
                "recording_time_source": "media_metadata",
                "recording_time_confidence": "high",
                "relative_timeline_ms": [{"start_ms": 0, "end_ms": 100}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    transcript = tmp_path / "transcripts" / "2026-07-18.jsonl"
    monkeypatch.setattr(transcriber, "ROOT", tmp_path)
    monkeypatch.setattr(
        transcriber,
        "transcript_path",
        lambda _day=None: transcript,
    )
    monkeypatch.setattr(
        transcriber,
        "transcribe_wav_detailed",
        lambda _path: ("会议内容", [[0, 100]]),
    )
    monkeypatch.setitem(transcriber.CONFIG["speaker"], "enabled", False)
    transcriber._done.clear()
    transcriber._processing.clear()

    transcriber.process_file(wav_path, "tx_import")

    record = json.loads(transcript.read_text(encoding="utf-8").splitlines()[0])
    assert record["start"] == "2026-07-18T09:15:30"
    assert record["original_name"] == "手机会议.m4a"
    assert record["relative_timeline_ms"] == [{"start_ms": 0, "end_ms": 100}]
    assert record["asr_timestamps_ms"] == [[0, 100]]
