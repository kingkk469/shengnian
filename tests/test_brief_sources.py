import datetime as dt
import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from brief_sources import (
    pending_hint,
    resolve_today_brief,
    resolve_yesterday_brief,
)


def test_today_brief_detects_stale_shared_output(tmp_path: Path):
    knowledge = tmp_path / "notes" / "第二大脑"
    knowledge.mkdir(parents=True)
    (knowledge / "每日简报.md").write_text(
        "# 每日简报 2026-07-25\n\n旧简报", encoding="utf-8"
    )
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    (transcripts / "2026-07-30.jsonl").write_text(
        '{"text":"今天有记录"}\n', encoding="utf-8"
    )

    source = resolve_today_brief(
        tmp_path, knowledge, today=dt.date(2026, 7, 30)
    )

    assert source.output_path == knowledge / "每日简报.md"
    assert source.output_day == dt.date(2026, 7, 25)
    assert source.has_transcript
    assert source.is_stale


def test_yesterday_uses_review_but_detects_raw_transcript(tmp_path: Path):
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    (transcripts / "2026-07-29.jsonl").write_text(
        '{"text":"昨天确实有语音"}\n', encoding="utf-8"
    )

    source = resolve_yesterday_brief(
        tmp_path, today=dt.date(2026, 7, 30)
    )

    assert source.output_path == tmp_path / "notes" / "2026-07-29-review.md"
    assert not source.has_output
    assert source.has_transcript
    assert "有语音记录" in pending_hint(source)


def test_pending_quota_error_is_presented_as_actionable_hint(tmp_path: Path):
    notes = tmp_path / "notes"
    transcripts = tmp_path / "transcripts"
    notes.mkdir()
    transcripts.mkdir()
    (transcripts / "2026-07-29.jsonl").write_text("{}\n", encoding="utf-8")
    (notes / "2026-07-29.pending.json").write_text(
        json.dumps({"error": "AuthHTTPError('AI Token 余额不足。')"}),
        encoding="utf-8",
    )

    source = resolve_yesterday_brief(
        tmp_path, today=dt.date(2026, 7, 30)
    )

    assert "AI API 额度不足" in pending_hint(source)
    assert "原始语音已安全保留" in pending_hint(source)
