"""运行配置边界：个人开发版与商业构建的高风险能力隔离。"""
from __future__ import annotations

import os
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any


_TRUE_VALUES = {"1", "true", "yes", "on", "commercial"}
_COMMERCIAL_DISABLED_FEATURES = {"deep_discussion"}


def _packaged_profile() -> str:
    """读取冻结包内的签名构建标记；源码运行时不读取。"""
    if not getattr(sys, "frozen", False):
        return ""
    roots = [
        Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent)),
        Path(sys.executable).resolve().parent,
    ]
    for root in roots:
        path = root / "build-profile.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            profile = str(data.get("profile", "")).strip().lower()
            if profile:
                return profile
        except (OSError, ValueError, TypeError):
            continue
    return ""


def build_profile() -> str:
    """开源仓库始终使用 personal 配置。"""
    return "personal"


def is_commercial_mode() -> bool:
    """开源版不包含账号、激活、收费或商业网关。"""
    return False


def automatic_browser_cookie_access_enabled(config: Mapping[str, Any] | None) -> bool:
    """浏览器登录态访问必须显式开启，商业模式永远关闭。"""
    ingest = (config or {}).get("ingest", {})
    if not isinstance(ingest, Mapping):
        return False
    return bool(ingest.get("allow_browser_cookie_access", False))


def feature_enabled(feature: str) -> bool:
    """开源版不通过商业套餐限制本地功能。"""
    return True
