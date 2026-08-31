"""今日/昨日简报的单一事实来源与状态判断。

经典视图和卡片工作台都必须通过这里解析同一份本地成品，避免两套界面
各自判断文件、各自兜底，最终出现一边旧内容、一边空白的情况。
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from pathlib import Path


_BRIEF_DATE_RE = re.compile(r"^\s*#\s*每日简报\s+(\d{4}-\d{2}-\d{2})\s*$", re.M)


@dataclass(frozen=True)
class BriefSource:
    card_id: str
    expected_day: dt.date
    output_path: Path
    transcript_path: Path
    pending_path: Path
    content: str = ""
    output_day: dt.date | None = None
    pending_error: str = ""

    @property
    def has_output(self) -> bool:
        return bool(self.content.strip())

    @property
    def has_transcript(self) -> bool:
        try:
            return self.transcript_path.is_file() and self.transcript_path.stat().st_size > 0
        except OSError:
            return False

    @property
    def is_stale(self) -> bool:
        return (
            self.card_id == "today_brief"
            and self.output_day is not None
            and self.output_day < self.expected_day
            and self.has_transcript
        )


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8") if path.is_file() else ""
    except OSError:
        return ""


def _pending_error(path: Path) -> str:
    raw = _read_text(path)
    if not raw:
        return ""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    value = str(payload.get("error") or "").strip()
    if "Token 余额不足" in value or "额度不足" in value:
        return "你的 AI API 额度不足，原始语音已安全保留；补充额度后可继续生成。"
    return value


def _brief_output_day(content: str) -> dt.date | None:
    match = _BRIEF_DATE_RE.search(content or "")
    if not match:
        return None
    try:
        return dt.date.fromisoformat(match.group(1))
    except ValueError:
        return None


def resolve_today_brief(
    data_root: Path,
    knowledge_root: Path,
    *,
    today: dt.date | None = None,
) -> BriefSource:
    day = today or dt.date.today()
    output = knowledge_root / "每日简报.md"
    content = _read_text(output)
    return BriefSource(
        card_id="today_brief",
        expected_day=day,
        output_path=output,
        transcript_path=data_root / "transcripts" / f"{day.isoformat()}.jsonl",
        pending_path=data_root / "notes" / f"{day.isoformat()}.pending.json",
        content=content,
        output_day=_brief_output_day(content),
        pending_error=_pending_error(
            data_root / "notes" / f"{day.isoformat()}.pending.json"
        ),
    )


def resolve_yesterday_brief(
    data_root: Path,
    *,
    today: dt.date | None = None,
) -> BriefSource:
    day = (today or dt.date.today()) - dt.timedelta(days=1)
    output = data_root / "notes" / f"{day.isoformat()}-review.md"
    return BriefSource(
        card_id="yesterday_brief",
        expected_day=day,
        output_path=output,
        transcript_path=data_root / "transcripts" / f"{day.isoformat()}.jsonl",
        pending_path=data_root / "notes" / f"{day.isoformat()}.pending.json",
        content=_read_text(output),
        output_day=day if output.is_file() else None,
        pending_error=_pending_error(
            data_root / "notes" / f"{day.isoformat()}.pending.json"
        ),
    )


def pending_hint(source: BriefSource) -> str:
    if source.pending_error:
        return source.pending_error
    if source.has_transcript:
        return "有语音记录，但简报尚未整理完成。点击“生成”即可继续。"
    return "暂无可整理的语音记录。"
