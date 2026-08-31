"""每日总结:拼当日 jsonl → Claude 总结 → 本地 MD → 飞书云文档。

用法:
    python daily_summary.py                  # 总结今天(增量模式:先做小结,再汇总)
    python daily_summary.py --date 2026-05-23
    python daily_summary.py --no-lark        # 跳过飞书上传(只本地 MD)
    python daily_summary.py --rerun-pending  # 优先补做之前失败的 .pending.json
    python daily_summary.py --mini           # 只做一次"3小时小结",不做最终总结

依赖环境变量(根据 config.toml 里的 provider 选其一):
- deepseek: DEEPSEEK_API_KEY
- anthropic: ANTHROPIC_API_KEY (可选 ANTHROPIC_BASE_URL 走代理)
- openai: OPENAI_API_KEY (可选 OPENAI_BASE_URL 走代理)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (
    CONFIG,
    ROOT,
    configured_owner_name,
    configured_obsidian_vault,
    knowledge_dir,
    note_path,
    read_jsonl,
    redact,
    setup_logger,
    transcript_path,
)
from onboarding_profile import load_user_profile
from runtime_profile import feature_enabled

log = setup_logger("daily-summary")


def obsidian_note_path(day: dt.date) -> Path | None:
    """返回 Obsidian vault 内对应日期的 MD 路径，配置缺失时返回 None。"""
    obs = CONFIG.get("obsidian", {})
    vault = configured_obsidian_vault()
    folder = obs.get("folder", "语音工作日志")
    if not vault:
        return None
    p = vault / folder
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{day.isoformat()}.md"


def write_obsidian(day: dt.date, md: str) -> Path | None:
    """把 MD 同步写一份到 Obsidian vault，返回路径；失败返回 None（不抛）。"""
    path = obsidian_note_path(day)
    if path is None:
        return None
    try:
        path.write_text(md, encoding="utf-8")
        log.info("Obsidian 同步完成: %s", path)
        return path
    except Exception as e:
        log.warning("Obsidian 写入失败: %s", e)
        return None


# 小结存储目录
MINI_DIR = ROOT / "notes" / "mini"
ATTRIBUTION_POLICY_VERSION = 2


def mini_path(day: dt.date, hour: int) -> Path:
    """notes/mini/YYYY-MM-DD-HH.json — 覆盖式写入(同小时重跑会覆盖旧的)。"""
    MINI_DIR.mkdir(parents=True, exist_ok=True)
    return MINI_DIR / f"{day.isoformat()}-{hour:02d}.json"


def list_mini_summaries(day: dt.date) -> list[dict]:
    """按小时顺序返回当天所有已有小结,每条包含 {hour, summary_text, last_ts, segment_count}。"""
    MINI_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for p in sorted(MINI_DIR.glob(f"{day.isoformat()}-??.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            # 旧版小结把所有实时录音都当作 King 本人，不能继续参与汇总。
            if data.get("attribution_policy_version") == ATTRIBUTION_POLICY_VERSION:
                results.append(data)
        except Exception:
            pass
    return results


def _last_mini_ts(day: dt.date) -> str | None:
    """返回当天最后一条小结记录的 last_ts(ISO 字符串)，或 None。"""
    minis = list_mini_summaries(day)
    if not minis:
        return None
    return minis[-1].get("last_ts")

# ============================================================
# 通用归属规则：本人 vs 转述（所有 prompt 共用）
# ============================================================
_ATTRIBUTION_RULES = """
说明：下文历史规则里的“king”只是“当前用户本人”的内部代称；最终输出请使用“本人”或第一人称，不要输出名字 king。

【极度重要：本人 vs 转述 严格区分】

king 的口播里有两种性质的内容，必须严格分开。**这是整套系统最容易出错也最关键的地方**。

▎判断方法（必须用）
读到任何一段话时问自己：**"这话当时是 king 自己原创产生的，还是从外部接收到的？"**

▎本人内容（king 自己的）
- 主语是 "我" + 主动动作：我在做 X / 我决定 Y / 我跟某某说 Z
- 基于自己的经验/数据/思考做出的判断
- 跟具体人对话产生的内容（"我跟张老师讨论了..."）
- 自己产出的代码/作品/决策

▎转述内容（从外部接收）—— 千万不要写成 king 的事
- "我看到一个视频说..." / "某博主讲..." / "视频里讲..."
- "听说..." / "有人提到..." / "听他们说..."
- 出现外部数据（销量/营业额/价格）但没有"我亲测/我算过/我做过"
- 介绍某个产品/方法论/赛道的具体细节（很可能是接收到的）

▎边界判断
- "某月某日要做某活动" —— 如果 king 没说"我某日要做"，那大概率是听别人讲的，**不要写进 king 的待办**
- "某产品卖了多少万本/营业额多少万" —— 这是数据，king 不会随口说精确数字，肯定是听来的
- "三万元搞定某流程" —— 服务商报价或视频里的信息，**不是 king 的承诺**

▎产出规则
- 凡是无法明确判断为"king 本人"的内容，**默认归到转述/外部信息**
- 宁可漏掉一些真的是 king 自己的想法，**也绝不能把转述内容混入 king 的待办/项目/行动**
- 涉及销量、营业额、价格、具体百分比等数据 → 几乎一定是转述

▎源标记（最强信号 — 不容置疑）
时间轴里的方括号前缀就是来源标记，按下面规则直接判定，**不要再去猜**：
- `[本人·king]` —— 声纹已明确识别为 king，可以作为本人内容
- `[待确认说话人]` —— 声纹未确认，绝不能写进本人观点、行动、金句、待办或项目
- `[他人·姓名]` —— 已识别为其他人，只能作为他人内容，绝不能归因给 king
- `[外部·抖音 · 博主名 · 标题]` —— king **粘贴的抖音视频**，整段是博主的内容，
  必须归到 `external_quotes`，**不能写进 todos/activities/my_quotes**
- `[外部·B站 · UP主 · 标题]` —— 同上，B 站 UP 主的内容
- `[外部·公众号 · 作者 · 标题]` —— 同上，公众号文章的内容

外部前缀的所有内容，在 narrative 里只能写"king 今天看了/学习了 XXX 关于 YYY 的内容"，
**绝对不能写成"king 今天做了 YYY"**。
如果要统计 king 今天的"学习/输入"时间，可以基于这些外部段落数量做估计。
"""


SUMMARY_PROMPT = """你是用户的私人秘书。下面是用户一天的口播片段（已按时间排序，每段一行）。下文历史规则中的 king 均指当前用户本人，最终输出不要使用名字 king。
""" + _ATTRIBUTION_RULES + """

请输出一份当日总结，严格按下面的 JSON schema 输出（只输出 JSON，不要任何前后缀或代码块）：

{
  "narrative": "用 200-400 字概括 king 今天**本人**做了什么、想了什么（绝不混入转述内容）",
  "activities": ["今天 king **本人** 做了哪些事，3-7 条，每条一句，动词开头。看视频/听播客不算做事"],
  "my_quotes": ["king **本人** 说出的有价值的金句或洞察，3-8 条"],
  "external_quotes": ["king **转述** 的外部内容：视频/博主/他人观点。每条用'【转述·来源】...'格式"],
  "learning_inputs": [
    "king 今天主动学习/输入的外部内容,每条形如 '抖音·博主·标题 · 一句核心要点'。",
    "**只从源标记为 `[外部·xxx]` 的段落里抽取**,这是 king 主动贴链接进来的。",
    "0-10 条均可,没有就空数组。"
  ],
  "todos": ["king **本人明确承诺且尚未完成的下一步动作**。必须有动作和对象；不能把推测当作本人承诺"],
  "todo_suggestions": ["根据 king 本人正在推进的事情推导出的可执行下一步，仅作待确认建议，不能冒充本人承诺"],
  "themes": ["反复出现的关注主题，2-5 个名词短语"],
  "mood": "king 整体状态，一句话（累/兴奋/焦虑/平静等）"
}

【最关键的检查】产生 todos 之前再确认一次：
- 每条 todos 是不是 king 第一人称承诺的？
- 还是博主/视频/朋友说的"应该做 X"？
- 后者绝对不能进 todos，要进 external_quotes / learning_inputs
- 是否能直接回答“下一步具体做什么”？不能回答就不是待办
- “考虑/可能/以后/后续/目标是/争取”不是承诺；同义重复只保留一条

人名直接用真名（张老师、李同学、王老板），不要用"某A/某B"。全程中文。
"""


def _clamped_limit(value, default: int = 3, maximum: int = 20) -> int:
    try:
        return max(1, min(maximum, int(value)))
    except (TypeError, ValueError):
        return default


def _todo_preferences() -> dict:
    workflow = (load_user_profile(ROOT).get("workflow_preferences") or {})
    raw = workflow.get("todos") or {}
    return {
        "enabled": raw.get("enabled", True) is not False,
        "capture_mode": str(raw.get("capture_mode") or "explicit_only"),
        "types": [str(item) for item in (raw.get("types") or []) if str(item).strip()],
        "max_items_per_run": _clamped_limit(raw.get("max_items_per_run"), 3),
    }


def _summary_prompt_with_preferences(base_prompt: str) -> str:
    prefs = _todo_preferences()
    limit = prefs["max_items_per_run"]
    types = "、".join(prefs["types"]) or "本人明确承诺、截止事项、项目下一步"
    if not prefs["enabled"]:
        rule = "用户未启用待办：todos 和 todo_suggestions 都必须返回空数组。"
    elif prefs["capture_mode"] == "confirmed_and_suggested":
        rule = (
            f"正式 todos 最多 {limit} 条，只放本人已确认或明确承诺的事项；"
            f"todo_suggestions 最多 {limit} 条，可从本人正在推进的事情中宽松提出下一步，"
            "但必须具体可执行，并且永远与正式待办分开、等待用户确认。"
        )
    else:
        rule = f"todos 最多 {limit} 条，只放本人明确确认的事项；todo_suggestions 必须为空数组。"
    return base_prompt + f"\n\n【本机用户的待办偏好】\n允许类型：{types}\n{rule}\n"


def load_segments(day: dt.date) -> list[dict]:
    path = transcript_path(day)
    records = list(read_jsonl(path))
    # 只保留有文本的,按 start 排序
    valid = [r for r in records if r.get("text")]
    valid.sort(key=lambda r: r.get("start", ""))
    return valid


_EXTERNAL_SOURCES = {"douyin", "bilibili", "wechat"}
_SOURCE_TAG = {
    "tx_import": "导入",
    "douyin": "外部·抖音",
    "bilibili": "外部·B站",
    "wechat": "外部·公众号",
}


def _speaker_name(segment: dict) -> str:
    return str(segment.get("speaker_name") or "").strip()


def _is_king_segment(segment: dict) -> bool:
    """只有声纹明确识别为当前用户的语音才属于本人。"""
    speaker = _speaker_name(segment).casefold()
    return speaker in {configured_owner_name().casefold(), "我", "king"}


def _summary_segments(segments: list[dict]) -> list[dict]:
    """AI 归纳只接收 King 本人语音和显式外部输入。

    未知或其他说话人的实时录音保留在原始时间轴中，但不进入任何本人总结、
    复盘、待办、项目追踪、第二大脑或内容素材。
    """
    return [
        segment for segment in segments
        if _is_king_segment(segment) or segment.get("source") in _EXTERNAL_SOURCES
    ]


def build_fulltext(segments: list[dict]) -> str:
    """把今天的所有片段拼成时间轴文本。

    - 声纹明确识别为 King 时使用 [本人·king]
    - 未知或其他说话人保留在原文，但明确标成待确认/他人
    - 贴链接抓回来的外部内容(抖音/B站/公众号)用 [外部·xxx | 标题] 前缀,
      明确告诉 LLM 这一段是 king 在看/听别人的内容,
      不能写成 king 自己做的事
    """
    lines = []
    for r in segments:
        ts = r.get("start", "")[11:19] if r.get("start") else ""  # HH:MM:SS
        source = r.get("source", "")
        if source in _EXTERNAL_SOURCES:
            tag = _SOURCE_TAG.get(source, f"外部·{source}")
            title = (r.get("title") or "").strip()
            uploader = (r.get("uploader") or "").strip()
            meta_bits = [tag]
            if uploader:
                meta_bits.append(uploader)
            if title:
                meta_bits.append(title[:30])
            prefix = "[" + " · ".join(meta_bits) + "]"
        elif _is_king_segment(r):
            prefix = f"[本人·{configured_owner_name()}]"
        else:
            speaker = _speaker_name(r)
            if speaker and speaker not in ("?", "未知"):
                prefix = f"[他人·{speaker}]"
            else:
                prefix = "[待确认说话人]"
        lines.append(f"[{ts}] {prefix} {r['text']}")
    return "\n".join(lines)


def build_summary_fulltext(segments: list[dict]) -> str:
    """构建可用于 AI 归纳的硬过滤时间轴。"""
    return build_fulltext(_summary_segments(segments))


def _strip_code_fence(raw: str) -> str:
    """容错:把 ```json ... ``` 之类的代码块壳剥掉。"""
    raw = raw.strip()
    if raw.startswith("```"):
        # 去掉首行(```json 或 ```)和尾行(```)
        lines = raw.splitlines()
        if len(lines) >= 2:
            lines = lines[1:-1] if lines[-1].strip().startswith("```") else lines[1:]
        raw = "\n".join(lines).strip()
    return raw


def _parse_json_object(raw: str) -> dict:
    """解析模型返回的 JSON 对象，并容忍代码块、前后说明和常见漏逗号。

    这里只做确定性的语法修复，不猜测或改写模型正文。仍无法解析时抛出原始
    ``JSONDecodeError``，由调用方决定是否发起一次受控的 JSON 修复请求。
    """
    cleaned = _strip_code_fence(raw or "").strip()
    if not cleaned:
        return {}
    first = cleaned.find("{")
    last = cleaned.rfind("}")
    if first >= 0 and last > first:
        cleaned = cleaned[first:last + 1]

    first_error: json.JSONDecodeError | None = None
    for candidate in (cleaned,):
        try:
            parsed = json.loads(candidate)
            if not isinstance(parsed, dict):
                raise ValueError("模型返回的 JSON 顶层不是对象")
            return parsed
        except json.JSONDecodeError as exc:
            first_error = exc

    # 常见失败：一个字段值结束后直接开始下一个字段，漏了逗号。
    repaired = re.sub(
        r'(?<=[\]}\"])\s*(?=\"(?:review_md|open_questions)\"\s*:)',
        ",",
        cleaned,
    )
    if repaired != cleaned:
        try:
            parsed = json.loads(repaired, strict=False)
            if not isinstance(parsed, dict):
                raise ValueError("模型返回的 JSON 顶层不是对象")
            return parsed
        except json.JSONDecodeError:
            pass

    if first_error is not None:
        raise first_error
    raise json.JSONDecodeError("无法解析 JSON 对象", cleaned, 0)


def _repair_review_json(client, model: str, raw: str) -> dict:
    """让同一模型只修复一次复盘 JSON 语法，不改写正文内容。"""
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "你是严格的 JSON 语法修复器。只修复输入中的 JSON 语法错误，"
                    "不得总结、删减或改写 review_md 正文。输出必须是一个包含 "
                    "review_md（字符串）和 open_questions（数组）的合法 JSON 对象。"
                ),
            },
            {"role": "user", "content": raw},
        ],
        response_format={"type": "json_object"},
        max_tokens=4000,
        temperature=0,
    )
    return _parse_json_object(response.choices[0].message.content or "{}")


def _call_deepseek(fulltext: str) -> dict:
    from ai_gateway import OpenAI, provider_api_key
    cfg = CONFIG["summary"]
    api_key = provider_api_key("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY 未设置")
    client = OpenAI(api_key=api_key, base_url=cfg["base_url_deepseek"])
    resp = client.chat.completions.create(
        model=cfg["model_deepseek"],
        messages=[
            {"role": "system", "content": _summary_prompt_with_preferences(SUMMARY_PROMPT)},
            {"role": "user", "content": f"今日口播片段:\n\n{fulltext}"},
        ],
        response_format={"type": "json_object"},
        max_tokens=2000,
        temperature=0.3,
    )
    return json.loads(resp.choices[0].message.content or "{}")


def _call_anthropic(fulltext: str) -> dict:
    import anthropic
    cfg = CONFIG["summary"]
    client = anthropic.Anthropic()  # 自动读 ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL
    resp = client.messages.create(
        model=cfg["model_anthropic"],
        max_tokens=2000,
        system=[{"type": "text", "text": _summary_prompt_with_preferences(SUMMARY_PROMPT), "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": [
            {"type": "text", "text": f"今日口播片段:\n\n{fulltext}",
             "cache_control": {"type": "ephemeral"}},
        ]}],
    )
    raw = "".join(b.text for b in resp.content if hasattr(b, "text"))
    return json.loads(_strip_code_fence(raw))


def _call_openai(fulltext: str) -> dict:
    from ai_gateway import OpenAI
    cfg = CONFIG["summary"]
    client = OpenAI()  # 自动读 OPENAI_API_KEY / OPENAI_BASE_URL
    resp = client.chat.completions.create(
        model=cfg["model_openai"],
        messages=[
            {"role": "system", "content": _summary_prompt_with_preferences(SUMMARY_PROMPT)},
            {"role": "user", "content": f"今日口播片段:\n\n{fulltext}"},
        ],
        response_format={"type": "json_object"},
        max_tokens=2000,
        temperature=0.3,
    )
    return json.loads(resp.choices[0].message.content or "{}")


_PROVIDERS = {
    "deepseek": _call_deepseek,
    "anthropic": _call_anthropic,
    "openai": _call_openai,
}


def call_claude(fulltext: str) -> dict:
    """根据 config.summary.provider 路由到对应模型;带重试。函数名保留兼容。"""
    cfg = CONFIG["summary"]
    provider = cfg.get("provider", "deepseek")
    fn = _PROVIDERS.get(provider)
    if fn is None:
        raise RuntimeError(f"未知 provider: {provider}")
    last_err = None
    for attempt in range(1, cfg["retry"] + 1):
        try:
            log.info("调用 %s (attempt=%d)", provider, attempt)
            return fn(fulltext)
        except Exception as e:
            last_err = e
            log.warning("%s 调用失败 attempt=%d: %s", provider, attempt, e)
            # 未授权、未登录、Token 不足等客户端确定性错误不应重复提交。
            # 网络异常可以继续走原有重试；服务端 Provider 本身已有三次重试。
            try:
                from auth_client import AuthConnectivityError, AuthError
                from cloud_consent import CloudConsentRequiredError
                stop_retry = isinstance(e, CloudConsentRequiredError) or (
                    isinstance(e, AuthError)
                    and not isinstance(e, AuthConnectivityError)
                )
            except ImportError:
                stop_retry = False
            if stop_retry:
                raise
            if attempt < cfg["retry"]:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"{provider} 调用 {cfg['retry']} 次全部失败: {last_err}")


def render_md(day: dt.date, segments: list[dict], summary: dict, fulltext: str) -> str:
    total_chars = sum(len(s["text"]) for s in segments)
    total_sec = sum(s.get("duration_sec", 0) for s in segments)

    def _items(key: str, prefix: str = "- ") -> list[str]:
        items = summary.get(key) or []
        return [f"{prefix}{x}" for x in items] if items else ["（无）"]

    parts = [
        "---",
        "ai_generated: true",
        "ai_service_provider: 声年",
        f"content_id: shengnian-daily-{day.isoformat()}",
        f"source_date: {day.isoformat()}",
        "---",
        "",
        f"# 语音日记 {day.isoformat()}",
        "",
        "> AI 辅助整理，使用或发布前请核实；末尾原文为本机转写。",
        "",
        f"> 录音 {len(segments)} 段 · {total_chars} 字 · "
        f"{int(total_sec // 60)} 分 {int(total_sec % 60)} 秒 · "
        f"状态：{summary.get('mood', '—')}",
        "",
        "---",
        "",
        "## 今日概述",
        "",
        summary.get("narrative", ""),
        "",
        "## 今天做了什么",
        "",
    ]
    parts += _items("activities")
    parts += [
        "",
        "## 我的金句",
        "",
        "> 以下是今天我自己说出的有价值的表达",
        "",
    ]
    my_quotes = summary.get("my_quotes") or []
    if my_quotes:
        for q in my_quotes:
            parts += [f"> 「{q}」", ""]
    else:
        parts += ["（无）", ""]

    ext_quotes = summary.get("external_quotes") or []
    if ext_quotes:
        parts += [
            "## 转述·他人观点",
            "",
            "> 以下内容来自他人（短视频/对话），不代表我的观点",
            "",
        ]
        for q in ext_quotes:
            parts += [f"- {q}"]
        parts += [""]

    # 学习/输入(贴链接抓回来的外部内容)
    learning_inputs = summary.get("learning_inputs") or []
    # 顺便统计今天的外部输入段落数,给一个粗略的"学习时长"参考
    ext_seg_count = sum(
        1 for s in segments if s.get("source") in ("douyin", "bilibili", "wechat")
    )
    # 收集今天所有 wiki_anchor(去重保序),用于双链
    seen_anchors: list[str] = []
    for s in segments:
        a = s.get("wiki_anchor")
        if a and a not in seen_anchors:
            seen_anchors.append(a)

    if learning_inputs or ext_seg_count:
        parts += [
            "## 学习·输入",
            "",
            f"> 今天主动贴了 {ext_seg_count} 段外部内容(抖音/B 站/公众号)",
            "",
        ]
        for q in learning_inputs:
            parts += [f"- {q}"]
        if seen_anchors:
            parts += ["", "**今日输入档案条目**(点击在 Obsidian 跳转):", ""]
            for a in seen_anchors:
                parts += [f"- [[输入档案#{a}]]"]
        parts += [""]

    parts += ["## 待办事项", ""]
    todos = summary.get("todos") or []
    parts += [f"- [ ] {x}" for x in todos] if todos else ["（无）"]

    suggestions = summary.get("todo_suggestions") or []
    if suggestions:
        parts += ["", "### 待确认建议", "", "> 以下内容由模型根据本人语料推导，确认后才会进入正式待办。", ""]
        parts += [f"- [ ] {x}" for x in suggestions]

    parts += ["", "## 关注主题", ""]
    themes = summary.get("themes") or []
    parts += [f"`{t}`" for t in themes] if themes else ["（无）"]

    chunk_narratives = summary.get("chunk_narratives") or []
    if chunk_narratives:
        parts += ["", "---", "", "## 分时段小结", ""]
        for i, s in enumerate(chunk_narratives):
            parts += [f"**时段 {i + 1}**", "", s, ""]

    parts += ["", "---", "", "## 原文（带时间戳）", "", "```", fulltext, "```", ""]
    return "\n".join(parts)


def recover_local_note(day: dt.date) -> Path | None:
    """Recover a readable daily note without sending transcript text to AI.

    This is an outage fallback, not a replacement for the normal summary.  It
    preserves any completed mini summaries and the full local transcript, and
    keeps the pending marker so a later successful cloud run can replace it.
    """
    segments = load_segments(day)
    if not segments:
        log.warning("[recovery] %s 没有任何转写段", day.isoformat())
        return None

    md_path = note_path(day)
    if md_path.exists():
        existing = md_path.read_text(encoding="utf-8")
        if "summary_status: pending_cloud" not in existing:
            write_obsidian(day, existing)
            log.info("[recovery] 已有正式日记，保留原文件: %s", md_path)
            return md_path

    minis = list_mini_summaries(day)
    summary = {
        "mood": "待云端总结",
        "narrative": (
            "声年账号服务暂时不可用，本页已从本机数据恢复。"
            f"当天共有 {len(segments)} 段转写、{len(minis)} 条分时段小结；"
            "完整原文保留在末尾，云端恢复后会自动重试正式总结。"
        ),
        "activities": [],
        "my_quotes": [],
        "external_quotes": [],
        "todos": [],
        "todo_suggestions": [],
        "themes": ["本地恢复", "待云端重试"],
        "chunk_narratives": [
            str(item.get("summary_text") or "").strip()
            for item in minis
            if str(item.get("summary_text") or "").strip()
        ],
    }
    fulltext = redact(build_fulltext(segments))
    md = render_md(day, segments, summary, fulltext)
    md = md.replace(
        f"source_date: {day.isoformat()}\n---",
        f"source_date: {day.isoformat()}\nsummary_status: pending_cloud\n---",
        1,
    )
    md_path.write_text(md, encoding="utf-8")
    write_obsidian(day, md)
    pending = ROOT / "notes" / f"{day.isoformat()}.pending.json"
    if not pending.exists():
        pending.write_text(
            json.dumps(
                {
                    "error": "本地恢复日记已生成，等待云端正式总结",
                    "ts": time.time(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    log.info("[recovery] 本地恢复日记已写入: %s", md_path)
    return md_path


def upload_to_lark(day: dt.date, md: str) -> str | None:
    """上传到飞书云文档,返回 doc URL 或 None。失败不抛(让本地 MD 已经保存)。"""
    try:
        title = f"语音日记 {day.isoformat()}"
        proc = subprocess.run(
            ["lark-cli", "docs", "+create", "--title", title, "--content", "-", "--api-version", "v2"],
            input=md.encode("utf-8"),
            capture_output=True,
            timeout=120,
        )
        if proc.returncode != 0:
            log.error("lark-cli 上传失败: %s", proc.stderr.decode("utf-8", "ignore"))
            return None
        out = proc.stdout.decode("utf-8", "ignore")
        log.info("飞书上传输出: %s", out[:500])
        # 解析 JSON 拿 URL(具体字段名要看实际返回,先存原始 stdout)
        try:
            data = json.loads(out)
            url = data.get("url") or data.get("data", {}).get("url")
            return url
        except json.JSONDecodeError:
            return None
    except FileNotFoundError:
        log.warning("lark-cli 未在 PATH 中,跳过飞书上传")
        return None
    except Exception as e:
        log.error("上传飞书异常: %s", e)
        return None


CHUNK_THRESHOLD_CHARS = 50000  # 超过则分块
CHUNK_WINDOW_HOURS = 2          # 每块覆盖多少小时


def _split_into_chunks(segments: list[dict]) -> list[list[dict]]:
    """按时间窗口切分;窗口起点是第一段的小时整点。"""
    if not segments:
        return []
    from datetime import timedelta, datetime as _dt
    chunks: list[list[dict]] = []
    cur: list[dict] = []
    window_end: _dt | None = None
    for r in segments:
        try:
            ts = _dt.fromisoformat(r["start"])
        except (ValueError, KeyError):
            cur.append(r)
            continue
        if window_end is None:
            anchor = ts.replace(minute=0, second=0, microsecond=0)
            window_end = anchor + timedelta(hours=CHUNK_WINDOW_HOURS)
            cur = [r]
        elif ts < window_end:
            cur.append(r)
        else:
            if cur:
                chunks.append(cur)
            cur = [r]
            window_end = ts.replace(minute=0, second=0, microsecond=0) + timedelta(hours=CHUNK_WINDOW_HOURS)
    if cur:
        chunks.append(cur)
    return chunks


CHUNK_SUMMARY_PROMPT = """这是用户一天中一个 2 小时时间段的口播片段。请用 80-150 字概括用户在聊/想什么。下文 king 均指当前用户本人，最终输出不要使用名字 king。

""" + _ATTRIBUTION_RULES + """

【输出格式】
分两段写，每段以特定标记开头：

[本人]king 自己做了 X、想了 Y、跟谁聊了什么...
[转述]king 接收到的外部信息：视频里讲 X、博主说 Y...

只输出简短文本，不要 JSON、不要分点、不要标题。如果某一类为空，就只写另一段。"""


MINI_SUMMARY_PROMPT = """你是用户的私人秘书。下面是用户最近一段时间（约 3 小时）的口播片段。下文 king 均指当前用户本人，最终输出不要使用名字 king。
""" + _ATTRIBUTION_RULES + """

请用 100-200 字概括这段时间，重点：
- king **自己**有价值的想法或决策（不要混入听来的）
- king **自己**承诺要做的事（"我下周去做 X"才算，"某博主建议下周做 X" 不算）
- king 的情绪/状态

【输出格式】
分两段：

[本人]xxx...
[转述]xxx...

只输出简短叙事文本，不要 JSON、不要分点标题、不要任何前缀。全程使用中文。如果某段为空可省略。"""


INCREMENTAL_FINAL_PROMPT = """你是用户的私人秘书。下面是用户今天按时间段拆分的多段“阶段小结”。下文 king 均指当前用户本人，最终输出不要使用名字 king。
请基于这些小结（不要假设有未提及的内容），输出今天的完整总结。

""" + _ATTRIBUTION_RULES + """

严格按下面的 JSON schema 输出（只输出 JSON，不要任何前后缀或代码块）：

{
  "narrative": "用 200-400 字概括 king 今天**本人**做了什么、想了什么（绝对不混入转述内容，转述去 external_quotes）",
  "activities": ["今天 king **本人** 做了哪些事，3-7 条，每条一句，动词开头。看视频/听播客不算做事"],
  "my_quotes": ["king **本人** 说出的有价值的金句或洞察，3-8 条。基于他自己的判断/经验"],
  "external_quotes": ["king **转述** 的外部内容：视频/博主/他人观点。每条用'【转述·来源】...'格式"],
  "learning_inputs": [
    "king 今天主动学习/输入的外部内容,每条形如 '抖音·博主·标题 · 一句核心要点'。",
    "**只从源标记为 `[外部·xxx]` 的段落里抽取**(粘贴链接抓回来的)。",
    "0-10 条均可,没有就空数组。"
  ],
  "todos": ["king **本人明确承诺且尚未完成的下一步动作**。必须有动作和对象；不能把推测当作本人承诺"],
  "todo_suggestions": ["根据 king 本人正在推进的事情推导出的可执行下一步，仅作待确认建议，不能冒充本人承诺"],
  "themes": ["反复出现的关注主题，2-5 个名词短语"],
  "mood": "king 整体状态，一句话（累/兴奋/焦虑/平静等）"
}

【最关键的检查】产生 todos 之前，再确认一次：
- 每条 todos 是不是 king 第一人称承诺的？（"我要做 X" / "我准备 Y"）
- 还是别人/视频/博主说的"应该做 X"？
- 如果是后者，**绝对不要进 todos**，进 external_quotes / learning_inputs
- 是否有明确动作和对象，能直接开始执行？没有就不进
- “考虑/可能/以后/后续/目标是/争取”不算承诺；同义重复只保留一条

【其他规则】
- 涉及具体人名直接用真名（张老师、李同学、王老板），不要用"某A/某B"
- my_quotes 和 external_quotes 不能交叉混合
- learning_inputs 必须严格来自 `[外部·xxx]` 段落
- 全程使用中文
"""


def _summarize_chunk(chunk_text: str) -> str:
    """用 provider 给一个 chunk 写 80-150 字的叙事。复用同样的 _call_* 函数,但 prompt 不同。"""
    cfg = CONFIG["summary"]
    provider = cfg.get("provider", "deepseek")
    # 走 chat,不强求 JSON
    if provider == "deepseek" or provider == "openai":
        from ai_gateway import OpenAI
        if provider == "deepseek":
            client = OpenAI(api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
                           base_url=cfg["base_url_deepseek"])
            model = cfg["model_deepseek"]
        else:
            client = OpenAI()
            model = cfg["model_openai"]
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": CHUNK_SUMMARY_PROMPT},
                {"role": "user", "content": chunk_text},
            ],
            max_tokens=400,
            temperature=0.3,
        )
        return (resp.choices[0].message.content or "").strip()
    else:  # anthropic
        import anthropic
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=cfg["model_anthropic"],
            max_tokens=400,
            system=CHUNK_SUMMARY_PROMPT,
            messages=[{"role": "user", "content": chunk_text}],
        )
        return "".join(b.text for b in resp.content if hasattr(b, "text")).strip()


def _call_mini_llm(text: str) -> str:
    """调用 LLM 生成 mini-summary 叙事文本（纯文本，100-200字）。"""
    cfg = CONFIG["summary"]
    provider = cfg.get("provider", "deepseek")
    if provider in ("deepseek", "openai"):
        from ai_gateway import OpenAI
        if provider == "deepseek":
            client = OpenAI(api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
                            base_url=cfg["base_url_deepseek"])
            model = cfg["model_deepseek"]
        else:
            client = OpenAI()
            model = cfg["model_openai"]
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": MINI_SUMMARY_PROMPT},
                {"role": "user", "content": text},
            ],
            max_tokens=500,
            temperature=0.3,
        )
        return (resp.choices[0].message.content or "").strip()
    else:  # anthropic
        import anthropic
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=cfg["model_anthropic"],
            max_tokens=500,
            system=MINI_SUMMARY_PROMPT,
            messages=[{"role": "user", "content": text}],
        )
        return "".join(b.text for b in resp.content if hasattr(b, "text")).strip()


def mini_summarize(day: dt.date | None = None, mock: bool = False) -> dict | None:
    """对当天「上次小结之后」的新段做一次小结，保存到 notes/mini/YYYY-MM-DD-HH.json。

    返回保存的 mini-summary dict，如果没有新内容则返回 None。
    """
    if day is None:
        day = dt.date.today()

    # 找出上次小结截止的时间戳
    last_ts = _last_mini_ts(day)

    # 加载当天所有 segments
    segments = load_segments(day)
    if not segments:
        log.info("[mini] %s 没有任何转写段，跳过", day.isoformat())
        return None

    # 只取 last_ts 之后的新段
    if last_ts:
        new_segs = [s for s in segments if s.get("start", "") > last_ts]
    else:
        new_segs = segments

    if not new_segs:
        log.info("[mini] %s 自上次小结以来没有新段，跳过", day.isoformat())
        return None

    log.info("[mini] %s 新段 %d 条 (last_ts=%s)", day.isoformat(), len(new_segs), last_ts)

    fulltext = redact(build_summary_fulltext(new_segs))
    now = dt.datetime.now()
    hour = now.hour

    if mock:
        summary_text = f"[mock] {day.isoformat()} {hour:02d}:00 段小结占位文本。"
    elif not fulltext.strip():
        summary_text = "[本人]本时段未识别到 king 本人语音。\n\n[转述]无。"
    else:
        try:
            summary_text = _call_mini_llm(fulltext)
        except Exception as e:
            log.error("[mini] LLM 调用失败: %s", e)
            summary_text = f"[小结失败: {e}]"

    # 计算本次新段的时间范围
    new_start = new_segs[0].get("start", "")
    new_end = new_segs[-1].get("start", "")

    result = {
        "day": day.isoformat(),
        "hour": hour,
        "created_at": now.isoformat(),
        "attribution_policy_version": ATTRIBUTION_POLICY_VERSION,
        "segment_count": len(new_segs),
        "period_start": new_start,
        "period_end": new_end,
        "last_ts": new_segs[-1].get("start", ""),  # 下次小结的起点
        "summary_text": summary_text,
    }

    path = mini_path(day, hour)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("[mini] 小结已写入 %s", path)
    return result


MOCK_SUMMARY = {
    "narrative": "[mock] 今天主要在推进语音日记项目，讨论了总结维度的优化方案，测试了 DeepSeek API 连通性。",
    "activities": ["[mock] 调试了启动器的总结按钮", "[mock] 和 Claude 讨论了金句提炼需求"],
    "my_quotes": ["[mock] 随口而出的表达，往往会更有价值", "[mock] 语料是你最核心的数字资产"],
    "external_quotes": ["[mock] 某博主：流量时代注意力就是货币"],
    "learning_inputs": ["[mock] 抖音·某博主·短视频结构 · 钩子+爆点+CTA 是基本三段式"],
    "todos": ["[mock] 测试真实总结流程"],
    "themes": ["[mock] 语音日记", "[mock] AI 工具"],
    "mood": "[mock] 专注",
}


def _incremental_summary(day: dt.date, minis: list[dict]) -> dict:
    """基于已有小结做当天最终汇总（不重读全量 jsonl）。"""
    # 先把当天"小结截止之后"的新段也做一次临时 mini（不保存文件）
    last_ts = minis[-1].get("last_ts") if minis else None
    segments = load_segments(day)
    if last_ts:
        tail_segs = [s for s in segments if s.get("start", "") > last_ts]
    else:
        tail_segs = segments

    # 拼所有小结文本 + 可能的尾段
    parts = []
    for m in minis:
        t = m.get("period_start", "")[11:16]  # HH:MM
        parts.append(f"【{t} 段】{m['summary_text']}")

    if tail_segs:
        log.info("[incremental] 追加尾段 %d 条做临时小结", len(tail_segs))
        tail_text = redact(build_summary_fulltext(tail_segs))
        if tail_text.strip():
            try:
                tail_mini = _call_mini_llm(tail_text)
            except Exception as e:
                tail_mini = f"[临时小结失败: {e}]"
        else:
            tail_mini = "[本人]最新时段未识别到 king 本人语音。\n\n[转述]无。"
        t = tail_segs[0].get("start", "")[11:16]
        parts.append(f"【{t} 段（最新）】{tail_mini}")

    digest = "\n\n".join(parts)
    log.info("[incremental] 基于 %d 段小结 + 尾段 做最终汇总", len(minis))

    # 调 LLM 出最终 JSON
    cfg = CONFIG["summary"]
    provider = cfg.get("provider", "deepseek")
    if provider in ("deepseek", "openai"):
        from ai_gateway import OpenAI
        if provider == "deepseek":
            client = OpenAI(api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
                            base_url=cfg["base_url_deepseek"])
            model = cfg["model_deepseek"]
        else:
            client = OpenAI()
            model = cfg["model_openai"]
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _summary_prompt_with_preferences(INCREMENTAL_FINAL_PROMPT)},
                {"role": "user", "content": f"今日各时段小结：\n\n{digest}"},
            ],
            response_format={"type": "json_object"},
            max_tokens=2000,
            temperature=0.3,
        )
        summary = json.loads(resp.choices[0].message.content or "{}")
    else:  # anthropic
        import anthropic
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=cfg["model_anthropic"],
            max_tokens=2000,
            system=[{"type": "text", "text": _summary_prompt_with_preferences(INCREMENTAL_FINAL_PROMPT),
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": f"今日各时段小结：\n\n{digest}"}],
        )
        raw = "".join(b.text for b in resp.content if hasattr(b, "text"))
        summary = json.loads(_strip_code_fence(raw))

    # 把各时段小结追加到 summary 供 MD 渲染
    summary["chunk_narratives"] = [m["summary_text"] for m in minis]
    return summary


def summarize_day(day: dt.date, no_lark: bool = False, mock: bool = False,
                  force_full: bool = False) -> None:
    """生成当天完整总结。

    - 有 mini-summary 时走增量路径（基于小结汇总，不重读全量 jsonl）。
    - 没有 mini-summary 或 force_full=True 时走全量路径。
    """
    segments = load_segments(day)
    if not segments:
        log.warning("%s 没有任何转写段,跳过", day.isoformat())
        return
    total_chars = sum(len(s["text"]) for s in segments)
    log.info("处理 %s: %d 段, %d 字", day.isoformat(), len(segments), total_chars)
    fulltext = build_fulltext(segments)
    fulltext_redacted = redact(fulltext)
    verified_segments = _summary_segments(segments)
    verified_chars = sum(len(s["text"]) for s in verified_segments)
    verified_fulltext_redacted = redact(build_fulltext(verified_segments))
    log.info(
        "归属硬过滤后: %d 段, %d 字（未知/他人实时语音不参与总结）",
        len(verified_segments),
        verified_chars,
    )

    if mock:
        summary = MOCK_SUMMARY
    elif not verified_segments:
        summary = {
            "narrative": "当天没有识别到可确认属于 king 本人的语音，未生成本人总结。",
            "activities": [],
            "my_quotes": [],
            "external_quotes": [],
            "learning_inputs": [],
            "todos": [],
            "todo_suggestions": [],
            "themes": [],
            "mood": "无可确认的本人语音",
        }
    else:
        minis = list_mini_summaries(day)
        if minis and not force_full:
            log.info("发现 %d 条小结，走增量模式", len(minis))
            try:
                summary = _incremental_summary(day, minis)
            except Exception as e:
                log.warning("增量总结失败(%s)，降级全量", e)
                summary = _full_summary(
                    day, verified_segments, verified_fulltext_redacted, verified_chars
                )
        else:
            if force_full:
                log.info("force_full=True，走全量模式")
            summary = _full_summary(
                day, verified_segments, verified_fulltext_redacted, verified_chars
            )

    md = render_md(day, segments, summary, fulltext_redacted)
    md_path = note_path(day)
    md_path.write_text(md, encoding="utf-8")
    log.info("本地 MD 已写入 %s", md_path)

    # 同步到 Obsidian vault
    write_obsidian(day, md)

    # 更新第二大脑 wiki（不阻塞，失败不影响主流程）
    try:
        update_wiki(day, summary, mock=mock)
    except Exception as e:
        log.warning("[wiki] 更新失败（不影响总结）: %s", e)

    # 卡帕西式 LLM Wiki:维护 entities/concepts/index/log（不阻塞）
    if not mock:
        try:
            import wiki_appender
            wiki_appender.update_from_daily(day, verified_fulltext_redacted)
        except Exception as e:
            log.warning("[wiki/karpathy] 失败（不影响总结）: %s", e)

        # 月度体检（C3）：跨月才跑一次，搭语音日记便车；失败不影响总结
        try:
            import monthly_lint
            if monthly_lint.run_if_due(day):
                log.info("[lint] 本月体检已生成（第二大脑/_lint/）")
        except Exception as e:
            log.warning("[lint] 月度体检失败（不影响总结）: %s", e)

    # 生成复盘报告（不阻塞，失败不影响总结）
    try:
        generate_review(day, mock=mock)
    except Exception as e:
        log.warning("[review] 生成失败（不影响总结）: %s", e)

    pending = ROOT / "notes" / f"{day.isoformat()}.pending.json"
    if pending.exists():
        try:
            pending.unlink()
            log.info("已清除 pending 文件: %s", pending)
        except OSError as e:
            log.warning("pending 文件清理失败（不影响总结）: %s", e)

    if no_lark:
        log.info("--no-lark 跳过飞书上传")
        return
    url = upload_to_lark(day, md)
    if url:
        log.info("飞书文档 URL: %s", url)
        # 把 URL 追加到本地 MD 末尾,方便回看
        with open(md_path, "a", encoding="utf-8") as f:
            f.write(f"\n\n[飞书文档]({url})\n")


def _full_summary(day: dt.date, segments: list[dict],
                  fulltext_redacted: str, total_chars: int) -> dict:
    """全量模式：直接读 jsonl 全文做总结（原有逻辑）。"""
    if total_chars > CHUNK_THRESHOLD_CHARS:
        log.info("字数 %d 超过 %d,启用分块总结", total_chars, CHUNK_THRESHOLD_CHARS)
        chunks = _split_into_chunks(segments)
        log.info("分成 %d 块", len(chunks))
        chunk_summaries: list[str] = []
        for i, chunk in enumerate(chunks):
            chunk_text = redact(build_fulltext(chunk))
            log.info("总结第 %d/%d 块(%d 段)", i + 1, len(chunks), len(chunk))
            chunk_summaries.append(_summarize_chunk(chunk_text))
        digest = "\n\n".join(f"【时段 {i+1}】{s}" for i, s in enumerate(chunk_summaries))
        summary = call_claude(digest)
        summary["chunk_narratives"] = chunk_summaries
    else:
        summary = call_claude(fulltext_redacted)
    return summary


# ============================================================
# Second Brain Wiki 更新
# ============================================================

WIKI_UPDATE_PROMPT = """你是用户的私人知识管理助手。下文 king 均指当前用户本人，最终输出不要使用名字 king。
""" + _ATTRIBUTION_RULES + """

## 你的任务
根据今日总结，输出严格的 JSON（不要代码块，不要前后缀）：

{
  "projects": "完整的项目追踪 Markdown 内容（替换原文件）",
  "todos_delta": {
    "add": ["今日新增的待办文本，不带 (来源:xxx)（系统会自动加）"],
    "complete": ["今日完成的待办文本（要跟原待办的文本完全一致，能匹配上才能标完成）"]
  },
  "brief": "完整的每日简报 Markdown 内容（替换原文件）"
}

⛔ **关键**：todos 用 delta（增量）输出，不要全量覆盖。
- `add`：今日 king 新承诺的待办（必须来自 todos 字段，已经过滤过转述）
- `complete`：今日完成的（要跟 todos_old 里的某条文本能精确匹配；不确定就别放）
- **不要在 add 里重复已有待办**——LLM 看到 "已有待办总览" 上下文里的旧条目，那些是历史，不要再加。
- 没有新增就 add 给 []，没有完成就 complete 给 []。
- `add` 的数量遵循系统附加的本机用户偏好；不要把未确认建议、目标、设想、讨论题或模糊项目名塞进来。

代码本地会做合并：保留所有旧待办 → 加 add 进来 → 把 complete 里能匹配上的从待完成移到本周已完成。

## ⛔ 红线（任何 prompt 的最高优先级）

下面三件事**绝对不能进**待办总览、项目追踪、今日重大提醒：

1. **博主/视频/播客里讲的"应该做 X" / "下周做 Y" / "建议..."**
   — 那是博主对他的观众的建议，不是 king 对自己的承诺

2. **king 听到的具体日期/数字**
   — "某月某日某活动" / "某产品销量多少" / "某服务的报价多少"
   — king 一般不会在录音里精确报这种数字，听到了几乎一定是转述

3. **泛指的方法论 / 知识介绍**
   — "某领域有几种方法" / "某事的几条路径"
   — 这是博主的内容框架，归到 external_quotes 不归 todos

## ✋ 产出 todos / 提醒 前的强制自检

对每一条候选 todos 或提醒，先在内心回答：

**"原话中有 'king 第一人称承诺' 的句子吗？比如'我下周要 X' '我去做 Y' '我安排 Z'？"**

- 有 → 可以进
- 没有，只是讨论 / 听说 / 评估 / 介绍 → **不能进**，归到 external_quotes 或 themes

**当不确定时，默认归到外部信息，不进 todos。** 宁可少一条真的 todos，也不要错放一条转述。

## 项目追踪（projects）规则
- 格式固定：
  # 项目追踪
  > 最后更新：YYYY-MM-DD
  ## 进行中
  ### 项目名称
  - **最新进度**：一句话
  - **下一步**：一句话
  - **最后提及**：YYYY-MM-DD
  ## 已完成
  - 项目名 (完成日期)
- **只追踪 king 本人在做的项目**。看视频里别人在做的项目不算
- 从今日 activities / narrative 提取，**绝对不从 external_quotes 提取**
- 超过 30 天没提及的项目移到已完成（除非明确还在推进）

## 待办总览（todos）规则
- 格式固定：
  # 待办总览
  > 最后更新：YYYY-MM-DD
  ## 待完成
  - [ ] 待办内容 (来源：YYYY-MM-DD)
  ## 本周已完成
  - [x] 已完成内容 (完成：YYYY-MM-DD)
- **只把 todos 字段的内容合并进来**（todos 已经在上游过滤过本人/转述）
- **绝对不要从 narrative 或 external_quotes 自己提取新待办**
- 若今日 activities 或 narrative 明确提到某个历史待办已完成，移到已完成区
- 一条合格待办必须同时满足：king 第一人称明确承诺、包含动作和对象、尚未完成、能在一次执行中推进
- “考虑/可能/以后/后续/目标/愿景/是否/如何”以及纯项目名，不是待办
- 项目只能写成一个具体下一步，例如“推进课程”不收，“把第三节讲义发给学员”才收
- 同义重复只保留一条；当天新增数量遵循系统附加的本机用户偏好

## 每日简报（brief）规则 — 给"今天还在进行中"看的轻量简报
- 格式固定：
  # 每日简报 YYYY-MM-DD
  ## 状态
  > {一句话状态：忙碌/兴奋/焦虑/疲惫等，结合 mood 字段}
  ## 今日重大提醒
  > 基于【已有项目追踪】【已有待办总览】中即将到期或重要的事项判断
  > 只能从 todos 字段和 projects 字段产生，**绝对不要凭空生成"应该做 X"**
  1. 第一重要提醒（必须是 king 自己承诺的事）
  2. 第二重要提醒（必须是 king 自己承诺的事）
  3. 第三重要提醒（如有，可省）
  ## 今日主要在做什么
  > 严格只列 king 本人**主动做的事**，3-5 条主线。看视频/听播客/听别人讲不算"做"
  - 主线一：xxx
  - 主线二：xxx
  - 主线三：xxx
  ## 时间分配 · 汇总
  > 把【今日时间轴】按事情类别分桶加总，不显示细粒度明细
  - **实操（写代码/调试/做工具/录课）**：约 X 小时 Y 分钟
  - **讨论/思考（聊天/评估/复盘）**：约 X 小时 Y 分钟
  - **学习/输入（看视频/听播客/阅读）**：约 X 小时 Y 分钟
  - **杂事（生活/吃饭/通勤）**：约 X 分钟
  > 总有效时长：约 X 小时

- 提醒优先级：① deadline 临近的待办 ② 进行中项目的下一步 ③ 今天产生的关键决策

## ⚠️ "今日主要在做什么" 的严格规则（最容易出错的部分）

**只列 king 主动做的事，绝对不列他接收到的信息**。

### 必须用的判断方法

读完每个候选主线，问自己一个问题：**"king 当时身体在做什么动作？"**

- 如果答案是"敲键盘、打电话、说话、走路、写字、点鼠标" = ✅ 做的事
- 如果答案是"坐着看屏幕、戴耳机听、滑手机刷视频" = ❌ 是接收信息

### 反例（这些都不算"做"）

- ❌ "评估 AI 书籍出版赛道可行性" — 如果是看视频里博主分析的销量数据/路径
- ❌ "分析社群产品定位问题" — 如果是听别人讨论的
- ❌ "思考知识主播话术结构" — 如果是博主把四种结构讲给你听的
- ❌ "了解 XX 行业现状" / "学习 XX 方法论"

这些应该归到"学习/输入"类时间，**不出现在"今日主要在做什么"列表里**。

### 正例（这些才算"做"）

- ✅ "调试语音转录系统" — 你在敲键盘改代码
- ✅ "制作 AI 音乐电台" — 你在跑 Claude Code
- ✅ "跟张老师讨论直播话术" — 你在打电话/对话
- ✅ "修复飞书会议助手报错" — 你在改代码

### 规则总结

宁可只列 2-3 条扎实的"实操+对话"，**也不要把"接收信息"算作"做的事"**。
如果今天 80% 时间是看视频，那"今日主要在做什么"就只列那 20% 实操的事，剩下 80% 进时间分配的"学习/输入"桶。

时间汇总规则：
* 同一类目的多段时间累加，只输出总时长
* 不要列出明细（"00:07-01:00 53分钟" 这种细节不进简报）
* 类目划分：实操 / 讨论 / 学习 / 杂事 四桶
* **看视频、听播客、听别人讲、被动接收信息 → 归"学习/输入"，不归"讨论"**
* "讨论"专指 king 跟具体的人（张老师/李同学等）真的对话
* 真的没有就写 "无" 或不列那一行

全程中文，**人名直接用真名**（如张老师、李同学、王老板），不要用"某A/某B"。
"""


def _is_actionable_todo(text: str) -> bool:
    """本地兜底：拦截目标、设想、讨论题和没有动作的模糊条目。"""
    import re
    s = re.sub(r"\s+", "", (text or "").strip())
    if len(s) < 5 or "?" in s or "？" in s:
        return False
    if any(word in s for word in ("可能", "也许", "考虑", "以后", "后续", "将来",
                                  "目标是", "愿景", "争取", "是否", "如何")):
        return False
    if s.startswith(("计划", "目标", "想法", "建议")):
        return False
    action_verbs = (
        "提交", "发送", "联系", "沟通", "检查", "修改", "调整", "完成",
        "制作", "整理", "更新", "修复", "测试", "调试", "录制", "发布",
        "安排", "邀请", "准备", "确认", "确定", "购买", "申请", "交付",
        "上传", "下载", "删除", "优化", "增加", "搭建", "封装", "同步",
        "提供", "写", "做", "让", "把", "将", "去",
    )
    return any(verb in s for verb in action_verbs)


def _merge_todos_delta(day: dt.date, add: list[str], complete: list[str]) -> None:
    """本地合并待办增量：保留所有旧待办，加新增的，把完成的移到本周已完成。

    add: 新增的待办文本列表（不带 来源、deadline 标签）
    complete: 今天完成的待办文本列表（要能匹配 待完成 区里的现有条目）
    """
    import re
    p = _wiki_path("待办总览.md")
    if not p:
        return
    today_str = day.isoformat()
    if not p.exists():
        # 不存在则创建
        content = f"# 待办总览\n> 最后更新：{today_str}\n## 待完成\n## 本周已完成\n"
        p.write_text(content, encoding="utf-8")

    text = p.read_text(encoding="utf-8")
    lines = text.splitlines()

    # 找区段位置
    pending_idx = -1
    done_idx = -1
    for i, ln in enumerate(lines):
        s = ln.strip()
        if pending_idx < 0 and (re.search(r"^##.*待完成", s) or re.search(r"^##.*待办", s)):
            pending_idx = i
        elif done_idx < 0 and (re.search(r"^##.*已完成", s)):
            done_idx = i
    if pending_idx < 0 or done_idx < 0:
        log.warning("[todos-merge] 待办文件结构异常，跳过合并")
        return

    pending_lines = lines[pending_idx + 1: done_idx]
    done_lines = lines[done_idx + 1:]

    # 1. 处理 complete：把待完成区里能匹配上的条目移到已完成
    moved = []
    new_pending = []
    for ln in pending_lines:
        s = ln.strip()
        if not s.startswith("- [ ]"):
            new_pending.append(ln)
            continue
        # 提取核心文本（去掉 deadline / 来源标记）
        body = s[5:].strip()
        body_clean = re.sub(r"\s*\(deadline[：:].+?\)", "", body)
        body_clean = re.sub(r"\s*\(来源[：:].+?\)", "", body_clean).strip()
        body_clean = body_clean.replace("📌", "").strip()
        matched = False
        for c in complete:
            c_clean = c.strip()
            # 模糊匹配：核心 8 字符相同就算匹配
            if c_clean and (c_clean in body_clean or body_clean in c_clean or
                            (len(c_clean) >= 6 and c_clean[:6] in body_clean)):
                matched = True
                break
        if matched:
            done_line = f"- [x] {body_clean} (完成：{today_str})"
            moved.append(done_line)
        else:
            new_pending.append(ln)

    # 2. 处理 add：把新增的加到待完成末尾（不重复）
    existing_texts = set()
    for ln in new_pending + done_lines:
        s = ln.strip()
        if s.startswith("- [ ]") or s.startswith("- [x]"):
            body = s[5:].strip()
            body = re.sub(r"\s*\(.+?\)", "", body).strip()
            existing_texts.add(body[:12])  # 前 12 字符作为去重 key

    prefs = _todo_preferences()
    if not prefs["enabled"]:
        add = []
    per_run_limit = prefs["max_items_per_run"]
    max_active = max(15, min(100, per_run_limit * 5))
    active_count = sum(1 for ln in new_pending if ln.strip().startswith("- [ ]"))
    available_slots = max(0, max_active - active_count)
    add_limit = min(per_run_limit, available_slots)
    added_lines = []
    for a in add:
        a = a.strip()
        if not a or not _is_actionable_todo(a):
            continue
        if a[:12] in existing_texts:
            continue
        if len(added_lines) >= add_limit:
            break
        added_lines.append(f"- [ ] {a} (来源：{today_str})")
        existing_texts.add(a[:12])

    # 3. 重组文件
    out = lines[:pending_idx + 1]              # 标题 + ## 待完成
    out.extend(new_pending)                    # 原待完成（已去掉 moved 的）
    out.extend(added_lines)                    # 新增的
    out.append(lines[done_idx])                # ## 本周已完成
    out.extend(moved)                          # 新完成的
    out.extend(done_lines)                     # 原已完成

    # 更新"最后更新"行
    for i, ln in enumerate(out):
        if ln.startswith("> 最后更新"):
            out[i] = f"> 最后更新：{today_str}"
            break

    p.write_text("\n".join(out) + "\n", encoding="utf-8")
    log.info("[todos-merge] 新增 %d 条，完成 %d 条", len(added_lines), len(moved))


def _wiki_path(filename: str) -> Path:
    """Return an always-writable local knowledge path.

    Obsidian is an optional viewer.  Without a configured vault, knowledge
    files live under the application's local notes directory.
    """
    return knowledge_dir() / filename


def _read_wiki(filename: str) -> str:
    p = _wiki_path(filename)
    if p is None or not p.exists():
        return ""
    return p.read_text(encoding="utf-8")


def update_wiki(day: dt.date, summary: dict, mock: bool = False) -> None:
    """根据今日总结更新 Obsidian 第二大脑三个文件。"""
    log.info("[wiki] 开始更新第二大脑...")

    projects_old = _read_wiki("项目追踪.md")
    todos_old = _read_wiki("待办总览.md")

    # 把今日总结关键字段提炼成输入
    today_str = day.isoformat()

    # 加载今日时间轴（每段 start + 本段时长 + 文本前 80 字），给 LLM 算耗时用
    # ⚠ 关键修复：原来只给 HH:MM、不给每段时长，LLM 没有时长原料，
    #   "时间分配·汇总"四个桶只能全填 0。现在补上每段秒数 + 系统精算总时长。
    timeline_rows: list[str] = []
    total_active_sec = 0
    try:
        segs = load_segments(day)
        for s in segs:
            start = (s.get("start") or "")[11:16]  # HH:MM
            txt = (s.get("text") or "").strip()[:80]
            dur = int(s.get("duration_sec", 0) or 0)
            total_active_sec += dur
            if start and txt:
                timeline_rows.append(f"{start} (+{dur}s) | {txt}")
    except Exception:
        pass
    # 限制喂给 LLM 的行数（极端日子可能上千段，全喂太贵；取首尾各一半）
    if len(timeline_rows) > 400:
        timeline_rows = timeline_rows[:200] + ["...（中间省略）..."] + timeline_rows[-200:]
    timeline_text = "\n".join(timeline_rows) if timeline_rows else "（无）"
    _h, _m = divmod(int(total_active_sec) // 60, 60)
    total_active_text = f"{_h} 小时 {_m} 分钟"

    digest = f"""今日日期：{today_str}

【今日做了什么 · 来自总结】
{chr(10).join('- ' + a for a in (summary.get('activities') or []))}

【今日待办】
{chr(10).join('- ' + t for t in (summary.get('todos') or []))}

【今日叙事】
{summary.get('narrative', '')}

【我的金句】
{chr(10).join('- ' + q for q in (summary.get('my_quotes') or []))}

【今日有效录音总时长 · 系统已精确计算】
{total_active_text}
（"时间分配·汇总"四个桶加总应≈这个总时长，绝不能全部填 0）

【今日时间轴 · 用于把时长分配到四个桶】（HH:MM (+本段秒数) | 片段前 80 字）
{timeline_text}

【当前项目追踪文件内容】
{projects_old or '（空）'}

【当前待办总览文件内容】
{todos_old or '（空）'}
"""

    if mock:
        result = {
            "projects": f"# 项目追踪\n\n> 最后更新：{today_str}\n\n## 进行中\n\n### [mock] 语音日记系统\n- **最新进度**：wiki 功能开发中\n- **下一步**：上线测试\n- **最后提及**：{today_str}\n\n## 已完成\n",
            "todos": f"# 待办总览\n\n> 最后更新：{today_str}\n\n## 待完成\n\n- [ ] [mock] 测试 wiki 更新 (来源：{today_str})\n\n## 本周已完成\n",
            "brief": f"# 每日简报 {today_str}\n\n## 今日提醒\n\n> 状态：[mock] 专注\n\n1. 测试 wiki 更新功能\n\n## 最重要的项目\n\n- **语音日记系统**：wiki 开发中 → 上线测试\n",
        }
    else:
        cfg = CONFIG["summary"]
        provider = cfg.get("provider", "deepseek")
        try:
            if provider in ("deepseek", "openai"):
                from ai_gateway import OpenAI
                if provider == "deepseek":
                    client = OpenAI(api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
                                    base_url=cfg["base_url_deepseek"])
                    model = cfg["model_deepseek"]
                else:
                    client = OpenAI()
                    model = cfg["model_openai"]
                resp = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": _summary_prompt_with_preferences(WIKI_UPDATE_PROMPT)},
                        {"role": "user", "content": digest},
                    ],
                    response_format={"type": "json_object"},
                    max_tokens=4000,
                    temperature=0.2,
                )
                result = json.loads(resp.choices[0].message.content or "{}")
            else:
                import anthropic
                client = anthropic.Anthropic()
                resp = client.messages.create(
                    model=cfg["model_anthropic"],
                    max_tokens=4000,
                    system=_summary_prompt_with_preferences(WIKI_UPDATE_PROMPT),
                    messages=[{"role": "user", "content": digest}],
                )
                raw = "".join(b.text for b in resp.content if hasattr(b, "text"))
                result = json.loads(_strip_code_fence(raw))
        except Exception as e:
            log.error("[wiki] LLM 调用失败: %s", e)
            return

    # 写入：projects 和 brief 仍全量覆盖；todos 用 delta 本地合并
    for filename, key in [("项目追踪.md", "projects"),
                          ("每日简报.md", "brief")]:
        content = result.get(key, "")
        if not content:
            continue
        p = _wiki_path(filename)
        if p:
            p.write_text(content, encoding="utf-8")
            log.info("[wiki] 已更新 %s", filename)

    # ── 待办：本地增量合并（保护历史不丢失） ──
    delta = result.get("todos_delta", {})
    if isinstance(delta, dict) and (delta.get("add") or delta.get("complete")):
        _merge_todos_delta(day, delta.get("add") or [], delta.get("complete") or [])
    else:
        # 兜底：万一 LLM 没按 delta 格式输出，退回到旧 todos 字段
        legacy = result.get("todos", "")
        if legacy:
            log.warning("[wiki] LLM 未按 delta 格式输出 todos，跳过待办更新（保护历史）")

    log.info("[wiki] 第二大脑更新完成")


# ============================================================
# 昨日复盘 · "我昨天干了什么 + 时间分配 + 可优化"
# ============================================================
REVIEW_PROMPT = """你是用户的私人时间复盘助手。任务：根据用户【昨天的转写时间轴】和【昨天的活动总结】，生成一份复盘报告 + 0-3 个真正值得在未来 7 天内做决定的问题。下文 king 均指当前用户本人，最终输出不要使用名字 king。

输出严格的 JSON：
{
  "review_md": "完整的 Markdown 文本,按下面规则写",
  "open_questions": [
    {
      "id": "q1",
      "title": "一句话点出他没想清楚的问题(15-25 字)",
      "context": "为什么觉得他没想清楚 — 引用他昨天 1-2 段原话/观察(50-120 字)",
      "why_matters": "如果不想清楚会有什么后果 / 想清楚能解锁什么(30-80 字)",
      "related_quotes": ["他昨天的某段原话(20-50 字)", "..."]
    }
  ]
}

## review_md 结构
Markdown 必须严格按下面来:

```
# 昨日复盘 YYYY-MM-DD

## 昨天做了什么
- **事件 1(约 X 小时 Y 分钟)**:详细描述这件事 — 完成度、关键决策、卡点
- **事件 2(约 X 分钟)**:...
- **事件 3(约 X 小时)**:...
> 按时间消耗从多到少排序,5-8 件主要事件

## 时间分配
- **实操类**:X 小时 Y 分钟 — 写代码、调试、做工具、录课
- **讨论思考**:X 小时 Y 分钟 — 聊天、复盘、评估
- **学习输入**:X 小时 Y 分钟 — 看视频、听播客、阅读、粘的链接
- **杂事**:X 分钟
> 总有效时长:X 小时

## 可以优化的地方
> 不是泛泛而谈,要具体。基于时间分配 + 内容质量提建议:
1. 第一条优化建议(指出某件事花的时间太长/太短,给出更好的分配方式)
2. 第二条(指出某个低价值动作可以省略、或某个高价值动作没足够时间)
3. 第三条(基于昨天的卡点提建议)

## 待讨论
> 以下是几个 king 昨天表达过但**还没想清楚**的问题,可在 launcher 里点「讨论」展开:
- **q1 · 一句话点出问题**
  > 简要回顾相关上下文(30-50 字)
- **q2 · ...**
  > ...
```

⚠ 「待讨论」节里的 q1/q2 编号必须和 JSON open_questions 数组的 id 一一对应。

## open_questions 提取规则(最关键)
只有同时满足下面 5 条，才进入「待讨论」：
1. 未来 7 天内确实要做决定；
2. 决定会明显影响收入、时间、团队、产品或交付；
3. 至少有两个真实可选方案，存在取舍；
4. 通过一次讨论能得到明确结论；
5. 结论会直接触发下一步行动。

任务、搜索、实验、Bug、SOP、知识问题、纯假设、已经有答案的问题，都不进入「待讨论」。

⚠ 不要凭空虚构,只能从原话里观察。每个 question 的 related_quotes 必须真的能在时间轴里找到。

⚠ 数量 0-3 个。默认可以是空数组；没有高价值真实决策就一个也不要给。

## 复盘核心原则
- **不是流水账**:合并同主题的时段,给出"这件事一共花了多久"
- **诚实评估**:如果某事花了 3 小时但价值低,要直接指出"花得不值"
- **可操作建议**:每条优化都要能落地("明天可以..."、"下次应该...")
- **基于真实数据**:时间数字从【时间轴】严格推算,不要编造
- **人名用真名**(张老师、李同学),不要用"某A/某B"
- **外部输入识别**:时间轴里 `[外部·xxx]` 前缀的段落是他贴的链接,
  全部归到「学习输入」时段,内容不能算作他自己做的事

全程中文。"""


def generate_review(day: dt.date, mock: bool = False) -> Path | None:
    """生成某一天的复盘报告，写到 notes/YYYY-MM-DD-review.md。"""
    log.info("[review] 开始生成 %s 复盘...", day.isoformat())
    discussion_enabled = feature_enabled("deep_discussion")

    # 加载该天的 segments + 已有总结
    segments = _summary_segments(load_segments(day))
    if not segments:
        log.warning("[review] %s 没有任何转写段", day.isoformat())
        return None

    # 时间轴（前 80 字）
    timeline_rows: list[str] = []
    for s in segments:
        start = (s.get("start") or "")[11:16]
        txt = (s.get("text") or "").strip()[:80]
        if start and txt:
            timeline_rows.append(f"{start} | {txt}")
    if len(timeline_rows) > 500:
        timeline_rows = timeline_rows[:250] + ["...（中间省略）..."] + timeline_rows[-250:]
    timeline_text = "\n".join(timeline_rows)

    # 已有日总结（如果有）
    note_p = note_path(day)
    summary_md = note_p.read_text(encoding="utf-8") if (note_p and note_p.exists()) else ""

    # 取当前 open 清单 — 让 LLM 别重复提
    existing_titles = []
    if discussion_enabled:
        try:
            import open_questions as _oq
            for q in _oq.list_open():
                existing_titles.append(q.get("title", ""))
        except Exception:
            pass
    existing_block = "\n".join(f"- {t}" for t in existing_titles) if existing_titles else "(无)"

    user_content = f"""日期：{day.isoformat()}
总段数：{len(segments)}

【时间轴 · 用于推算耗时】（HH:MM | 内容片段）
{timeline_text}

【已有日总结，仅供参考，不要照抄】
{summary_md[:3000] if summary_md else '（无）'}

【当前的「待讨论」清单 — 这些问题已经在队列里,**不要再重复提**】
{existing_block}

⚠ open_questions 字段里只放**真正新的**问题。如果今天的内容和上面清单完全重叠,
   open_questions 可以为空数组,不要为了凑数硬提。
"""
    if not discussion_enabled:
        user_content += (
            "\n【商业 V1 约束】深度讨论暂不启用。open_questions 必须返回空数组，"
            "review_md 不要生成『待讨论』章节。\n"
        )

    if mock:
        result_md = (
            f"# 昨日复盘 {day.isoformat()}\n\n"
            f"## 昨天做了什么\n- mock 事件 1\n\n"
            f"## 时间分配\n- 实操类:3 小时\n\n"
            f"## 可以优化的地方\n1. mock 建议\n\n"
            f"## 待讨论\n- **q1 · mock 没想清楚的问题**\n  > mock 上下文\n"
        )
        open_questions = [{
            "id": "q1",
            "title": "mock 没想清楚的问题",
            "context": "mock 上下文",
            "why_matters": "mock 重要性",
            "related_quotes": ["mock 原话"],
        }] if discussion_enabled else []
    else:
        cfg = CONFIG["summary"]
        provider = cfg.get("provider", "deepseek")
        try:
            if provider in ("deepseek", "openai"):
                from ai_gateway import OpenAI
                if provider == "deepseek":
                    client = OpenAI(api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
                                    base_url=cfg["base_url_deepseek"])
                    model = cfg["model_deepseek"]
                else:
                    client = OpenAI()
                    model = cfg["model_openai"]
                resp = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": REVIEW_PROMPT},
                        {"role": "user", "content": user_content},
                    ],
                    response_format={"type": "json_object"},
                    max_tokens=4000,
                    temperature=0.3,
                )
                raw = resp.choices[0].message.content or "{}"
                try:
                    data = _parse_json_object(raw)
                except json.JSONDecodeError as parse_error:
                    log.warning(
                        "[review] JSON 格式异常，执行一次受控修复重试: %s",
                        parse_error,
                    )
                    data = _repair_review_json(client, model, raw)
            else:
                import anthropic
                client = anthropic.Anthropic()
                resp = client.messages.create(
                    model=cfg["model_anthropic"],
                    max_tokens=4000,
                    system=REVIEW_PROMPT,
                    messages=[{"role": "user", "content": user_content}],
                )
                raw = "".join(b.text for b in resp.content if hasattr(b, "text"))
                data = _parse_json_object(raw)
            result_md = data.get("review_md", "")
            open_questions = data.get("open_questions") or []
        except Exception as e:
            log.error("[review] LLM 调用失败: %s", e)
            return None

    if not discussion_enabled:
        import re
        open_questions = []
        result_md = re.sub(
            r"\n## 待讨论\b.*?(?=\n## |\Z)", "", result_md or "", flags=re.S
        ).rstrip() + "\n"

    if not result_md:
        log.warning("[review] 模型未返回有效内容")
        return None

    out_p = ROOT / "notes" / f"{day.isoformat()}-review.md"
    out_p.parent.mkdir(parents=True, exist_ok=True)
    out_p.write_text(result_md, encoding="utf-8")
    log.info("[review] 复盘已写入 %s", out_p)

    # 把 open_questions 加到 pool(自动去重)
    if discussion_enabled and open_questions:
        try:
            import open_questions as _oq
            r = _oq.add_questions(open_questions, source_date=day.isoformat())
            log.info("[review] 新问题入 pool:新增 %d / 跳过重复 %d",
                     r["added"], r["skipped"])
        except Exception as e:
            log.warning("[review] open_questions 入 pool 失败: %s", e)

    # 内容选题雷达：新版只从当天本人语音中挖短视频选题。
    # 失败不影响复盘；旧公众号选题数据保留，但不再自动生成。
    try:
        import content_radar
        content_radar.extract_from_day(day)
    except Exception as e:
        log.warning("[radar] 短视频选题提取失败(不影响复盘): %s", e)

    return out_p


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="YYYY-MM-DD,默认今天")
    parser.add_argument("--no-lark", action="store_true")
    parser.add_argument("--mock", action="store_true", help="跳过 Claude 调用,用假数据测渲染")
    parser.add_argument("--rerun-pending", action="store_true",
                        help="先把 notes/*.pending.json 涉及的日期补做,再做今天")
    parser.add_argument("--mini", action="store_true",
                        help="只做一次 3 小时小结(不生成完整日总结)")
    parser.add_argument("--force-full", action="store_true",
                        help="强制全量总结(忽略已有小结,重读全文)")
    parser.add_argument("--review", action="store_true",
                        help="生成某一天的复盘报告(默认昨天)")
    parser.add_argument("--recover-local", action="store_true",
                        help="不调用云端 AI，仅从分时段小结和本地转写恢复日记")
    args = parser.parse_args()

    day = dt.date.fromisoformat(args.date) if args.date else dt.date.today()

    if args.recover_local:
        p = recover_local_note(day)
        if p:
            log.info("本地恢复完成: %s", p)
        return

    # 生成复盘（不动其他流程）
    if args.review:
        # 默认是昨天，除非显式给了 --date
        if not args.date:
            day = dt.date.today() - dt.timedelta(days=1)
        try:
            p = generate_review(day, mock=args.mock)
            if p:
                log.info("复盘完成: %s", p)
        except Exception as e:
            log.error("复盘失败: %s", e)
            sys.exit(1)
        return

    # 只做小结
    if args.mini:
        try:
            result = mini_summarize(day, mock=args.mock)
            if result:
                log.info("小结完成: %s 第%02d小时, %d段", day, result["hour"], result["segment_count"])
                # 小结后也更新一次 wiki（待办/项目/简报）——用当天已有小结增量推一遍
                try:
                    minis = list_mini_summaries(day)
                    if minis:
                        log.info("[mini] 用 %d 条小结增量更新 wiki...", len(minis))
                        incr_summary = _incremental_summary(day, minis)
                        update_wiki(day, incr_summary, mock=args.mock)
                except Exception as e:
                    log.warning("[mini] wiki 增量更新失败（不影响小结）: %s", e)
                # concepts/entities 也持续更新:用当天全文提取,覆盖当天分节(幂等,不重复堆)
                if not args.mock:
                    try:
                        import wiki_appender
                        full = redact(build_summary_fulltext(load_segments(day)))
                        if full and len(full) >= 100:
                            log.info("[mini] 增量更新 concepts/entities...")
                            wiki_appender.update_from_daily(day, full)
                    except Exception as e:
                        log.warning("[mini] concepts/entities 更新失败（不影响小结）: %s", e)
            else:
                log.info("没有新内容,跳过小结")
        except Exception as e:
            log.error("小结失败: %s", e)
            sys.exit(1)
        return

    if args.rerun_pending:
        for p in sorted((ROOT / "notes").glob("*.pending.json")):
            try:
                day_str = p.stem.split(".")[0]
                pday = dt.date.fromisoformat(day_str)
                log.info("补做 pending: %s", pday)
                summarize_day(pday, no_lark=args.no_lark, mock=args.mock,
                              force_full=args.force_full)
                p.unlink()
            except Exception as e:
                log.error("补做 %s 失败: %s", p, e)

    try:
        summarize_day(day, no_lark=args.no_lark, mock=args.mock,
                      force_full=args.force_full)
    except Exception as e:
        log.error("总结失败: %s", e)
        pending = ROOT / "notes" / f"{day.isoformat()}.pending.json"
        pending.write_text(json.dumps({"error": repr(e), "ts": time.time()}, ensure_ascii=False), encoding="utf-8")
        log.info("已记录 pending 文件 %s,下次运行会重试", pending)
        try:
            recover_local_note(day)
        except Exception as recovery_error:
            log.warning("本地恢复日记失败（原始转写与 pending 均保留）: %s", recovery_error)
        sys.exit(1)


if __name__ == "__main__":
    main()
