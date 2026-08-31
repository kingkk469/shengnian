"""卡帕西式 LLM Wiki · 维护 第二大脑/entities + concepts + index + log

设计原则:
- Append-only:实体/概念页只追加,不覆盖
- 频率门槛:今日全文里 ≥3 次提及才单独建页(避免一次性话题污染)
- 三种触发:
  1. daily_summary 跑完后 → 从今天全文 LLM 提实体/概念(主要入口)
  2. DiscussDialog 点「✓ 想清楚了」→ 把结论同步到对应实体/概念
  3. ingest_url 抓回外部内容时 → 标记成 [转述] 同步到实体/概念

数据布局:
  第二大脑/
    entities/张老师.md     ← 一个人一个文件,日期 ## 分节,新内容追加
    entities/李同学.md
    concepts/某项目.md     ← 一个概念一个文件,同样追加
    concepts/某话题.md
    index.md               ← 全局索引,列出所有 entities/concepts
    log.md                 ← 全局时间线 ## [YYYY-MM-DD] action | one-liner
    讨论档案.md             ← (旧,继续保留) 完整对话留底
    输入档案.md             ← (旧,继续保留) 贴链接抓回的内容
    每日简报.md 项目追踪.md 待办总览.md  ← (旧,继续保留)
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import CONFIG, ROOT, knowledge_dir, setup_logger

log = setup_logger("wiki-appender")

STATE_PATH = ROOT / "runtime" / "wiki_appender_state.json"


# ============================================================
# 状态文件:跟踪每天跑没跑/失败几次
# ============================================================
def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {"runs": {}}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"runs": {}}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _mark_state(day_str: str, ok: bool, detail: dict | str = "") -> None:
    state = _load_state()
    state["runs"][day_str] = {
        "status": "ok" if ok else "failed",
        "ran_at": dt.datetime.now().isoformat(timespec="seconds"),
        "detail": detail,
        "attempts": state["runs"].get(day_str, {}).get("attempts", 0) + 1,
    }
    _save_state(state)


def get_state_for(day: dt.date) -> dict | None:
    """返回某天的运行状态,没跑过返回 None。"""
    return _load_state()["runs"].get(day.isoformat())


def list_missing_days(lookback_days: int = 7) -> list[dt.date]:
    """找最近 lookback_days 天里:有 transcripts 但 wiki_appender 没跑过(或失败)的日子。
    返回需要补做的日期列表(升序)。"""
    state = _load_state()
    today = dt.date.today()
    missing = []
    for i in range(lookback_days):
        d = today - dt.timedelta(days=i)
        # 必须有 transcripts
        tp = ROOT / "transcripts" / f"{d.isoformat()}.jsonl"
        if not tp.exists():
            continue
        # 看 state
        run = state["runs"].get(d.isoformat())
        if run is None:
            # 从未跑过 — 但只补做"昨天及之前",今天的等 daily_summary 主流程跑
            if d < today:
                missing.append(d)
        elif run.get("status") == "failed" and run.get("attempts", 0) < 3:
            # 失败 < 3 次,可以重试
            missing.append(d)
    return sorted(missing)


# ============================================================
# 路径
# ============================================================
def _wiki_root() -> Path:
    """第二大脑根目录；未配置 Obsidian 时使用本地 notes。"""
    p = knowledge_dir()
    (p / "entities").mkdir(exist_ok=True)
    (p / "concepts").mkdir(exist_ok=True)
    return p


def _entity_path(name: str) -> Path | None:
    root = _wiki_root()
    if root is None:
        return None
    safe = _safe_filename(name)
    return root / "entities" / f"{safe}.md"


def _concept_path(name: str) -> Path | None:
    root = _wiki_root()
    if root is None:
        return None
    safe = _safe_filename(name)
    return root / "concepts" / f"{safe}.md"


def _safe_filename(name: str) -> str:
    """文件名清洗:去 / \\ : * ? " < > | 等危险字符。"""
    return re.sub(r'[\\/:*?"<>|]', "_", name.strip())[:80]


# ============================================================
# 写入:追加到实体/概念页
# ============================================================
def _append_to_page(page_path: Path, day: dt.date, content: str,
                    section_tag: str = "daily") -> None:
    """追加一节到实体/概念页。

    content 已经是 markdown 片段(含 - bullets 之类)。
    自动加 ## YYYY-MM-DD · section_tag 作为分节。
    如果页面不存在,加文件头。
    """
    if not content.strip():
        return
    header = f"## {day.isoformat()} · {section_tag}"
    section = f"{header}\n\n{content.strip()}\n"
    if page_path.exists():
        old = page_path.read_text(encoding="utf-8")
        import re as _re
        # 幂等:同一天同 tag 的分节若已存在则【覆盖】(供 mini 一天多次跑,不重复堆叠)
        pat = _re.compile(r"\n?" + _re.escape(header) + r"\n.*?(?=\n## |\Z)", _re.DOTALL)
        if pat.search(old):
            new_text = pat.sub("\n" + section.rstrip() + "\n", old, count=1)
            page_path.write_text(new_text.rstrip() + "\n", encoding="utf-8")
        else:
            page_path.write_text(old.rstrip() + "\n\n" + section, encoding="utf-8")
    else:
        # 新建,加 H1 标题(用文件名,去 .md)
        title = page_path.stem
        head = f"# {title}\n\n> 由 voice-journal 自动维护 · 按日期追加\n\n"
        page_path.write_text(head + section, encoding="utf-8")
    log.info("[wiki] 写入 %s (+%d 字)", page_path.name, len(content))


_DEFAULT_LOG_HEADER = "# 时间线\n\n> 全局事件流 · 每条一行 ## [日期] 类型 | 摘要"


def _log_key(line: str) -> tuple[str, str]:
    """从 '## [YYYY-MM-DD] action | ...' 解析 (日期, 动作)。"""
    m = re.match(r"^##\s*\[(\d{4}-\d{2}-\d{2})\]\s*([^\s|]+)", line)
    return (m.group(1) if m else "0000-00-00", m.group(2) if m else "other")


def _append_log(wiki_root: Path, day: dt.date, entry: str) -> None:
    """全局时间线 log.md —— 幂等 + 去重 + 按日期排序（每次重写整文件，自愈历史重复）。

    - daily 类型：每天最多一条（重跑覆盖，不再堆叠）。这是历史上 log 被刷成
      2-5 倍重复的根因（daily_summary 一天会重跑多次增量小结，每次 append 一条）。
    - 其他类型（discuss / ingest / ...）：允许一天多条，但去掉完全相同的重复行。
    - 新条目方括号里的日期统一改写成 day —— 防 LLM 生成 log_entry 时写错年份
      （历史上出现过一条 2025 年的乱序记录）。
    """
    log_path = wiki_root / "log.md"

    header = _DEFAULT_LOG_HEADER
    existing: list[str] = []
    if log_path.exists():
        lines = log_path.read_text(encoding="utf-8").splitlines()
        i = 0
        head_lines: list[str] = []
        while i < len(lines) and not lines[i].lstrip().startswith("## ["):
            head_lines.append(lines[i]); i += 1
        if "".join(head_lines).strip():
            header = "\n".join(head_lines).rstrip()
        existing = [ln.strip() for ln in lines[i:] if ln.strip().startswith("## [")]

    # 规范化新条目：取首个非空行，把方括号日期强制改成 day
    new_line = ""
    for ln in entry.strip().splitlines():
        if ln.strip():
            new_line = ln.strip()
            break
    if new_line:
        new_line = re.sub(r"\[\d{4}-\d{2}-\d{2}\]", f"[{day.isoformat()}]", new_line, count=1)
        if not new_line.startswith("## ["):
            new_line = f"## [{day.isoformat()}] other | " + new_line.lstrip("# ").strip()

    # 合并 + 去重：daily 每天一条（后者覆盖），其余按整行去重
    daily_by_date: dict[str, str] = {}
    others: list[str] = []
    seen_others: set[str] = set()
    for line in existing + ([new_line] if new_line else []):
        d, action = _log_key(line)
        if action == "daily":
            daily_by_date[d] = line
        elif line not in seen_others:
            seen_others.add(line)
            others.append(line)

    merged = list(daily_by_date.values()) + others
    merged.sort(key=lambda l: _log_key(l)[0])  # 按日期升序，稳定排序

    log_path.write_text(header.rstrip() + "\n\n" + "\n\n".join(merged) + "\n", encoding="utf-8")


def _refresh_index(wiki_root: Path, day: dt.date,
                   new_entities: list[str] = None,
                   new_concepts: list[str] = None) -> None:
    """重建 index.md(全量,从目录扫文件)。"""
    new_entities = new_entities or []
    new_concepts = new_concepts or []

    entities = sorted((wiki_root / "entities").glob("*.md"))
    concepts = sorted((wiki_root / "concepts").glob("*.md"))

    lines = [
        "# 第二大脑 · 索引",
        f"> 最后更新:{day.isoformat()} · 自动维护",
        "",
    ]
    # 月度体检提醒（C3）：链最近一期体检报告，king 打开 index 即见；删报告则消失
    lint_dir = wiki_root / "_lint"
    if lint_dir.exists():
        reports = sorted(lint_dir.glob("*-体检.md"))
        if reports:
            lines += [
                "## 📋 体检提醒",
                f"- 最近体检报告：[[{reports[-1].stem}]]（处理完删掉该文件，本提醒随之消失）",
                "",
            ]
    lines += [
        f"## 实体 entities · {len(entities)} 个",
        ""
    ]
    for p in entities:
        lines.append(f"- [[entities/{p.stem}|{p.stem}]]")
    lines += ["", f"## 概念 concepts · {len(concepts)} 个", ""]
    for p in concepts:
        lines.append(f"- [[concepts/{p.stem}|{p.stem}]]")

    if new_entities or new_concepts:
        lines += ["", f"## 今日新增 · {day.isoformat()}", ""]
        for n in new_entities:
            lines.append(f"- 实体:[[entities/{n}|{n}]]")
        for n in new_concepts:
            lines.append(f"- 概念:[[concepts/{n}|{n}]]")

    lines += [
        "",
        "---",
        "",
        "## 其他档案",
        "",
        "- [[每日简报]]",
        "- [[待办总览]]",
        "- [[项目追踪]]",
        "- [[讨论档案]]",
        "- [[输入档案]]",
        "- [[会议纪要/]]",
    ]
    (wiki_root / "index.md").write_text("\n".join(lines), encoding="utf-8")


# ============================================================
# Prompt:从今日全文提取 entities/concepts
# ============================================================
DAILY_EXTRACT_PROMPT = """你是用户的私人知识库管理员（卡帕西式 LLM Wiki）。下文 king 均指当前用户本人，最终输出不要使用名字 king。任务:
从今天的口播全文里,提取出**值得单独建页或更新的实体/概念**。

【🔒 最高红线（2026-06-27 king 定）· 方法论只装 king 自己的】
- concept/entity 必须来自 king **在录音里明确讲过的**主张/决定，**或他讨论得出的结论**。
- **绝不把外部信息写成 king 的结论**：凡他转述/引用/听来的（即便出现在自己录音里），一律不建页、不写进任何"king 认为/king 决定"。
- 判不准是不是 king 自己的 → **默认不写**。宁可漏一条真的，也不错把外部当成 king 的方法论。

【⚠️ 极度重要:本人 vs 转述 严格分开】
king 说话有两种性质:
1. king 本人的想法/计划/行动/判断("我"开头,直接陈述自己要做的事)
   → 用 "king" 主语写:'king 计划...' / 'king 认为...' / 'king 正在做...'
2. 转述外部内容(短视频博主、看到的文章、别人说的话)
   引导词:"我看到一个视频..." "他说..." "视频里讲..." "听说..."
   → 用 **[转述·来源]** 前缀:'[转述·短视频] 博主说某个产品的市场数据'

绝对不能把转述写成 king 的观点。

【频率门槛 — 核心规则,违反就是错的】
- 一个名字 / 一个话题 在今日全文里 **出现 ≥ 3 次** 才单独建页或更新
- 出现 1-2 次的:丢到 log_entry,不要建独立页
- 这条规则保护 wiki 不被一次性话题污染(饭名、路人名、灵感闪念)

【entities(人)规则】
- 必须是 ≥ 3 次提及的真人名
- "张老师"、"李同学"、"王老板" 这种反复讨论的合作伙伴/老师才建页
- 一次性提到的路人、饭名一律不建
- 转述里出现的人也算次数,但内容里要标 [转述·xxx]
- 已有页面 → 输出的是要 **追加** 的内容(不要复述老内容)

【concepts(话题/项目/想法)规则】
- 必须是 ≥ 3 次反复出现的话题
- "某项目"、"某产品"、"声纹识别" 这种贯穿全天的议题
- 一次性想法、抱怨不要建页
- 已有页面 → 追加形式

【输出 JSON】
{
  "entities": {
    "张老师": "- king 跟她聊了 X 进度\\n- 她的当前状态:Y\\n- 待跟进:Z\\n- [转述·张老师朋友圈] 她提到...",
    "李同学": "..."
  },
  "concepts": {
    "某项目": "- king 今天的判断:...\\n- 新决策:...\\n- 卡点:...\\n- [转述·短视频] 某博主说某领域的关键点...",
    "某话题": "..."
  },
  "log_entry": "## [YYYY-MM-DD] daily | 用一句话概括今天最重要的事(40-70 字)",
  "index_additions": {
    "entities": ["今天新建的实体名"],
    "concepts": ["今天新建的概念名"]
  }
}

【自检】产出前再确认:
- 每个 entities 内容里至少能引用 3 句不同来源(否则 1-2 次提及,塞 log 不要建页)
- 每个 concepts 能体现"反复思考"特征
- [转述·xxx] 标记完整,没有把别人的话写成 king 的判断
- 没有创建一次性的饭名、路人名

只输出 JSON,不要任何前后缀或代码块。
"""


def _call_llm(system_prompt: str, user_content: str,
              max_tokens: int = 6000) -> dict:
    """统一 LLM 调用(deepseek/openai/anthropic)。"""
    cfg = CONFIG["summary"]
    provider = cfg.get("provider", "deepseek")
    if provider in ("deepseek", "openai"):
        from ai_gateway import OpenAI, provider_api_key
        if provider == "deepseek":
            api_key = provider_api_key("DEEPSEEK_API_KEY")
            if not api_key:
                raise RuntimeError("DEEPSEEK_API_KEY 未设置")
            client = OpenAI(api_key=api_key, base_url=cfg["base_url_deepseek"])
            model = cfg["model_deepseek"]
        else:
            client = OpenAI()
            model = cfg["model_openai"]
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            response_format={"type": "json_object"},
            max_tokens=max_tokens,
            temperature=0.2,
        )
        return json.loads(resp.choices[0].message.content or "{}")
    else:
        import anthropic
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=cfg["model_anthropic"],
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = "".join(b.text for b in resp.content if hasattr(b, "text"))
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)


# ============================================================
# 按来源分流：本人 vs 外部（2026-06-27 收口红线的执行点）
# ============================================================
def _split_fulltext_by_source(fulltext: str) -> tuple[str, str]:
    """把 build_fulltext 的带前缀全文，按来源拆成 (本人文本, 外部文本)。

    行格式：`[HH:MM:SS] [实时]/[导入]/[外部·xxx] 文本`
    - `[外部·...]`（贴的抖音/B站/公众号链接）→ 外部
    - `[实时]`/`[导入]`（king 本人录音）→ 本人
    口头转述（本人录音里转述别人）无法靠前缀分辨，由方法论 prompt 的"转述"红线挡。
    """
    own, ext = [], []
    for line in fulltext.splitlines():
        m = re.search(r"\]\s*(\[[^\]]*\])", line)
        tag = m.group(1) if m else ""
        (ext if tag.startswith("[外部") else own).append(line)
    return "\n".join(own), "\n".join(ext)


# ============================================================
# 主入口 1:daily_summary 跑完调
# ============================================================
def update_from_daily(day: dt.date, fulltext: str) -> dict:
    """每天 daily_summary 跑完后调一次。

    输入:今天的完整口播全文(带 [实时]/[导入]/[外部·xxx] 前缀)
    动作:LLM 提取 entities/concepts → 追加到对应页 → 更新 index + log

    返回:{entities: int, concepts: int, log_added: bool}
    """
    wiki_root = _wiki_root()
    if wiki_root is None:
        log.warning("[wiki] vault 未配置,跳过")
        _mark_state(day.isoformat(), False, "vault_not_configured")
        return {"entities": 0, "concepts": 0, "log_added": False}

    # ⭐ 按来源分流（2026-06-27 收口红线）：方法论只从"本人"内容提炼，
    #   贴的外部链接（抖音/B站/公众号）整段排除，绝不进 concepts/entities。
    own_text, _ext_text = _split_fulltext_by_source(fulltext)

    if not own_text or len(own_text) < 100:
        log.info("[wiki] 今日本人内容太短(<100 字),跳过方法论提炼")
        _mark_state(day.isoformat(), True, "skipped_too_short")
        return {"entities": 0, "concepts": 0, "log_added": False}

    # 准备 prompt input（只喂本人段）
    existing_e = sorted((wiki_root / "entities").glob("*.md"))
    existing_c = sorted((wiki_root / "concepts").glob("*.md"))
    user_content = f"""今日日期:{day.isoformat()}

【已有 entities 页(不重复创建,可追加新内容)】
{', '.join(e.stem for e in existing_e) or '(无)'}

【已有 concepts 页(不重复创建,可追加新内容)】
{', '.join(c.stem for c in existing_c) or '(无)'}

【今日口播全文 · 仅 king 本人录音（已剔除贴的外部链接）】
{own_text[:30000]}
"""

    log.info("[wiki] 调 LLM 提取 entities/concepts(本人 %d 字 / 全文 %d 字)...", len(own_text), len(fulltext))
    try:
        result = _call_llm(DAILY_EXTRACT_PROMPT, user_content)
    except Exception as e:
        log.error("[wiki] LLM 调用失败: %s", e)
        _mark_state(day.isoformat(), False, f"llm_failed:{str(e)[:200]}")
        return {"entities": 0, "concepts": 0, "log_added": False}

    # 写实体页
    entities = result.get("entities") or {}
    for name, content in entities.items():
        p = _entity_path(name)
        if p and content:
            _append_to_page(p, day, content, section_tag="daily")

    # 写概念页
    concepts = result.get("concepts") or {}
    for name, content in concepts.items():
        p = _concept_path(name)
        if p and content:
            _append_to_page(p, day, content, section_tag="daily")

    # log.md
    log_entry = result.get("log_entry", "")
    if log_entry:
        _append_log(wiki_root, day, log_entry)

    # index.md
    additions = result.get("index_additions") or {}
    _refresh_index(
        wiki_root, day,
        new_entities=additions.get("entities") or [],
        new_concepts=additions.get("concepts") or [],
    )

    summary = {
        "entities": len(entities),
        "concepts": len(concepts),
        "log_added": bool(log_entry),
    }
    log.info("[wiki] 完成:%d 实体 / %d 概念 / log %s",
             len(entities), len(concepts), "+1" if log_entry else "x")
    _mark_state(day.isoformat(), True, summary)
    return summary


def backfill_missing_days(lookback_days: int = 7,
                          on_progress=None) -> dict:
    """补做最近 lookback_days 天里缺失的 wiki appender。
    适合 launcher 启动时调:确保即使中间有几天没跑也能补上。

    返回:{ran: list[date_str], skipped: list[date_str], errors: int}
    """
    from daily_summary import load_segments, build_fulltext

    missing = list_missing_days(lookback_days)
    if not missing:
        log.info("[wiki/backfill] 最近 %d 天都已跑过,无需补", lookback_days)
        return {"ran": [], "skipped": [], "errors": 0}

    ran, errors = [], 0
    for day in missing:
        if on_progress:
            on_progress(f"补做 {day.isoformat()} 的 wiki appender...")
        try:
            segments = load_segments(day)
            if not segments:
                _mark_state(day.isoformat(), True, "no_segments")
                continue
            fulltext = build_fulltext(segments)
            r = update_from_daily(day, fulltext)
            ran.append(day.isoformat())
            log.info("[wiki/backfill] %s OK: %s", day, r)
        except Exception as e:
            errors += 1
            log.error("[wiki/backfill] %s 失败: %s", day, e)
            _mark_state(day.isoformat(), False, f"backfill_error:{str(e)[:200]}")
    return {"ran": ran, "skipped": [], "errors": errors}


# ============================================================
# 主入口 2:DiscussDialog 点「✓ 想清楚了」时调
# ============================================================
DISCUSS_TAG_PROMPT = """你是用户的知识库管理员。用户刚跟 AI 讨论完一个问题；下文 king 均指当前用户本人，最终输出不要使用名字 king。
得到了一个结论。任务:判断这个结论涉及哪些已存在的实体/概念,然后:
- 涉及哪个实体 → 在那个实体页追加一节
- 涉及哪个概念 → 在那个概念页追加一节
- 涉及的实体/概念不在已有列表里但反复出现在结论里 → 可以新建

【已有的实体】
{existing_entities}

【已有的概念】
{existing_concepts}

【这次讨论】
- 问题:{title}
- 结论:{conclusion}
- 框架:{framework}
- 对话片段(最后 1500 字):{chat_tail}

输出 JSON:
{{
  "entities": {{
    "张老师": "- 关于'问题' king 想清楚了:结论 X\\n- 这个判断会影响接下来的 Y"
  }},
  "concepts": {{
    "AI 项目": "- 决策:...(从这次讨论得出)\\n- 理由:..."
  }},
  "log_entry": "## [YYYY-MM-DD] discuss | 想清楚了 X (结论:Y)"
}}

【规则】
- 只涉及 1-3 个实体/概念是常态,不要硬塞
- 写的内容是 **追加** 给已有页面,不重复老内容
- 实体/概念在已有列表里没找到完美匹配,但结论确实关于某个东西 → 可以新建一个
- 结论一定要原汁原味写进去,不要改写

只输出 JSON。
"""


def update_from_discuss(question: dict, conclusion: str, framework: str,
                        messages: list[dict]) -> dict:
    """DiscussDialog 点「✓ 想清楚了」后调。

    把结论 sync 到对应实体/概念页,并加一条 log。
    返回 {entities: int, concepts: int}
    """
    wiki_root = _wiki_root()
    if wiki_root is None:
        return {"entities": 0, "concepts": 0}

    existing_e = sorted((wiki_root / "entities").glob("*.md"))
    existing_c = sorted((wiki_root / "concepts").glob("*.md"))

    # 对话最后部分作为上下文给 LLM
    chat_text = ""
    for m in messages[-6:]:
        if m["role"] == "system":
            continue
        chat_text += f"\n[{m['role']}] {m['content'][:300]}"
    chat_tail = chat_text[-1500:]

    prompt = DISCUSS_TAG_PROMPT.format(
        existing_entities=", ".join(e.stem for e in existing_e) or "(无)",
        existing_concepts=", ".join(c.stem for c in existing_c) or "(无)",
        title=question.get("title", ""),
        conclusion=conclusion,
        framework=framework,
        chat_tail=chat_tail,
    )

    try:
        result = _call_llm(prompt, "请按上述规则输出 JSON", max_tokens=2000)
    except Exception as e:
        log.error("[wiki/discuss] LLM 失败: %s", e)
        return {"entities": 0, "concepts": 0}

    day = dt.date.today()
    entities = result.get("entities") or {}
    for name, content in entities.items():
        p = _entity_path(name)
        if p and content:
            _append_to_page(p, day, content, section_tag="discuss")

    concepts = result.get("concepts") or {}
    for name, content in concepts.items():
        p = _concept_path(name)
        if p and content:
            _append_to_page(p, day, content, section_tag="discuss")

    log_entry = result.get("log_entry", "")
    if log_entry:
        _append_log(wiki_root, day, log_entry)

    # 刷 index
    _refresh_index(wiki_root, day)

    log.info("[wiki/discuss] sync 完成:%d 实体 / %d 概念",
             len(entities), len(concepts))
    return {"entities": len(entities), "concepts": len(concepts)}


# ============================================================
# CLI:python -m wiki_appender [daily / rebuild-index]
# ============================================================
def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("cmd", choices=["daily", "rebuild-index", "status", "dry-run"])
    parser.add_argument("--date", help="YYYY-MM-DD,默认今天")
    args = parser.parse_args()

    day = dt.date.fromisoformat(args.date) if args.date else dt.date.today()

    if args.cmd == "daily":
        from daily_summary import load_segments, build_fulltext
        segments = load_segments(day)
        if not segments:
            print(f"[X] {day} 没有转写段")
            return
        ft = build_fulltext(segments)
        print(f"[载入] {len(segments)} 段 · {len(ft)} 字 · {day}")
        r = update_from_daily(day, ft)
        print(f"[OK] entities {r['entities']} / concepts {r['concepts']} / log {r['log_added']}")
    elif args.cmd == "rebuild-index":
        wiki_root = _wiki_root()
        if wiki_root is None:
            print("[X] vault 未配置")
            return
        _refresh_index(wiki_root, day)
        print(f"[OK] index 已刷新 → {wiki_root / 'index.md'}")
    elif args.cmd == "status":
        wiki_root = _wiki_root()
        if wiki_root is None:
            print("[X] vault 未配置")
            return
        e = list((wiki_root / "entities").glob("*.md"))
        c = list((wiki_root / "concepts").glob("*.md"))
        print(f"entities:{len(e)}")
        for p in sorted(e):
            print(f"  - {p.stem}")
        print(f"\nconcepts:{len(c)}")
        for p in sorted(c):
            print(f"  - {p.stem}")
    elif args.cmd == "dry-run":
        from daily_summary import load_segments, build_fulltext
        segments = load_segments(day)
        if not segments:
            print(f"[X] {day} 没有转写段")
            return
        ft = build_fulltext(segments)
        own, ext = _split_fulltext_by_source(ft)
        own_lines = [l for l in own.splitlines() if l.strip()]
        ext_lines = [l for l in ext.splitlines() if l.strip()]
        print(f"=== {day} 分流预览（不调 LLM、不写文件）===")
        print(f"本人段 -> 方法论 : {len(own_lines)} 行 / {len(own)} 字")
        print(f"外部段 -> 子弹库 : {len(ext_lines)} 行 / {len(ext)} 字（这些绝不进方法论）")
        print("\n--- 外部段样例（最多 8 行）---")
        for l in ext_lines[:8]:
            print("  " + l[:100])
        print("\n--- 本人段样例（最多 5 行）---")
        for l in own_lines[:5]:
            print("  " + l[:100])


if __name__ == "__main__":
    main()
