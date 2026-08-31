"""声年本地界面偏好；不上传、不包含账号或正文。"""
from __future__ import annotations

import json
import re
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path


FONT_SCALES: tuple[float, ...] = (0.9, 1.0, 1.15)
DEFAULT_FONT_SCALE = 1.0
_FONT_SIZE_RE = re.compile(
    r"(?P<prefix>font-size\s*:\s*)(?P<size>\d+(?:\.\d+)?)(?P<unit>px)",
    re.IGNORECASE,
)


def _preference_path(data_root: str | Path) -> Path:
    return Path(data_root) / "runtime" / "ui-preferences.json"


def normalize_font_scale(value: object) -> float:
    try:
        candidate = float(value)
    except (TypeError, ValueError):
        return DEFAULT_FONT_SCALE
    return min(FONT_SCALES, key=lambda allowed: abs(allowed - candidate))


def load_font_scale(data_root: str | Path) -> float:
    path = _preference_path(data_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return normalize_font_scale(payload.get("font_scale"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return DEFAULT_FONT_SCALE


def save_font_scale(data_root: str | Path, scale: float) -> float:
    normalized = normalize_font_scale(scale)
    path = _preference_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {"schema_version": 1, "font_scale": normalized},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    temporary.replace(path)
    return normalized


def scale_stylesheet_font_sizes(stylesheet: str, scale: float) -> str:
    """只缩放样式表中的字体，不改变卡片尺寸、边距和拖拽布局。"""

    normalized = normalize_font_scale(scale)

    def replace(match: re.Match[str]) -> str:
        original = Decimal(match.group("size"))
        scaled = max(
            Decimal("8"),
            original * Decimal(str(normalized)),
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        rendered = format(scaled, "f").rstrip("0").rstrip(".")
        return f"{match.group('prefix')}{rendered}{match.group('unit')}"

    return _FONT_SIZE_RE.sub(replace, str(stylesheet))


__all__ = [
    "DEFAULT_FONT_SCALE",
    "FONT_SCALES",
    "load_font_scale",
    "normalize_font_scale",
    "save_font_scale",
    "scale_stylesheet_font_sizes",
]
