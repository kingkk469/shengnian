"""声年内置卡片注册表。"""
from __future__ import annotations

from dataclasses import replace

from .models import CardNotFoundError, CardSpec


def _default_card(
    card_id: str,
    name: str,
    *,
    purpose: str,
    item_limit: int,
    sources: tuple[str, ...],
    time_range: str,
    rules: str,
    output_type: str = "text",
    trigger_mode: str = "manual",
    width: str = "standard",
    height: int = 320,
    position: int,
) -> CardSpec:
    return CardSpec(
        card_id=card_id,
        name=name,
        purpose=purpose,
        item_limit=item_limit,
        card_type="default",
        sources=sources,
        time_range=time_range,
        rules=rules,
        user_prompt="",
        output_type=output_type,
        trigger_mode=trigger_mode,
        dependencies=(),
        width=width,
        height=height,
        position=position,
        visible=True,
        enabled=True,
        is_default=True,
        rules_version=1,
    ).validated()


DEFAULT_CARDS: tuple[CardSpec, ...] = (
    _default_card(
        "today_brief",
        "今日简报",
        purpose="快速看清今天发生了什么、做了什么决定、下一步是什么。",
        item_limit=10,
        sources=("transcripts",),
        time_range="today",
        rules="整理今天值得记录的进展、决定、事实和下一步；不把转述当作本人观点。",
        trigger_mode="manual",
        position=0,
    ),
    _default_card(
        "yesterday_brief",
        "昨日简报",
        purpose="回顾昨天的关键进展与遗留事项。",
        item_limit=10,
        sources=("transcripts",),
        time_range="yesterday",
        rules="回顾昨天值得记录的进展、决定、事实和遗留事项。",
        trigger_mode="manual",
        position=1,
    ),
    _default_card(
        "todos",
        "待办",
        purpose="集中展示本人已经确认但尚未完成的行动。",
        item_limit=20,
        sources=("transcripts",),
        time_range="today",
        rules="只收录本人明确承诺、尚未完成且可以执行的事项。",
        output_type="structured_todos",
        trigger_mode="manual",
        width="standard",
        position=2,
    ),
    _default_card(
        "done",
        "已办",
        purpose="回看最近已经确认完成的事情。",
        item_limit=20,
        sources=("done",),
        time_range="last_7_days",
        rules="展示已经由用户确认完成的事项，不推测完成状态。",
        output_type="structured_done",
        width="standard",
        position=3,
    ),
    _default_card(
        "short_video",
        "短视频",
        purpose="从本人原始语料中找到值得继续发展的短视频选题。",
        item_limit=3,
        sources=("transcripts",),
        time_range="last_7_days",
        rules="先给出约三个有原始语料依据的选题；用户选择后再生成完整口播稿。",
        output_type="short_video",
        width="full",
        height=420,
        position=4,
    ),
)

DEFAULT_CARD_IDS = frozenset(card.card_id for card in DEFAULT_CARDS)


class CardRegistry:
    """提供不可变的内置定义，避免 UI 复制产品规则。"""

    def list_defaults(self) -> tuple[CardSpec, ...]:
        return tuple(replace(card) for card in DEFAULT_CARDS)

    def get_default(self, card_id: str) -> CardSpec:
        for card in DEFAULT_CARDS:
            if card.card_id == card_id:
                return replace(card)
        raise CardNotFoundError(f"不是内置卡片：{card_id}")

    def is_default(self, card_id: str) -> bool:
        return card_id in DEFAULT_CARD_IDS
