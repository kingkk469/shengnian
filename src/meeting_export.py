"""会议纪要导出：从多个 segment → 云端 AI 总结 → 本地 + Obsidian MD。

原始音频始终留在本机。用户确认生成会议纪要后，所选 segment 的转写文字会
通过统一 AI 网关发送给配置的模型服务处理；生成结果再写入本地文件。

    segments (jsonl 行的列表)
    └─> _build_transcript        → 拼成 LLM 可读的转写文本
    └─> _call_llm_for_meeting    → DeepSeek 生成 JSON
                                    {summary, decisions, action_items,
                                     chapters, open_questions, external_inputs}
    └─> render_meeting_md        → 拼装 MD（含本地逐字稿）
    └─> 写本地 meetings/<id>.md
    └─> 同步 Obsidian 第二大脑/会议纪要/<id>.md

独立可跑：
    python -m meeting_export segments.json --title "周会复盘"
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (
    CONFIG,
    ROOT,
    configured_obsidian_vault,
    configured_owner_name,
    setup_logger,
)

log = setup_logger("meeting-export")


# ============================================================
# 配置 + 路径
# ============================================================
def _cfg() -> dict:
    return CONFIG.get("meeting", {}) or {}

def _local_dir() -> Path:
    p = ROOT / _cfg().get("local_dir", "meetings")
    p.mkdir(parents=True, exist_ok=True)
    return p

def _obsidian_dir() -> Path | None:
    vault = configured_obsidian_vault()
    if not vault:
        return None
    sub = _cfg().get("obsidian_subdir", "会议纪要")
    p = vault / "第二大脑" / sub
    p.mkdir(parents=True, exist_ok=True)
    return p


# ============================================================
# Progress callback
# ============================================================
ProgressCB = Callable[[str, str], None]
NOOP_PROGRESS: ProgressCB = lambda stage, msg: None


# ============================================================
# AI 总结 Prompt（强调本人 / 他人发言 / 转述 严格区分）
# ============================================================
MEETING_PROMPT = """你是会议纪要助手。下面是一次会议的**完整本地转写**（已带时间戳和声纹归因标签）。下文历史字段中的 king 均指当前用户本人，最终正文请写“本人”或配置的姓名，不要输出名字 king。
请输出一份结构化的会议纪要，**严格遵循 king 团队已有的格式约定**。

【⚠️ 极度重要：本人/他人/转述 严格区分】

方括号里的声纹归因标签是唯一可信身份来源，必须原样服从，禁止根据第一人称、
内容主题、会议标题、上下文或常识猜测说话人：

1. `[本人·king]`：声纹明确识别为 king，只有这种段落能提炼为 king 本人观点、
   king 的决策、king 的承诺或 king 的行动项。
2. `[他人·姓名]`：已识别为其他人，只能归因给该人，绝不能写成 king 的观点或行动。
3. `[待确认说话人]`：声纹未确认。即使原文说“我”“我的公司”“我决定”，也绝不能
   推断为 king；只能写成“待确认说话人提出……”，负责人必须写“待确认”。
4. `[外部·来源]`：外部输入或转述，只能进入 external_inputs。

如果整份转写没有任何 `[本人·king]`，则 `king_viewpoints` 必须为空，且不得生成
任何负责人为 king 的决策或行动项。

【输出严格 JSON】（仿照 king 团队的周会/直播复盘格式）

{
  "summary": "用 80-150 字概括这次会议讨论了什么、达成了什么。短小精悍，标主语，区分谁说的什么。",
  "topics": [
    {
      "title": "议题名（10-25 字）",
      "bullets": [
        "要点 1（必须带真实主语：张老师/李同学/待确认说话人/king...）",
        "要点 2（不得省略主语）",
        "要点 3（不得省略主语）"
      ]
    }
  ],
  "decisions": ["明确达成的决策（一句话）。只有提议没拍板的不算。"],
  "king_viewpoints": [
    {"text": "king 本人明确说出的观点", "evidence_start": "对应 [本人·king] 段落的 HH:MM:SS"}
  ],
  "action_items": [
    {
      "who": "谁负责（king/张老师/李同学/待确认）",
      "what": "做什么",
      "deadline": "如果原话提到时间就填，否则留空",
      "evidence_start": "本人明确承诺该行动的段落 HH:MM:SS；没有明确承诺则不要生成"
    }
  ],
  "open_questions": ["会上提出但没有结论的问题，0-5 条，可省略"],
  "external_inputs": ["会上有人引用的外部信息（视频/博主/听说），可省略"]
}

格式参考你团队既有周会纪要的结构：
- summary 短，对应"## 会议概要"
- topics 是核心，对应"## 主要议题"，每个 topic 是粗体标题 + bullet 列表
- 议题数量：周会 5-8 个 / 直播复盘 3-6 个 / 短会议 2-4 个
- 每个 bullet 一句话，控制在 30-50 字

【最关键的检查】产生 action_items 之前再确认：
- 每条的 who 是不是真有人承诺要做？
- 还是只是有人说"我们应该 X"但没人接手？后者不算。
- 转述里出现的"应该做 X" / 博主建议 → 绝对不进 action_items，进 external_inputs
- who=king 时，evidence_start 对应的原文必须明确标为 `[本人·king]`；否则删除该项
- `[待确认说话人]` 的第一人称承诺也只能标为“待确认”，绝不能写成 king

人名直接用真名（张老师、李同学、王老板），不要用"某A/某B"。全程中文。
"""


def _call_llm_for_meeting(transcript: str,
                          on_progress: ProgressCB = NOOP_PROGRESS) -> dict:
    """调 DeepSeek 生成会议纪要 JSON。复用 daily_summary 的 provider 配置。"""
    cfg = CONFIG["summary"]
    provider = cfg.get("provider", "deepseek")
    on_progress("ai", f"调用 {provider} 分析会议内容...")

    if provider in ("deepseek", "openai"):
        from ai_gateway import OpenAI, provider_api_key
        if provider == "deepseek":
            api_key = provider_api_key("DEEPSEEK_API_KEY")
            if not api_key:
                raise RuntimeError(
                    "缺少环境变量 DEEPSEEK_API_KEY，请运行：\n"
                    '    setx DEEPSEEK_API_KEY "sk-xxx"\n'
                    "然后重启 launcher。"
                )
            client = OpenAI(api_key=api_key, base_url=cfg["base_url_deepseek"])
            model = cfg["model_deepseek"]
        else:
            client = OpenAI()
            model = cfg["model_openai"]
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": MEETING_PROMPT},
                {"role": "user", "content": f"会议转写：\n\n{transcript}"},
            ],
            response_format={"type": "json_object"},
            max_tokens=4000,
            temperature=0.3,
        )
        raw = resp.choices[0].message.content or "{}"
    else:
        import anthropic
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=cfg["model_anthropic"],
            max_tokens=4000,
            system=MEETING_PROMPT,
            messages=[{"role": "user", "content": f"会议转写：\n\n{transcript}"}],
        )
        raw = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log.error("LLM 返回非 JSON: %s", raw[:300])
        raise RuntimeError(f"LLM 返回的不是有效 JSON: {e}")


# ============================================================
# 把 segments 拼成 LLM 友好的转写文本
# ============================================================
def _build_transcript(segments: list[dict]) -> str:
    """生成带强制声纹归因标签的转写，禁止模型根据内容猜身份。"""
    lines = []
    for seg in segments:
        ts = (seg.get("start") or "")[11:19]
        spk = str(seg.get("speaker_name") or "").strip()
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        if spk.casefold() in {configured_owner_name().casefold(), "king", "我"}:
            attribution = f"[本人·{configured_owner_name()}]"
        elif spk and spk not in ("?", "未知"):
            attribution = f"[他人·{spk}]"
        else:
            attribution = "[待确认说话人]"
        lines.append(f"[{ts}] {attribution} · {text}")
    return "\n".join(lines)


def _evidence_speaker(segments: list[dict], evidence_start: str) -> str:
    """根据行动/观点提供的时间戳反查声纹身份。"""
    evidence = str(evidence_start or "").strip()
    if not evidence:
        return ""
    hhmmss = evidence[-8:]
    for seg in segments:
        start = str(seg.get("start") or "")
        if start[11:19] == hhmmss:
            return str(seg.get("speaker_name") or "").strip()
    return ""


def enforce_king_attribution(notes: dict, segments: list[dict]) -> dict:
    """只保留能由 King 声纹时间戳直接佐证的本人观点和行动项。

    这是模型提示词之外的程序级门禁：即使模型把未知说话人的第一人称误判为
    King，也不会进入最终纪要中的 King 本人观点或行动项。
    """
    cleaned = dict(notes or {})

    verified_viewpoints = []
    for item in cleaned.get("king_viewpoints") or []:
        if not isinstance(item, dict):
            continue
        if _evidence_speaker(segments, item.get("evidence_start", "")).casefold() in {
            configured_owner_name().casefold(), "king", "我"
        }:
            verified_viewpoints.append(item)
    cleaned["king_viewpoints"] = verified_viewpoints

    verified_actions = []
    for item in cleaned.get("action_items") or []:
        if not isinstance(item, dict):
            continue
        who = str(item.get("who") or "").strip()
        if who.casefold() in {configured_owner_name().casefold(), "king", "本人", "我"}:
            evidence_speaker = _evidence_speaker(
                segments, item.get("evidence_start", "")
            )
            if evidence_speaker.casefold() not in {
                configured_owner_name().casefold(), "king", "我"
            }:
                log.warning("丢弃无 King 声纹佐证的本人行动项: %s", item)
                continue
            item = dict(item)
            item["who"] = configured_owner_name()
        verified_actions.append(item)
    cleaned["action_items"] = verified_actions
    return cleaned


# ============================================================
# 渲染 MD
# ============================================================
def _fmt_duration(sec: float) -> str:
    m = int(sec // 60)
    h = m // 60
    m = m % 60
    if h > 0:
        return f"{h}h{m}min"
    return f"{m}min"


# ============================================================
# 分类 → 目录 / tags / category 的映射（跟既有 vault 格式对齐）
# ============================================================
# 用户在 dialog 里选分类，对应不同的存储目录和 frontmatter
CATEGORIES = {
    "周会记录": {
        "dir": "周会记录",
        "tags": ["周会", "会议纪要"],
        "category": "周会",
        "title_prefix": "周会",
    },
    "直播复盘": {
        "dir": "直播复盘",
        "tags": ["直播复盘", "会议纪要"],
        "category": "直播复盘",
        "title_prefix": "直播复盘",
    },
    "AI课项目": {
        "dir": "AI课项目",
        "tags": ["AI课项目", "会议纪要"],
        "category": "AI课项目",
        "title_prefix": "项目",
    },
    "客户拜访": {
        "dir": "",   # 客户拜访没有专属目录，落到 vault 根
        "tags": ["客户拜访", "会议纪要"],
        "category": "客户拜访",
        "title_prefix": "客户拜访",
    },
    "通用会议": {
        "dir": "第二大脑/会议纪要",
        "tags": ["会议纪要"],
        "category": "会议纪要",
        "title_prefix": "会议",
    },
}


def render_meeting_md(
    title: str,
    segments: list[dict],
    notes: dict,
    category: str = "通用会议",
    source_label: str = "",
) -> str:
    """拼装会议纪要 Markdown。

    notes: LLM 输出，schema = {summary, topics, decisions, action_items,
                              open_questions, external_inputs}
    category: CATEGORIES 里的 key，决定 frontmatter
    source_label: 来源标识（如音频文件名），可空
    """
    cat = CATEGORIES.get(category, CATEGORIES["通用会议"])

    # 元信息
    starts = sorted([s.get("start", "") for s in segments if s.get("start")])
    first = starts[0] if starts else ""
    last = starts[-1] if starts else ""
    duration_sec = sum(s.get("duration_sec", 0) for s in segments)
    speakers = sorted({s.get("speaker_name") or "未知"
                       for s in segments if s.get("speaker_name")})
    date_iso = first[:10] if first else dt.date.today().isoformat()

    # frontmatter（兼容现有周会/直播复盘格式）
    fm_tags = "\n".join(f"  - {t}" for t in cat["tags"])
    src = source_label or f"voice-journal · {len(segments)} 段 · {_fmt_duration(duration_sec)}"
    fm = [
        "---",
        f"date: {date_iso}",
        "ai_generated: true",
        "ai_service_provider: 声年",
        f"content_id: shengnian-meeting-{hashlib.sha256((title + '|' + first + '|' + last).encode('utf-8')).hexdigest()[:20]}",
        "tags:",
        fm_tags,
        f"category: {cat['category']}",
        f"source: {src}",
        "---",
        "",
    ]

    head = [
        f"# {title}",
        "",
        "> AI 辅助整理，使用或发布前请核实；逐字稿来自本机转写。",
        "",
    ]

    parts: list[str] = []

    # 摘要 → ## 会议概要
    summary = (notes.get("summary") or "").strip()
    parts += ["## 会议概要", "", summary or "_（无）_", ""]

    # 主要议题（核心内容）
    topics = notes.get("topics") or []
    parts += ["## 主要议题", ""]
    if topics:
        for i, t in enumerate(topics, 1):
            title_t = t.get("title", "").strip()
            bullets = t.get("bullets") or []
            parts.append(f"**{i}. {title_t}**")
            for b in bullets:
                parts.append(f"- {b}")
            parts.append("")
    else:
        parts += ["_（无议题）_", ""]

    # 决策
    decisions = notes.get("decisions") or []
    if decisions:
        parts += ["## 关键决策", ""]
        parts += [f"- {d}" for d in decisions]
        parts.append("")

    # King 本人观点：必须经过声纹时间戳程序校验
    viewpoints = notes.get("king_viewpoints") or []
    if viewpoints:
        parts += ["## 本人观点（声纹确认）", ""]
        for item in viewpoints:
            text = str(item.get("text") or "").strip()
            evidence = str(item.get("evidence_start") or "").strip()
            if text:
                suffix = f"  _（{evidence}）_" if evidence else ""
                parts.append(f"- {text}{suffix}")
        parts.append("")

    # 行动项
    actions = notes.get("action_items") or []
    if actions:
        parts += ["## 行动项", ""]
        for a in actions:
            who = a.get("who", "").strip()
            what = a.get("what", "").strip()
            dl = (a.get("deadline") or "").strip()
            line = f"- **{who}** · {what}" if who else f"- {what}"
            if dl:
                line += f"  _（{dl}）_"
            parts.append(line)
        parts.append("")

    # 开放问题
    questions = notes.get("open_questions") or []
    if questions:
        parts += ["## 待解决的问题", ""]
        parts += [f"- {q}" for q in questions]
        parts.append("")

    # 外部引用
    externals = notes.get("external_inputs") or []
    if externals:
        parts += ["## 会上提到的外部信息（转述）", ""]
        parts += [f"- {e}" for e in externals]
        parts.append("")

    # 时段信息（短小，写在末尾）
    parts += ["---", ""]
    parts += [
        f"> {len(segments)} 段 · {_fmt_duration(duration_sec)} · "
        f"说话人：{', '.join(speakers) or '未识别'}",
        f"> 起：{first}    止：{last}",
        "",
    ]

    # 逐字稿（折叠在最后，方便查证）
    parts += ["## 逐字稿（本地转写）", ""]
    for seg in segments:
        ts = (seg.get("start") or "")[11:19]
        spk = seg.get("speaker_name") or ""
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        prefix = f"`{ts}`"
        if spk and spk not in ("?", "未知"):
            prefix += f" **{spk}**"
        parts.append(f"- {prefix} · {text}")

    return "\n".join(fm + head + parts) + "\n"


# ============================================================
# 主入口
# ============================================================
def _safe_filename(s: str) -> str:
    """去掉 Windows 路径非法字符。"""
    return re.sub(r'[\\/:*?"<>|]', "_", s).strip()[:60] or "meeting"


def _build_meeting_id(segments: list[dict], title: str) -> str:
    """生成文件名前缀：YYYY-MM-DD-HHMM-标题。"""
    starts = sorted([s.get("start", "") for s in segments if s.get("start")])
    if not starts:
        ts = dt.datetime.now().strftime("%Y-%m-%d-%H%M")
    else:
        first = starts[0]
        ts = first[:10] + "-" + first[11:13] + first[14:16]
    return f"{ts}-{_safe_filename(title)}"


def _meeting_filename(segments: list[dict], category: str, title: str) -> str:
    """文件名格式仿照 vault 既有命名：
       YYYY-MM-DD-{category 前缀}-{标题}.md
       例：2026-05-26-周会-本周营收复盘.md
    """
    starts = sorted([s.get("start", "") for s in segments if s.get("start")])
    date_iso = starts[0][:10] if starts else dt.date.today().isoformat()
    cat = CATEGORIES.get(category, CATEGORIES["通用会议"])
    prefix = cat["title_prefix"]
    clean = _safe_filename(title)
    # 如果标题已经包含 prefix，避免重复
    if prefix and clean.startswith(prefix):
        return f"{date_iso}-{clean}.md"
    return f"{date_iso}-{prefix}-{clean}.md"


def _resolve_obsidian_target(category: str) -> Path | None:
    """根据分类返回 vault 内的最终保存目录。"""
    vault = configured_obsidian_vault()
    if not vault:
        return None
    cat = CATEGORIES.get(category, CATEGORIES["通用会议"])
    sub = cat["dir"]
    if not sub:
        target = vault
    else:
        target = vault / sub
    target.mkdir(parents=True, exist_ok=True)
    return target


def export_meeting(
    segments: list[dict],
    title: str | None = None,
    category: str = "通用会议",
    on_progress: ProgressCB = NOOP_PROGRESS,
) -> Path:
    """主入口：从 segments 一站式生成会议纪要。

    原始音频不上传；所选 segment 的转写文字会发送给配置的云端 AI。

    segments: jsonl 行的字典列表，要求每条至少有 'start' 'text' 字段
    title: 会议名，None 则自动生成
    category: '周会记录' / '直播复盘' / 'AI课项目' / '客户拜访' / '通用会议'
    on_progress(stage, msg): 进度回调
    返回：保存到 vault 的 MD 路径（本地备份也会同步）
    """
    if not segments:
        raise ValueError("没有选中任何段")

    # 按 start 排序
    segments = sorted(segments, key=lambda s: s.get("start", ""))

    # 默认标题
    if not title:
        first = segments[0].get("start", "")
        title = f"{first[:10]} {first[11:16] if len(first) >= 16 else ''}".strip() or "未命名会议"

    on_progress("prepare", f"分析 {len(segments)} 段语料...")

    # 1. 拼转写文本
    transcript = _build_transcript(segments)
    if not transcript.strip():
        raise RuntimeError("选中的段全部是空文本，无法生成纪要")

    char_count = len(transcript)
    log.info("[meeting] 转写 %d 字 · %d 段 · 分类=%s", char_count, len(segments), category)
    if char_count > 60000:
        log.warning("转写超长（%d 字），可能影响 LLM 表现", char_count)

    # 2. 调 LLM
    try:
        notes = _call_llm_for_meeting(transcript, on_progress=on_progress)
    except Exception as e:
        raise RuntimeError(f"AI 分析失败: {e}")
    notes = enforce_king_attribution(notes, segments)

    # 3. 渲染 MD
    on_progress("render", "拼装会议纪要 Markdown...")
    md = render_meeting_md(title, segments, notes, category=category)

    # 4. 写到 vault 对应目录（优先），同时备份到本地 meetings/
    filename = _meeting_filename(segments, category, title)
    vault_dir = _resolve_obsidian_target(category)
    if vault_dir:
        vault_md = vault_dir / filename
        vault_md.write_text(md, encoding="utf-8")
        log.info("[write] vault: %s", vault_md)
        primary = vault_md
    else:
        primary = None
        log.warning("Obsidian vault 未配置")

    # 本地备份
    local_md = _local_dir() / filename
    local_md.write_text(md, encoding="utf-8")
    log.info("[write] 本地备份: %s", local_md)

    out = primary or local_md
    on_progress("done", f"完成 → {out.name}")
    return out


# ============================================================
# CLI 入口（独立可跑）
# ============================================================
def main():
    import argparse
    parser = argparse.ArgumentParser(description="从 segments JSON 生成会议纪要")
    parser.add_argument("segments_json",
                        help="包含 segment dict 列表的 JSON 文件")
    parser.add_argument("--title", help="会议标题（可选）")
    args = parser.parse_args()

    with open(args.segments_json, "r", encoding="utf-8") as f:
        segments = json.load(f)

    def progress(stage: str, msg: str):
        print(f"[{stage}] {msg}", flush=True)

    try:
        out = export_meeting(segments, title=args.title, on_progress=progress)
        print(f"\n[ok] {out}")
    except Exception as e:
        print(f"\n[error] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
