"""受控的卡片领域模型。

本模块不依赖桌面 UI 或全局配置，便于在升级、测试和后台任务中复用。
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping


CARD_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
ALLOWED_CARD_TYPES = frozenset({"default", "custom"})
ALLOWED_SOURCES = frozenset(
    {"transcripts", "imports", "confirmed_cards", "todos", "done"}
)
ALLOWED_TIME_RANGES = frozenset(
    {"today", "yesterday", "last_7_days", "last_30_days", "all"}
)
CUSTOM_TIME_RANGE_RE = re.compile(
    r"^custom:(\d{4}-\d{2}-\d{2}):(\d{4}-\d{2}-\d{2})$"
)
ALLOWED_OUTPUT_TYPES = frozenset(
    {"text", "list", "short_video", "structured_todos", "structured_done"}
)
ALLOWED_TRIGGER_MODES = frozenset({"manual", "daily"})
ALLOWED_WIDTHS = frozenset({"narrow", "standard", "full"})
MIN_CARD_HEIGHT = 260
MAX_CARD_HEIGHT = 760
DEFAULT_CARD_HEIGHT = 320
ALLOWED_REVISION_KINDS = frozenset(
    {"initial", "generated", "ai", "manual", "restore", "imported"}
)
ALLOWED_PREFERENCE_SCOPES = frozenset({"card", "global"})

MAX_RULE_CHARS = 2_000
# 自建短提示词通常很短，但外部文字 Skill 往往包含完整方法论。允许完整
# 保存到本地卡片；服务端仍会按整体上下文和 Token 预算执行。
MAX_PROMPT_CHARS = 50_000
MAX_CARD_NAME_CHARS = 40
MAX_PURPOSE_CHARS = 300
MAX_DEPENDENCIES = 10


class CardError(RuntimeError):
    """卡片模块的基础异常。"""


class CardValidationError(CardError, ValueError):
    """输入不符合卡片安全边界。"""


class CardNotFoundError(CardError, LookupError):
    """目标卡片或版本不存在。"""


class CardLimitError(CardValidationError):
    """启用卡片或定时卡片超过产品上限。"""


class CardDependencyError(CardValidationError):
    """卡片依赖无效、成环或过深。"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_card_id() -> str:
    return f"card_{uuid.uuid4().hex[:20]}"


def new_record_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _clean_text(value: Any, *, field: str, limit: int, required: bool = False) -> str:
    if not isinstance(value, str):
        raise CardValidationError(f"{field} 必须是文本")
    cleaned = value.strip()
    if required and not cleaned:
        raise CardValidationError(f"{field} 不能为空")
    if len(cleaned) > limit:
        raise CardValidationError(f"{field} 不能超过 {limit} 个字符")
    if "\x00" in cleaned:
        raise CardValidationError(f"{field} 不能包含空字符")
    return cleaned


def _clean_enum(value: Any, *, field: str, allowed: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        choices = "、".join(sorted(allowed))
        raise CardValidationError(f"{field} 只能是：{choices}")
    return value


def clean_time_range(value: Any) -> str:
    if isinstance(value, str) and value in ALLOWED_TIME_RANGES:
        return value
    if not isinstance(value, str):
        raise CardValidationError("time_range 必须是文本")
    match = CUSTOM_TIME_RANGE_RE.fullmatch(value.strip())
    if not match:
        choices = "、".join(sorted(ALLOWED_TIME_RANGES))
        raise CardValidationError(
            f"time_range 只能是：{choices}，或 custom:开始日期:结束日期"
        )
    try:
        start = date.fromisoformat(match.group(1))
        end = date.fromisoformat(match.group(2))
    except ValueError as exc:
        raise CardValidationError("自定义语料日期无效") from exc
    if start > end:
        raise CardValidationError("自定义语料开始日期不能晚于结束日期")
    return f"custom:{start.isoformat()}:{end.isoformat()}"


def _clean_string_tuple(
    value: Iterable[Any] | None,
    *,
    field: str,
    allowed: frozenset[str] | None = None,
    maximum: int | None = None,
) -> tuple[str, ...]:
    if value is None:
        values: tuple[str, ...] = ()
    elif isinstance(value, str):
        raise CardValidationError(f"{field} 必须是列表，不能是单个字符串")
    else:
        try:
            values = tuple(value)
        except TypeError as exc:
            raise CardValidationError(f"{field} 必须是列表") from exc
    if not all(isinstance(item, str) and item for item in values):
        raise CardValidationError(f"{field} 只能包含非空字符串")
    if len(values) != len(set(values)):
        raise CardValidationError(f"{field} 不能包含重复项")
    if maximum is not None and len(values) > maximum:
        raise CardValidationError(f"{field} 最多允许 {maximum} 项")
    if allowed is not None:
        invalid = set(values) - allowed
        if invalid:
            raise CardValidationError(f"{field} 包含不支持的值：{sorted(invalid)}")
    return values


@dataclass(frozen=True, slots=True)
class CardSpec:
    """一张卡片的持久化定义与首页布局。"""

    card_id: str
    name: str
    purpose: str = ""
    item_limit: int = 3
    card_type: str = "custom"
    sources: tuple[str, ...] = ("transcripts",)
    time_range: str = "all"
    rules: str = ""
    user_prompt: str = ""
    output_type: str = "text"
    trigger_mode: str = "manual"
    dependencies: tuple[str, ...] = ()
    width: str = "standard"
    height: int = DEFAULT_CARD_HEIGHT
    position: int = 0
    visible: bool = True
    enabled: bool = True
    is_default: bool = False
    rules_version: int = 1
    created_at: str = ""
    updated_at: str = ""
    deleted_at: str | None = None

    @classmethod
    def new_custom(
        cls,
        name: str,
        *,
        purpose: str = "",
        item_limit: int = 3,
        sources: Iterable[str] = ("transcripts",),
        time_range: str = "all",
        rules: str = "",
        user_prompt: str = "",
        output_type: str = "text",
        trigger_mode: str = "manual",
        dependencies: Iterable[str] = (),
        width: str = "standard",
        height: int = DEFAULT_CARD_HEIGHT,
        position: int = 0,
        visible: bool = True,
        enabled: bool = True,
        card_id: str | None = None,
    ) -> "CardSpec":
        now = utc_now_iso()
        return cls(
            card_id=card_id or new_card_id(),
            name=name,
            purpose=purpose,
            item_limit=item_limit,
            card_type="custom",
            sources=tuple(sources),
            time_range=time_range,
            rules=rules,
            user_prompt=user_prompt,
            output_type=output_type,
            trigger_mode=trigger_mode,
            dependencies=tuple(dependencies),
            width=width,
            height=height,
            position=position,
            visible=visible,
            enabled=enabled,
            is_default=False,
            rules_version=1,
            created_at=now,
            updated_at=now,
        ).validated()

    def validated(self) -> "CardSpec":
        card_id = _clean_text(
            self.card_id, field="card_id", limit=64, required=True
        ).lower()
        if not CARD_ID_RE.fullmatch(card_id):
            raise CardValidationError(
                "card_id 只能使用小写字母、数字、下划线和连字符"
            )
        name = _clean_text(
            self.name, field="卡片名称", limit=MAX_CARD_NAME_CHARS, required=True
        )
        purpose = _clean_text(
            self.purpose, field="卡片目的", limit=MAX_PURPOSE_CHARS
        )
        if not isinstance(self.item_limit, int) or not 1 <= self.item_limit <= 20:
            raise CardValidationError("item_limit 必须在 1 到 20 之间")
        card_type = _clean_enum(
            self.card_type, field="card_type", allowed=ALLOWED_CARD_TYPES
        )
        sources = _clean_string_tuple(
            self.sources, field="sources", allowed=ALLOWED_SOURCES
        )
        if not sources:
            raise CardValidationError("卡片至少需要一个数据来源")
        time_range = clean_time_range(self.time_range)
        rules = _clean_text(self.rules, field="生成规则", limit=MAX_RULE_CHARS)
        user_prompt = _clean_text(
            self.user_prompt, field="用户 Prompt", limit=MAX_PROMPT_CHARS
        )
        output_type = _clean_enum(
            self.output_type, field="output_type", allowed=ALLOWED_OUTPUT_TYPES
        )
        trigger_mode = _clean_enum(
            self.trigger_mode,
            field="trigger_mode",
            allowed=ALLOWED_TRIGGER_MODES,
        )
        dependencies = _clean_string_tuple(
            self.dependencies,
            field="dependencies",
            maximum=MAX_DEPENDENCIES,
        )
        for dependency in dependencies:
            if not CARD_ID_RE.fullmatch(dependency):
                raise CardValidationError(f"无效的依赖卡片 ID：{dependency}")
        if dependencies and "confirmed_cards" not in sources:
            raise CardValidationError(
                "引用其他卡片时，sources 必须包含 confirmed_cards"
            )
        if "confirmed_cards" in sources and not dependencies:
            raise CardValidationError(
                "sources 包含 confirmed_cards 时必须明确指定依赖卡片"
            )
        width = _clean_enum(self.width, field="width", allowed=ALLOWED_WIDTHS)
        if (
            not isinstance(self.height, int)
            or not MIN_CARD_HEIGHT <= self.height <= MAX_CARD_HEIGHT
        ):
            raise CardValidationError(
                f"height 必须在 {MIN_CARD_HEIGHT} 到 {MAX_CARD_HEIGHT} 之间"
            )
        if not isinstance(self.position, int) or self.position < 0:
            raise CardValidationError("position 必须是非负整数")
        if type(self.visible) is not bool or type(self.enabled) is not bool:
            raise CardValidationError("visible 和 enabled 必须是布尔值")
        if type(self.is_default) is not bool:
            raise CardValidationError("is_default 必须是布尔值")
        if self.is_default != (card_type == "default"):
            raise CardValidationError("is_default 与 card_type 不一致")
        if not isinstance(self.rules_version, int) or self.rules_version < 1:
            raise CardValidationError("rules_version 必须是正整数")
        if self.deleted_at is not None and not isinstance(self.deleted_at, str):
            raise CardValidationError("deleted_at 必须是时间文本或 None")
        return replace(
            self,
            card_id=card_id,
            name=name,
            purpose=purpose,
            card_type=card_type,
            sources=sources,
            time_range=time_range,
            rules=rules,
            user_prompt=user_prompt,
            output_type=output_type,
            trigger_mode=trigger_mode,
            dependencies=dependencies,
            width=width,
            height=self.height,
        )

    def with_updates(self, **changes: Any) -> "CardSpec":
        allowed = {
            "name",
            "purpose",
            "item_limit",
            "sources",
            "time_range",
            "rules",
            "user_prompt",
            "output_type",
            "trigger_mode",
            "dependencies",
            "width",
            "height",
            "position",
            "visible",
            "enabled",
            "rules_version",
            "updated_at",
            "deleted_at",
        }
        invalid = set(changes) - allowed
        if invalid:
            raise CardValidationError(f"不允许修改字段：{sorted(invalid)}")
        if "sources" in changes:
            changes["sources"] = tuple(changes["sources"])
        if "dependencies" in changes:
            changes["dependencies"] = tuple(changes["dependencies"])
        return replace(self, **changes).validated()

    def generation_snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "purpose": self.purpose,
            "item_limit": self.item_limit,
            "sources": list(self.sources),
            "time_range": self.time_range,
            "rules": self.rules,
            "user_prompt": self.user_prompt,
            "output_type": self.output_type,
            "trigger_mode": self.trigger_mode,
            "dependencies": list(self.dependencies),
        }

    def to_record(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "name": self.name,
            "purpose": self.purpose,
            "item_limit": self.item_limit,
            "card_type": self.card_type,
            "sources_json": json.dumps(list(self.sources), ensure_ascii=False),
            "time_range": self.time_range,
            "rules": self.rules,
            "user_prompt": self.user_prompt,
            "output_type": self.output_type,
            "trigger_mode": self.trigger_mode,
            "dependencies_json": json.dumps(
                list(self.dependencies), ensure_ascii=False
            ),
            "width": self.width,
            "height": self.height,
            "position": self.position,
            "visible": int(self.visible),
            "enabled": int(self.enabled),
            "is_default": int(self.is_default),
            "rules_version": self.rules_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "deleted_at": self.deleted_at,
        }

    @classmethod
    def from_record(cls, row: Mapping[str, Any]) -> "CardSpec":
        return cls(
            card_id=row["card_id"],
            name=row["name"],
            purpose=row["purpose"],
            item_limit=int(row["item_limit"]),
            card_type=row["card_type"],
            sources=tuple(json.loads(row["sources_json"])),
            time_range=row["time_range"],
            rules=row["rules"],
            user_prompt=row["user_prompt"],
            output_type=row["output_type"],
            trigger_mode=row["trigger_mode"],
            dependencies=tuple(json.loads(row["dependencies_json"])),
            width=row["width"],
            height=int(row["height"]) if "height" in row.keys() else DEFAULT_CARD_HEIGHT,
            position=int(row["position"]),
            visible=bool(row["visible"]),
            enabled=bool(row["enabled"]),
            is_default=bool(row["is_default"]),
            rules_version=int(row["rules_version"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            deleted_at=row["deleted_at"],
        ).validated()


@dataclass(frozen=True, slots=True)
class ContentRevision:
    revision_id: str
    card_id: str
    parent_revision_id: str | None
    content: str
    kind: str
    source_hash: str
    accepted: bool
    created_at: str
    is_current: bool = False


@dataclass(frozen=True, slots=True)
class PreferenceRule:
    preference_id: str
    card_id: str | None
    scope: str
    rule_text: str
    source_revision_id: str | None
    active: bool
    created_at: str
    revoked_at: str | None = None


@dataclass(frozen=True, slots=True)
class CardRun:
    run_id: str
    card_id: str
    rules_version: int
    source_hash: str
    idempotency_key: str
    status: str
    revision_id: str | None
    error_code: str | None
    created_at: str
    finished_at: str | None


@dataclass(frozen=True, slots=True)
class SourceRef:
    ref_id: str
    source_type: str
    source_path: str
    content: str
    content_hash: str
    started_at: str | None
    fact_level: str
    metadata: Mapping[str, Any]
    score: float = 0.0
