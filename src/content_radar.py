"""content_radar 选题雷达 — 从每天的素材里挖内容选题候选。

两个入口:
- extract_from_day(day)   单日素材 → 短视频选题候选(接到 daily_summary 自动跑)
- aggregate_themes(days)  跨周聚合 → 公众号选题候选(Phase 2)

素材来源:
- notes/<day>.md 日报(已提炼的金句/概述/主题)
- transcripts/<day>.jsonl 本人口播原文

模型:内容生产优先用 Claude(model_anthropic),没配 ANTHROPIC_API_KEY 自动退 DeepSeek。
产出入 content_ideas 选题池。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import CONFIG, RESOURCE_ROOT, ROOT, configured_owner_name, setup_logger, note_path, transcript_path, read_jsonl
from onboarding_profile import load_user_profile
import content_ideas

log = setup_logger("content-radar")

PROMPTS_DIR = RESOURCE_ROOT / "prompts" / "content"
STATE_PATH = ROOT / "runtime" / "content_radar_state.json"


# ============================================================
# prompt 加载
# ============================================================
def _load_prompt(name: str) -> str:
    p = PROMPTS_DIR / f"{name}.md"
    if not p.exists():
        raise FileNotFoundError(f"prompt 不存在: {p}")
    return p.read_text(encoding="utf-8").strip()


# ============================================================
# LLM 调用 — 内容生产优先 Claude,fallback DeepSeek
# ============================================================
def _strip_code_fence(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        lines = lines[1:-1] if lines[-1].strip().startswith("```") else lines[1:]
        raw = "\n".join(lines).strip()
    # 鲁棒兜底:若仍夹带前言/解释,抓第一个 { 到最后一个 } 之间的 JSON 对象
    if not raw.startswith("{"):
        s, e = raw.find("{"), raw.rfind("}")
        if s != -1 and e != -1 and e > s:
            raw = raw[s:e + 1]
    return raw


def _ensure_env():
    """Windows:用注册表 HKCU\\Environment(用户 setx 的权威值)覆盖继承的环境变量。
    必须「覆盖」而非「仅补空」——父进程(如 Claude Code 自身)会向子进程注入它自己的
    ANTHROPIC_BASE_URL=api.anthropic.com,污染内容生产配置导致打错 endpoint。
    生产环境(用户双击 launcher.pyw)无父进程注入,无副作用。"""
    if sys.platform != "win32":
        return
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
                         "DEEPSEEK_API_KEY", "HTTPS_PROXY"):
                try:
                    val, _ = winreg.QueryValueEx(k, name)
                    if val:
                        os.environ[name] = str(val)
                except FileNotFoundError:
                    pass
    except Exception:
        pass


def resolve_provider() -> str:
    """内容生产用哪个 provider。优先 content.provider(默认 anthropic),
    anthropic 没 key 自动退 deepseek。"""
    _ensure_env()
    content_cfg = CONFIG.get("content", {})
    provider = content_cfg.get("provider", "anthropic")
    if provider == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
        log.warning("[radar] 未配 ANTHROPIC_API_KEY,内容生产退回 DeepSeek(质量打折,"
                    "建议 setx ANTHROPIC_API_KEY 后用 Claude)")
        provider = "deepseek"
    return provider


def chat(system: str, messages: list, max_tokens: int = 2000,
         temperature: float = 0.6, prefill: str | None = None,
         force_provider: str | None = None,
         timeout_sec: int | None = None) -> str:
    """多轮内容生产 LLM 调用。messages=[{role:user/assistant, content}]。
    anthropic 走 httpx 直接 POST(兼容第三方中转 + 系统代理,绕过 SDK 不认代理的坑);
    deepseek 走 openai SDK。
    prefill: 预填 assistant 开头续写(注:部分中转不支持,会把 prefill 硬拼在对话前)。
    force_provider: 强制指定 provider。radar 的 JSON 提取强制走 deepseek,
    绕开「coding agent 套壳、不遵循自定义格式」的中转。"""
    _ensure_env()
    cfg = CONFIG["summary"]
    provider = force_provider or resolve_provider()
    timeout_sec = timeout_sec or 120

    def _join(prefill, text):
        if not prefill:
            return text
        return text if text.startswith(prefill) else prefill + text

    if provider == "anthropic":
        import httpx
        base = os.environ.get("ANTHROPIC_BASE_URL",
                              "https://api.anthropic.com").rstrip("/")
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        client = httpx.Client(proxy=proxy, timeout=timeout_sec) if proxy else httpx.Client(timeout=timeout_sec)
        msgs = list(messages)
        if prefill:
            msgs = msgs + [{"role": "assistant", "content": prefill}]
        try:
            r = client.post(
                f"{base}/v1/messages",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": cfg["model_anthropic"], "max_tokens": max_tokens,
                      "temperature": temperature, "system": system,
                      "messages": msgs},
            )
            r.raise_for_status()
            data = r.json()
            text = "".join(b.get("text", "") for b in data.get("content", [])
                           if b.get("type") == "text").strip()
            return _join(prefill, text)
        finally:
            client.close()
    else:
        # openai 兼容分支:gpt(经中转的 gpt-5.5,key 复用 ANTHROPIC_API_KEY) 或 deepseek(官方)
        from ai_gateway import OpenAI, provider_api_key
        content_cfg = CONFIG.get("content", {})
        if provider == "gpt":
            base_url = content_cfg.get("gpt_base_url", "https://api.shqbb.com/v1")
            model = content_cfg.get("gpt_model", "gpt-5.5")
            api_key = provider_api_key("ANTHROPIC_API_KEY")  # 中转统一 key
            if not api_key:
                raise RuntimeError("gpt provider 需要 ANTHROPIC_API_KEY(中转统一 key)")
        else:
            base_url = cfg["base_url_deepseek"]
            model = cfg["model_deepseek"]
            api_key = provider_api_key("DEEPSEEK_API_KEY")
            if not api_key:
                raise RuntimeError("DEEPSEEK_API_KEY 未设置")
        # timeout 必设:中转挂起时没有超时会永远卡死(贴链接的「AI 拆解爆款结构」卡死就是这个)
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout_sec, max_retries=1)
        msgs = [{"role": "system", "content": system}] + messages
        if prefill:
            msgs.append({"role": "assistant", "content": prefill})
        resp = client.chat.completions.create(
            model=model, messages=msgs,
            max_tokens=max_tokens, temperature=temperature,
        )
        return _join(prefill, (resp.choices[0].message.content or "").strip())


_ORIGINAL_CONTENT_CHAT = chat


def _is_model_channel_error(exc: Exception) -> bool:
    text = str(exc).lower()
    needles = (
        "model_not_found",
        "no available channel",
        "503",
        "insufficient_quota",
        "rate_limit",
    )
    return any(needle in text for needle in needles)


def chat(system: str, messages: list, max_tokens: int = 2000,
         temperature: float = 0.6, prefill: str | None = None,
         force_provider: str | None = None,
         timeout_sec: int | None = None) -> str:
    provider = force_provider or resolve_provider()
    try:
        return _ORIGINAL_CONTENT_CHAT(
            system, messages, max_tokens, temperature,
            prefill=prefill, force_provider=force_provider, timeout_sec=timeout_sec,
        )
    except Exception as exc:
        if provider != "gpt" or not _is_model_channel_error(exc):
            raise
        log.warning(
            "[content] gpt relay unavailable, fallback to deepseek: %s",
            exc,
        )
        return _ORIGINAL_CONTENT_CHAT(
            system, messages, max_tokens, temperature,
            prefill=prefill, force_provider="deepseek", timeout_sec=timeout_sec,
        )


def call_llm(system: str, user: str, max_tokens: int = 2000,
             temperature: float = 0.6, prefill: str | None = None,
             force_provider: str | None = None) -> str:
    """单轮包装。"""
    return chat(system, [{"role": "user", "content": user}], max_tokens, temperature,
                prefill=prefill, force_provider=force_provider)


def call_json_ideas(system: str, user: str, *, max_tokens: int,
                    force_provider: str, label: str) -> dict:
    """生成并解析 ideas JSON；格式被截断时自动用低温紧凑模式重试一次。"""
    attempts = [
        (user, 0.5),
        (
            user
            + "\n\n【格式重试】上一次回答不是完整合法的 JSON。"
            + "请重新生成，使用紧凑单行 JSON；每个字段控制在必要长度，"
            + "不要 Markdown、解释、前后缀或省略号。",
            0.1,
        ),
    ]
    last_error: Exception | None = None
    for attempt, (prompt, temperature) in enumerate(attempts, start=1):
        raw = call_llm(
            system,
            prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            force_provider=force_provider,
        )
        try:
            data = json.loads(_strip_code_fence(raw))
            if not isinstance(data, dict) or not isinstance(data.get("ideas"), list):
                raise ValueError("顶层必须是包含 ideas 数组的 JSON 对象")
            return data
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = exc
            if attempt == 1:
                log.warning("[%s] JSON 不完整，自动以紧凑模式重试: %s", label, exc)
    raise RuntimeError(f"{label}返回格式错误，自动重试后仍未得到完整 JSON") from last_error


def studio_chat(system: str, messages: list, max_tokens: int = 2500,
                temperature: float = 0.6) -> str:
    """内容创作工坊专用 chat。provider 取 content.studio_provider(默认 deepseek 安全兜底)。
    工坊和雷达解耦,各自可配,将来换模型只改 config。"""
    content_cfg = CONFIG.get("content", {})
    sp = content_cfg.get("studio_provider", "deepseek")
    # 公众号全文/质检会输出很长，120s 容易在中转站超时；工坊单独给更长等待时间。
    timeout_sec = int(content_cfg.get("studio_timeout_sec", 600))
    return chat(system, messages, max_tokens, temperature, force_provider=sp,
                timeout_sec=timeout_sec)


# ============================================================
# 状态文件:防止同一天重复 extract
# ============================================================
def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {"extracted": {}}
    try:
        d = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        d.setdefault("extracted", {})
        return d
    except Exception:
        return {"extracted": {}}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _mark_extracted(day_str: str, n_added: int) -> None:
    state = _load_state()
    state["extracted"][day_str] = {
        "at": dt.datetime.now().isoformat(timespec="seconds"),
        "added": n_added,
    }
    _save_state(state)


def _already_extracted(day_str: str) -> bool:
    return day_str in _load_state()["extracted"]


# ============================================================
# 素材采集
# ============================================================
def _gather_day_material(day: dt.date) -> str:
    """凑齐单日素材:日报(已提炼) + 本人口播原文。"""
    parts = []

    np = note_path(day)
    if np and np.exists():
        md = np.read_text(encoding="utf-8")
        # 日报本身已含金句/概述/主题,直接给(去掉末尾"原文"大段避免太长)
        cut = md.split("## 原文")[0] if "## 原文" in md else md
        parts.append("【今日日报 · 已提炼的金句/活动/主题】\n" + cut.strip())

    # 本人口播原文(king 段落)
    try:
        recs = list(read_jsonl(transcript_path(day)))
        king_lines = [
            (r.get("text") or "").strip()
            for r in recs
            if str(r.get("speaker_name") or "").casefold()
            in {configured_owner_name().casefold(), "我", "king"} and r.get("text")
            and r.get("source") not in ("douyin", "bilibili", "wechat")
        ]
        if king_lines:
            blob = "\n".join(king_lines)
            if len(blob) > 9000:
                blob = blob[:9000] + "\n...(原文过长已截断)"
            parts.append("【今日本人口播原文(节选)】\n" + blob)
    except Exception as e:
        log.warning("[radar] 读 transcripts 失败: %s", e)

    return "\n\n".join(parts)


def _shortvideo_preferences() -> dict:
    workflow = (load_user_profile(ROOT).get("workflow_preferences") or {})
    raw = workflow.get("content") or {}
    types = [str(item) for item in (raw.get("types") or [])]
    try:
        limit = max(1, min(20, int(raw.get("max_items_per_run", 3))))
    except (TypeError, ValueError):
        limit = 3
    return {
        "enabled": raw.get("enabled", True) is not False and (not types or "短视频口播" in types),
        "max_items_per_run": limit,
        "priority_topics": [str(item) for item in (raw.get("priority_topics") or []) if str(item).strip()],
        "excluded_topics": [str(item) for item in (raw.get("excluded_topics") or []) if str(item).strip()],
    }


# ============================================================
# 主入口 1:单日 → 短视频选题
# ============================================================
def extract_from_day(day: dt.date, force: bool = False) -> dict:
    """从单日素材挖短视频选题候选,入 content_ideas 池。
    返回 {added, skipped} 或 {skipped_reason}。"""
    day_str = day.isoformat()
    prefs = _shortvideo_preferences()
    if not prefs["enabled"]:
        return {"skipped_reason": "shortvideo_disabled"}
    if not force and _already_extracted(day_str):
        log.info("[radar] %s 已 extract 过,跳过(force 可覆盖)", day_str)
        return {"skipped_reason": "already_extracted"}

    material = _gather_day_material(day)
    if len(material) < 100:
        log.info("[radar] %s 素材太短,跳过", day_str)
        _mark_extracted(day_str, 0)
        return {"skipped_reason": "material_too_short"}

    log.info("[radar] %s 挖短视频选题(provider=%s)...", day_str,
             CONFIG.get("content", {}).get("radar_provider", "deepseek"))
    try:
        system = _load_prompt("radar_shortvideo")
        limit = prefs["max_items_per_run"]
        tmpl = ('{"ideas":[{"title":"选题标题(带钩子)","hook":"为什么值得继续发展",'
                '"source_quotes":["本人的原话1","原话2"],"angle_seed":"成熟度：可直接做/待补素材；初步角度"}]}')
        preference_lines = []
        if prefs["priority_topics"]:
            preference_lines.append("优先主题：" + "、".join(prefs["priority_topics"]))
        if prefs["excluded_topics"]:
            preference_lines.append("排除主题：" + "、".join(prefs["excluded_topics"]))
        preference_text = "\n".join(preference_lines)
        user_msg = (
            material
            + "\n\n════════ 素材结束,以下是任务 ════════\n"
            + f"从上面素材里宽松收集 0-{limit} 个可继续发展的短视频候选。成熟选题和『值得发展但还需补素材』的候选都可以收；"
            + "只从本人说的话里挑，别拿待办、日程、纯转述当选题，也不要编造事实。\n"
            + (preference_text + "\n" if preference_text else "")
            + "【输出格式·必须严格遵守】只输出一个 JSON 对象,顶层是 ideas 数组,"
            + "每个元素的字段名必须和下面模板完全一致:\n" + tmpl)
        rp = CONFIG.get("content", {}).get("radar_provider", "deepseek")
        output_tokens = max(3000, min(8000, limit * 350))
        data = call_json_ideas(
            system,
            user_msg,
            max_tokens=output_tokens,
            force_provider=rp,
            label="短视频选题",
        )
    except Exception as e:
        log.error("[radar] LLM/解析失败: %s", e)
        return {"error": str(e)}

    ideas = (data.get("ideas") or [])[:prefs["max_items_per_run"]]
    for i in ideas:
        i["format"] = "shortvideo"
    r = content_ideas.add_ideas(ideas, source_date=day_str)
    _mark_extracted(day_str, r["added"])
    log.info("[radar] %s 短视频选题:新增 %d / 跳过 %d", day_str, r["added"], r["skipped"])
    return r


# ============================================================
# 主入口 2:跨周聚合 → 公众号长文选题
# ============================================================
def _gather_window_material(lookback_days: int) -> str:
    """凑齐跨天素材:concepts 档案(反复思考的主题) + 多天日报金句/主题 + 多天本人口播采样。"""
    parts = []
    today = dt.date.today()
    days = [today - dt.timedelta(days=i) for i in range(lookback_days)]

    # 1. concepts 档案 — 反复思考的话题/项目,最重要的长文主题信号
    try:
        import wiki_appender
        root = wiki_appender._wiki_root()
        if root:
            cfiles = sorted((root / "concepts").glob("*.md"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
            if cfiles:
                cparts = []
                for p in cfiles[:12]:
                    txt = p.read_text(encoding="utf-8").strip()
                    if len(txt) > 800:
                        txt = txt[:800] + "…"
                    cparts.append(f"### {p.stem}\n{txt}")
                parts.append("【反复思考的话题/项目 concepts(跨天累积,最重要的长文主题信号)】\n"
                             + "\n\n".join(cparts))
    except Exception as e:
        log.warning("[radar] 读 concepts 失败: %s", e)

    # 2. 多天日报 — 已提炼的金句/活动/主题
    note_parts = []
    for d in days:
        np = note_path(d)
        if np and np.exists():
            md = np.read_text(encoding="utf-8")
            cut = (md.split("## 原文")[0] if "## 原文" in md else md).strip()
            if cut:
                note_parts.append(f"### {d.isoformat()}\n{cut}")
    if note_parts:
        blob = "\n\n".join(note_parts)
        if len(blob) > 8000:
            blob = blob[:8000] + "\n…(日报过长已截断)"
        parts.append("【最近若干天的日报 · 已提炼金句/活动/主题】\n" + blob)

    # 3. 多天本人口播采样
    quote_parts = []
    for d in days:
        try:
            recs = list(read_jsonl(transcript_path(d)))
        except Exception:
            continue
        king_lines = [
            (r.get("text") or "").strip()
            for r in recs
            if str(r.get("speaker_name") or "").casefold()
            in {configured_owner_name().casefold(), "我", "king"} and r.get("text")
            and r.get("source") not in ("douyin", "bilibili", "wechat")
        ]
        if king_lines:
            day_blob = "\n".join(king_lines)
            if len(day_blob) > 1500:
                day_blob = day_blob[:1500] + "…"
            quote_parts.append(f"### {d.isoformat()}\n{day_blob}")
    if quote_parts:
        blob = "\n\n".join(quote_parts)
        if len(blob) > 7000:
            blob = blob[:7000] + "\n…(口播过长已截断)"
        parts.append("【最近若干天的本人口播节选】\n" + blob)

    return "\n\n".join(parts)


def aggregate_themes(lookback_days: int | None = None, force: bool = False) -> dict:
    """跨周聚合主题 → 公众号长文选题候选,入 content_ideas 池。
    防重:距上次聚合不足 (lookback_days//2) 天则跳过(force 覆盖)。
    返回 {added, skipped} 或 {skipped_reason}。"""
    if lookback_days is None:
        lookback_days = CONFIG.get("content", {}).get("aggregate_lookback_days", 7)
    lookback_days = max(2, int(lookback_days))

    state = _load_state()
    agg = state.setdefault("aggregated", {})
    if not force:
        last = agg.get("last_run")
        if last:
            try:
                gap = (dt.date.today() - dt.date.fromisoformat(last)).days
                if gap < max(1, lookback_days // 2):
                    log.info("[radar] 距上次聚合仅 %d 天,跳过(force 可覆盖)", gap)
                    return {"skipped_reason": "too_soon", "last_run": last}
            except Exception:
                pass

    material = _gather_window_material(lookback_days)
    if len(material) < 300:
        log.info("[radar] 跨 %d 天素材太短,跳过聚合", lookback_days)
        return {"skipped_reason": "material_too_short"}

    log.info("[radar] 跨 %d 天聚合公众号选题(provider=%s,素材 %d 字)...",
             lookback_days, CONFIG.get("content", {}).get("radar_provider", "deepseek"),
             len(material))
    try:
        system = _load_prompt("radar_article")
        tmpl = ('{"ideas":[{"title":"选题标题","hook":"为什么值得写长文","prototype":"调查实验型/'
                '产品体验型/现象解读型/工具分享型/方法论分享型 里选一个","theme":"主题内核一句话",'
                '"source_quotes":["他的原话1","原话2"],"source_concepts":[],"why_now":"为什么现在写"}]}')
        user_msg = (
            material
            + "\n\n════════ 素材结束,以下是任务 ════════\n"
            "从上面素材里筛出 0-3 个真正值得写公众号长文的选题。剥开事务的壳,看到主题的核；不够好就返回空数组。\n"
            "【输出格式·必须严格遵守】只输出一个 JSON 对象,顶层是 ideas 数组,"
            "每个元素的字段名必须和下面模板完全一致(不许发明 urgent_items 等别的字段):\n" + tmpl)
        rp = CONFIG.get("content", {}).get("radar_provider", "deepseek")
        data = call_json_ideas(
            system,
            user_msg,
            max_tokens=4000,
            force_provider=rp,
            label="公众号选题",
        )
    except Exception as e:
        log.error("[radar] 聚合 LLM/解析失败: %s", e)
        return {"error": str(e)}

    ideas = data.get("ideas") or []
    for i in ideas:
        i["format"] = "article"
    today_str = dt.date.today().isoformat()
    r = content_ideas.add_ideas(ideas, source_date=today_str)

    agg["last_run"] = today_str
    agg["last_added"] = r["added"]
    _save_state(state)
    log.info("[radar] 公众号选题:新增 %d / 跳过 %d", r["added"], r["skipped"])
    return r


# ============================================================
# CLI
# ============================================================
def main():
    import argparse
    parser = argparse.ArgumentParser(description="content_radar 选题雷达")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_day = sub.add_parser("day", help="单日挖短视频选题")
    p_day.add_argument("--date", help="YYYY-MM-DD,默认今天")
    p_day.add_argument("--force", action="store_true")

    p_agg = sub.add_parser("aggregate", help="跨周聚合公众号选题")
    p_agg.add_argument("--days", type=int, default=None, help="回溯天数,默认读 config")
    p_agg.add_argument("--force", action="store_true", help="绕过防重强制聚合")

    sub.add_parser("list", help="列当前选题池")

    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    if args.cmd == "day":
        day = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
        r = extract_from_day(day, force=args.force)
        print(f"[{day}] {r}")
    elif args.cmd == "aggregate":
        r = aggregate_themes(lookback_days=args.days, force=args.force)
        print(r)
    elif args.cmd == "list":
        for i in content_ideas.list_open():
            badge = "📱" if i["format"] == "shortvideo" else "📄"
            print(f"  {badge} {i['title']}")
            print(f"     {i.get('hook', '')[:60]}")


if __name__ == "__main__":
    main()
