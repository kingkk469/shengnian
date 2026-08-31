"""用户自有 OpenAI 兼容 API 的轻量调用入口。

声年开源版不经过产品账号、计费服务或 Token 网关。API Key 只从当前
进程的环境变量读取。可选的本地用量日志只记录模型、耗时和 token 数，
不记录提示词、回复正文或 Key。
"""
from __future__ import annotations

import datetime as dt
import inspect
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from openai import OpenAI as _SDKOpenAI


DEEPSEEK_V4_FLASH = "deepseek-v4-flash"
_DEPRECATED_DEEPSEEK_ALIASES = {"deepseek-chat", "deepseek-reasoner"}
_FALSE_VALUES = {"0", "false", "no", "off", "disabled"}
_METER_LOCK = threading.Lock()
_WARNED_MODELS: set[str] = set()
_LOG = logging.getLogger("voice-journal.ai-gateway")


def direct_provider_enabled() -> bool:
    return True


def provider_api_key(env_name: str) -> str:
    """只读取用户本机环境变量，不读取项目配置或远端凭证。"""
    return os.environ.get(env_name, "")


def normalize_model_name(model: str | None) -> str | None:
    if model not in _DEPRECATED_DEEPSEEK_ALIASES:
        return model
    if model not in _WARNED_MODELS:
        _WARNED_MODELS.add(model)
        _LOG.warning("DeepSeek 模型别名 %s 已迁移为 %s", model, DEEPSEEK_V4_FLASH)
    return DEEPSEEK_V4_FLASH


def shadow_metering_enabled() -> bool:
    raw = os.environ.get("VOICE_JOURNAL_AI_SHADOW_METERING", "0")
    return raw.strip().lower() not in _FALSE_VALUES


def _meter_dir() -> Path:
    override = os.environ.get("VOICE_JOURNAL_AI_METER_DIR", "").strip()
    if override:
        return Path(override)
    try:
        from common import ROOT

        return ROOT / "runtime" / "ai-usage"
    except Exception:
        return Path(__file__).resolve().parent.parent / "runtime" / "ai-usage"


def _caller_feature() -> str:
    frame = inspect.currentframe()
    try:
        frame = frame.f_back if frame else None
        while frame:
            module = str(frame.f_globals.get("__name__", "unknown"))
            if module != __name__ and not module.startswith("openai"):
                return f"{module}.{frame.f_code.co_name}"
            frame = frame.f_back
    finally:
        del frame
    return "unknown"


def _usage_value(usage: Any, name: str) -> int:
    value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, 0)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _append_meter_record(record: dict[str, Any]) -> None:
    if not shadow_metering_enabled():
        return
    try:
        directory = _meter_dir()
        directory.mkdir(parents=True, exist_ok=True)
        day = dt.datetime.now(dt.timezone.utc).date().isoformat()
        path = directory / f"{day}-{os.getpid()}.jsonl"
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with _METER_LOCK, path.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except Exception as exc:
        _LOG.warning("本地 API 用量记录失败: %s", type(exc).__name__)


class _CompletionsProxy:
    def __init__(self, inner: Any, base_url: str):
        self._inner = inner
        self._base_url = base_url

    def create(self, *args: Any, **kwargs: Any) -> Any:
        requested_model = kwargs.get("model")
        resolved_model = normalize_model_name(requested_model)
        if requested_model is not None:
            kwargs["model"] = resolved_model
        if (
            isinstance(resolved_model, str)
            and resolved_model.startswith("deepseek-")
            and "extra_body" not in kwargs
        ):
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

        request_id = str(uuid.uuid4())
        started = time.perf_counter()
        base_record = {
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "request_id": request_id,
            "feature": _caller_feature(),
            "model": resolved_model,
            "base_url_host": self._base_url.split("//")[-1].split("/")[0],
        }
        try:
            response = self._inner.create(*args, **kwargs)
        except Exception as exc:
            _append_meter_record({
                **base_record,
                "status": "error",
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "error_type": type(exc).__name__,
            })
            raise

        usage = getattr(response, "usage", None)
        _append_meter_record({
            **base_record,
            "status": "ok",
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "prompt_tokens": _usage_value(usage, "prompt_tokens"),
            "completion_tokens": _usage_value(usage, "completion_tokens"),
        })
        return response

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _ChatProxy:
    def __init__(self, inner: Any, base_url: str):
        self._inner = inner
        self.completions = _CompletionsProxy(inner.completions, base_url)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class OpenAI:
    """兼容 ``openai.OpenAI`` 的本地包装，只增加可关闭的无正文用量记录。"""

    def __init__(self, *args: Any, **kwargs: Any):
        self._client = _SDKOpenAI(*args, **kwargs)
        base_url = str(kwargs.get("base_url") or getattr(self._client, "base_url", ""))
        self.chat = _ChatProxy(self._client.chat, base_url)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "OpenAI":
        self._client.__enter__()
        return self

    def __exit__(self, *args: Any) -> Any:
        return self._client.__exit__(*args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)
