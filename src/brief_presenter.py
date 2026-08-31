"""把简报 Markdown 转成适合桌面端直接阅读的纯文本。

Markdown 文件仍然作为本地可移植的真实数据保存；桌面端只负责展示经过
排版的阅读版本，避免把 ``#``、``**``、链接等语法直接暴露给普通用户。
"""

from __future__ import annotations

import re


_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]+\)")
_HTML_RE = re.compile(r"<[^>]+>")
_ORDERED_PREFIX_RE = re.compile(r"^\s*\d+[.)、]\s*")


def _plain(text: str) -> str:
    value = str(text or "").strip()
    value = _IMAGE_RE.sub(lambda match: match.group(1), value)
    value = _LINK_RE.sub(lambda match: match.group(1), value)
    value = _HTML_RE.sub("", value)
    value = re.sub(r"(\*\*|__)(.+?)\1", r"\2", value)
    value = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", value)
    value = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"\1", value)
    value = value.replace("`", "")
    value = value.replace("\\#", "#")
    return re.sub(r"[ \t]+", " ", value).strip()


def render_yesterday_review(markdown: str) -> str:
    """渲染已经由复盘链路生成的昨日 review，不接受原始转写语料。"""

    output: list[str] = []
    for raw in str(markdown or "").splitlines():
        stripped = raw.strip()
        if not stripped or stripped in {"---", "***", "___"}:
            continue
        if stripped.startswith("# "):
            title = _plain(stripped[2:])
            if title:
                output.extend([title, ""])
            continue
        if stripped.startswith("## "):
            title = _plain(stripped[3:])
            if title:
                if output and output[-1] != "":
                    output.append("")
                output.append(f"— {title} —")
            continue
        if stripped.startswith("### "):
            title = _plain(stripped[4:])
            if title:
                output.append(f"  {title}")
            continue
        if stripped.startswith(">"):
            text = _plain(stripped.lstrip("> "))
            if text:
                output.append(f"  {text}")
            continue
        if stripped.startswith(("- ", "* ", "+ ")):
            text = _plain(stripped[2:])
            if text:
                output.append(f"  · {text}")
            continue
        ordered = _ORDERED_PREFIX_RE.sub("", stripped)
        text = _plain(ordered)
        if text:
            output.append(f"  {text}")
    return "\n".join(output).strip()


def render_card_content(content: str) -> str:
    """把任意文字生成卡片统一适配为“声年”阅读样式。

    这只是展示层转换，不改写数据库中的原始生成结果。
    """

    output: list[str] = []
    in_code_block = False
    for raw in str(content or "").splitlines():
        stripped = raw.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if not stripped:
            if output and output[-1] != "":
                output.append("")
            continue
        if stripped in {"---", "***", "___"}:
            continue
        heading = re.match(r"^#{1,6}\s+(.+)$", stripped)
        if heading:
            title = _plain(heading.group(1))
            if title:
                if output and output[-1] != "":
                    output.append("")
                output.append(f"— {title} —")
            continue
        if stripped.startswith(">"):
            text = _plain(stripped.lstrip("> "))
            if text:
                output.append(f"  {text}")
            continue
        if stripped.startswith(("- ", "* ", "+ ")):
            text = _plain(stripped[2:])
            if text:
                output.append(f"  · {text}")
            continue
        ordered_match = re.match(r"^\s*(\d+)[.)、]\s*(.+)$", stripped)
        if ordered_match:
            text = _plain(ordered_match.group(2))
            if text:
                output.append(f"  {ordered_match.group(1)}. {text}")
            continue
        text = _plain(stripped)
        if text:
            output.append(("    " if in_code_block else "") + text)
    return "\n".join(output).strip()


def render_today_brief(markdown: str) -> str:
    """按经典视图的信息层级渲染今日简报。"""

    output: list[str] = []
    section: str | None = None
    for raw in str(markdown or "").splitlines():
        stripped = raw.strip()
        if not stripped or stripped in {"---", "***", "___"}:
            continue
        if stripped.startswith("# "):
            title = _plain(stripped[2:])
            if title:
                output.extend([title, ""])
            continue
        if stripped.startswith(("> 状态：", "> 状态:")):
            state = _plain(re.sub(r"^>\s*状态[：:]\s*", "", stripped))
            if state:
                output.extend([f"状态：{state}", ""])
            continue
        if stripped == "## 状态" or "## 状态" in stripped:
            section = "status"
            continue
        if "今日重大提醒" in stripped or "今日提醒" in stripped:
            section = "remind"
            output.extend(["", "— 今日重大提醒 —"])
            continue
        if "今日主要在做什么" in stripped or "今日主要做什么" in stripped:
            section = "doing"
            output.extend(["", "— 今日主要在做什么 —"])
            continue
        if "时间分配" in stripped or "时间汇总" in stripped:
            section = "time"
            output.extend(["", "— 时间分配 —"])
            continue
        if "最重要的项目" in stripped or "重要的项目" in stripped:
            section = "project"
            output.extend(["", "— 重要项目 —"])
            continue
        if stripped.startswith("## "):
            section = None
            continue

        text = _plain(stripped.lstrip("> "))
        if section == "status":
            if text:
                output.append(f"状态：{text}")
        elif section == "remind":
            if stripped.startswith(">"):
                continue
            text = _plain(_ORDERED_PREFIX_RE.sub("", stripped))
            text = text.lstrip("-*+· ").strip()
            if text:
                output.append(f"  · {text}")
        elif section == "doing":
            if stripped.startswith(">"):
                continue
            text = text.lstrip("-*+· ").strip()
            if text:
                output.append(f"  · {text}")
        elif section == "time":
            text = text.lstrip("-*+· ").strip()
            if text:
                prefix = "  " if stripped.startswith(">") else "  · "
                output.append(f"{prefix}{text}")
        elif section == "project":
            text = text.lstrip("-*+· ").strip()
            if text:
                output.append(f"  ◆ {text[:120]}")
    return "\n".join(output).strip()
