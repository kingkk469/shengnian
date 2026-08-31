from __future__ import annotations

import sys
from pathlib import Path


SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from brief_presenter import (  # noqa: E402
    render_card_content,
    render_today_brief,
    render_yesterday_review,
)


def test_today_brief_is_readable_without_markdown_syntax():
    source = """# 每日简报 2026-07-24

## 状态
> **专注推进**

## 今日重大提醒
1. **确认**收费方案

## 今日主要在做什么
- 完成[卡片工作台](https://example.com)优化

## 时间分配
- **开发**：2 小时

## 最重要的项目
- **声年**：修复简报链路
"""
    rendered = render_today_brief(source)
    assert "每日简报 2026-07-24" in rendered
    assert "状态：专注推进" in rendered
    assert "· 确认收费方案" in rendered
    assert "◆ 声年：修复简报链路" in rendered
    assert "**" not in rendered
    assert "](" not in rendered
    assert "##" not in rendered


def test_yesterday_review_is_readable_without_markdown_syntax():
    source = """# 昨日复盘 2026-07-23

## 昨天做了什么
- **完成**卡片工作台
- 查看[说明](https://example.com)

## 遗留事项
> 明天继续验证
"""
    rendered = render_yesterday_review(source)
    assert "昨日复盘 2026-07-23" in rendered
    assert "— 昨天做了什么 —" in rendered
    assert "· 完成卡片工作台" in rendered
    assert "· 查看说明" in rendered
    assert "明天继续验证" in rendered
    assert "**" not in rendered
    assert "](" not in rendered
    assert "##" not in rendered


def test_custom_prompt_and_skill_outputs_share_shengnian_display_style():
    source = """# **选题建议**

1. [第一个观点](https://example.com)
2. 第二个观点

> 使用前请核实
"""
    rendered = render_card_content(source)
    assert "— 选题建议 —" in rendered
    assert "1. 第一个观点" in rendered
    assert "2. 第二个观点" in rendered
    assert "使用前请核实" in rendered
    assert "**" not in rendered
    assert "](" not in rendered
    assert "#" not in rendered
