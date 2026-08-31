"""URL → 内容抓取 → 追加到当日 jsonl。

支持 4 类来源：
- **抖音**：yt-dlp 抓音频 → funasr 转写（走现有 transcriber 流程）
- **B 站**：yt-dlp 抓 CC 字幕（如果有）→ 直接入 jsonl，不走 ASR
- **微信公众号**：trafilatura 抽正文 → 拆段入 jsonl
- **微信视频号**：SnapAny 官方 API 解析直链 → 本地下载 → funasr 转写

输出格式（追加到 transcripts/<日期>.jsonl）：
    {
        "start": ISO datetime,  # 入库时间，不是视频时间
        "wav": null,            # 文章无音频；视频可能有
        "source": "douyin/bilibili/wechat/wechat_channels",
        "url": "原始 URL",
        "title": "视频/文章标题",
        "duration_sec": 0,
        "text": "正文/字幕一段",
        "speaker_id": null,
        "speaker_name": "外部·抖音博主"  # 标注外部，daily_summary 会归"转述"
    }

独立可跑：
    python -m ingest_url <url>
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import CONFIG, ROOT, setup_logger, transcript_path, day_dir
from runtime_profile import automatic_browser_cookie_access_enabled

# 子进程不弹控制台黑窗(win32):本模块常被 launcher 以无窗口子进程调起,
# 内部再起 yt-dlp/ffmpeg/funasr 等 console 程序时若不加此标志会闪黑框
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

log = setup_logger("ingest-url")

# 进度回调
ProgressCB = Callable[[str, str], None]
NOOP_PROGRESS: ProgressCB = lambda stage, msg: None


# ============================================================
# 来源识别
# ============================================================
def detect_source(url: str) -> str:
    """返回 'douyin' / 'bilibili' / 'wechat' / 'wechat_channels' / 'unknown'。"""
    u = url.lower()
    if "douyin.com" in u or "iesdouyin.com" in u or "v.douyin.com" in u:
        return "douyin"
    if "bilibili.com" in u or "b23.tv" in u:
        return "bilibili"
    if "weixin.qq.com/sph/" in u:
        return "wechat_channels"
    if "mp.weixin.qq.com" in u:
        return "wechat"
    return "unknown"


def _extract_url(text: str) -> str:
    """从分享文本(可能含口令/标题/文案/#标签)里抠出真正的链接。
    用户常整段粘贴抖音/B站的分享文案,而不是纯链接。"""
    import re
    # 抖音短链优先(v.douyin.com/xxxx)
    m = re.search(r"https?://v\.douyin\.com/[\w\-]+", text)
    if m:
        return m.group(0)
    # 其他平台 / 通用 http(s) 链接(到空白或中文标点为止)
    m = re.search(r"https?://[^\s，。、）)】\]》\"']+", text)
    if m:
        return m.group(0)
    return text.strip()  # 没找到链接,原样返回(让 detect_source 报 unknown)


# ============================================================
# 外放回录清除:看视频时麦克风录到的视频声音,从今天 transcripts 清掉
# ============================================================
def _norm_for_match(t: str) -> str:
    """匹配前归一化:去掉空白和所有标点,只留文字。"""
    return re.sub(r"[\s,。，、！？!?.;；:：\"'「」『』()（）【】…·~——\-]+", "", t or "")


def _gram_hit_ratio(seg: str, ext: str, n: int = 6) -> float:
    """seg 的 n 字滑窗在 ext 里的命中率(0~1)。ASR 同源文本命中率接近 1。"""
    if len(seg) < n:
        return 1.0 if seg and seg in ext else 0.0
    total = len(seg) - n + 1
    hits = sum(1 for i in range(total) if seg[i:i + n] in ext)
    return hits / total


def _scrub_recent_transcripts(external_text: str, window_minutes: int = 45) -> int:
    """把「看视频时被麦克风录进今天 transcripts 的外放声音」清掉(实时滚动读这个文件)。
    判定:最近 window_minutes 分钟内 source=live 的段,文本与外部内容高度重合(6字滑窗命中率>=0.55)。
    被删的段先备份到 runtime/scrubbed-<日期>.jsonl(可恢复)。返回删除条数。失败不抛。"""
    try:
        ext = _norm_for_match(external_text)
        if len(ext) < 50:
            return 0
        p = transcript_path()
        if not p.exists():
            return 0
        cutoff = dt.datetime.now() - dt.timedelta(minutes=window_minutes)
        kept, removed = [], []
        for ln in p.read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            drop = False
            try:
                r = json.loads(ln)
                if r.get("source") == "live":
                    ts = dt.datetime.fromisoformat(r.get("start", ""))
                    if ts >= cutoff:
                        seg = _norm_for_match(r.get("text", "") or "")
                        if len(seg) >= 10 and _gram_hit_ratio(seg, ext) >= 0.55:
                            drop = True
            except Exception:
                pass
            (removed if drop else kept).append(ln)
        if not removed:
            return 0
        bak = ROOT / "runtime" / f"scrubbed-{dt.date.today().isoformat()}.jsonl"
        bak.parent.mkdir(parents=True, exist_ok=True)
        with open(bak, "a", encoding="utf-8") as f:
            f.write("\n".join(removed) + "\n")
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        os.replace(tmp, p)
        log.info("[scrub] 清除外放回录 %d 段(备份 %s)", len(removed), bak.name)
        return len(removed)
    except Exception as e:
        log.warning("[scrub] 清除失败(不影响主流程): %s", e)
        return 0


# ============================================================
# 工具：追加到 jsonl
# ============================================================
def _append_jsonl(records: list[dict]) -> int:
    """追加一组记录到今日 jsonl。返回写入条数。"""
    p = transcript_path()
    with open(p, "a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(records)


def _now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


# ============================================================
# 第二大脑「输入档案.md」
# ============================================================
_SOURCE_CN = {
    "douyin": "抖音",
    "bilibili": "B站",
    "wechat": "公众号",
    "wechat_channels": "视频号",
}


def _wiki_input_path() -> Path | None:
    """返回第二大脑/输入档案.md；没连接 Obsidian 时保存到本地 notes。"""
    vault = _configured_obsidian_vault()
    p = (vault if vault else (ROOT / "notes")) / "第二大脑"
    p.mkdir(parents=True, exist_ok=True)
    return p / "输入档案.md"


def _make_wiki_anchor(source: str, title: str, uploader: str) -> str:
    """生成 wiki 一节的稳定 anchor — Obsidian 双链可以直接用 `输入档案#anchor`"""
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    src_cn = _SOURCE_CN.get(source, source)
    safe_title = (title or "").replace("\n", " ").strip()[:30]
    safe_uploader = (uploader or "").strip()[:20]
    parts = [now, src_cn]
    if safe_uploader:
        parts.append(safe_uploader)
    if safe_title:
        parts.append(safe_title)
    return " · ".join(parts)


def _extract_essence(text: str, source: str, title: str, uploader: str) -> dict:
    """调 DeepSeek 提一句核心 + 3 条要点。失败返回空 essence。"""
    if not text or len(text.strip()) < 50:
        return {}
    from ai_gateway import provider_api_key
    api_key = provider_api_key("DEEPSEEK_API_KEY")
    if not api_key:
        log.warning("[wiki] DEEPSEEK_API_KEY 未设置,跳过 essence 提取")
        return {}
    try:
        from ai_gateway import OpenAI
        client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
        src_cn = _SOURCE_CN.get(source, source)
        prompt = (
            "下面是用户刚刚收藏的一段外部内容（由用户粘贴链接后获取）。"
            "请你提炼 JSON,字段:\n"
            "- core: 一句 20-40 字的核心观点(他抓这条想记住的本质)\n"
            "- bullets: 3-5 条要点,每条 15-30 字,从原文真实提取\n"
            "- tags: 1-3 个主题标签,从这条内容能反复检索的关键词(中文,不带 #)\n"
            "- worth_revisit: true/false,这条值不值得后面回来再看\n\n"
            f"来源: {src_cn} · {uploader}\n标题: {title}\n\n"
            "正文(可能很长,只看前 3000 字):\n"
            f"{text[:3000]}"
        )
        resp = client.chat.completions.create(
            model="deepseek-v4-flash",
            messages=[
                {"role": "system", "content": "你是用户的知识整理助手。严格输出 JSON，不要任何前后缀。"},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            max_tokens=600,
            temperature=0.2,
        )
        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
        return {}
    except Exception as e:
        log.warning("[wiki] essence 提取失败: %s", e)
        return {}


def _post_ingest_to_wiki(
    *,
    source: str,
    url: str,
    title: str,
    uploader: str,
    full_text: str,
    record_count: int,
    on_progress: ProgressCB,
) -> str | None:
    """把这次抓到的内容追加到「第二大脑/输入档案.md」一节。返回 anchor。

    失败不抛(主流程已经入了 jsonl),只记日志。
    """
    p = _wiki_input_path()
    if p is None:
        return None

    on_progress("wiki", "提炼核心要点,写入第二大脑...")
    essence = _extract_essence(full_text, source, title, uploader)
    anchor = _make_wiki_anchor(source, title, uploader)
    src_cn = _SOURCE_CN.get(source, source)

    # 拼一节
    core = essence.get("core", "") or "(未生成 — DEEPSEEK_API_KEY 未配或调用失败)"
    bullets = essence.get("bullets") or []
    tags = essence.get("tags") or []
    worth = essence.get("worth_revisit")

    lines = [f"\n## {anchor}\n",
             f"> 原链接:<{url}>",
             f"> 入库时间:{_now_iso()} · 共 {record_count} 段文字",
             ""]
    if worth is True:
        lines.append("> ⭐ 标记值得回看")
        lines.append("")
    lines += ["### 一句核心", "", core, ""]
    if bullets:
        lines += ["### 要点", ""]
        for b in bullets:
            lines.append(f"- {b}")
        lines.append("")
    if tags:
        tag_str = " ".join(f"#{t}" for t in tags)
        lines += ["### 标签", "", tag_str, ""]
    if full_text.strip():
        quoted = full_text.strip().replace("\n", "\n> ")
        lines += [
            "### 完整原文",
            "",
            "> [!quote]- 点开看全文",
            f"> {quoted}",
            "",
        ]
    lines += [
        "### 存档",
        "",
        f"- 来源:{src_cn} · {uploader}",
        f"- 共 {record_count} 段 · 仅存于本档案,不进实时滚动",
        "",
        "---",
    ]
    section = "\n".join(lines) + "\n"

    try:
        if not p.exists():
            head = (
                "# 输入档案\n\n"
                "> 这里是用户在语音日记系统里粘贴链接后获取的外部内容。\n"
                "> 每节一条 — 抖音/B 站/视频号/公众号都会进。AI 提的「一句核心 + 要点 + 标签」让以后能反向检索。\n"
                "> 每节含 AI 提炼的核心要点 + 完整原文(折叠)。外部内容只存这里,不进实时滚动。\n\n"
                "---\n"
            )
            p.write_text(head + section, encoding="utf-8")
        else:
            with open(p, "a", encoding="utf-8") as f:
                f.write(section)
        log.info("[wiki] 已追加输入档案一节:%s", anchor)
        return anchor
    except Exception as e:
        log.warning("[wiki] 写输入档案失败: %s", e)
        return None


# ============================================================
# 爆款分析:贴链接 → 选题角度/钩子/结构/可借鉴/仿写模板
# 存到「会议助手同款」爆款分析文件夹,给二创用。用 GPT-5.5(env key,绝不硬编码)。
# ============================================================
_BAOKUAN_SYSTEM = ("你是一位资深短视频/公众号爆款分析专家,擅长拆解爆款内容的选题、钩子、"
                   "结构,并给出可直接复用的仿写模板。输出简洁有力,不说废话。")


def _baokuan_folder():
    """爆款分析输出文件夹:优先 config [ingest].baokuan_folder,
    否则放 Obsidian vault 下的「爆款分析」；未连接 Obsidian 时保存到本地 notes。"""
    cfg = CONFIG.get("ingest", {}).get("baokuan_folder", "")
    if cfg:
        return Path(cfg)
    vault = _configured_obsidian_vault()
    if vault:
        return vault / "爆款分析"
    return ROOT / "notes" / "爆款分析"


def _configured_obsidian_vault() -> Path | None:
    """读取本机 Obsidian 仓库覆盖值，再回退到发行配置。

    开源配置不能硬编码开发者的私人目录，因此外部仓库路径保存在用户
    数据目录的 ``runtime/local-paths.json`` 中。
    """
    local_paths = ROOT / "runtime" / "local-paths.json"
    try:
        value = json.loads(local_paths.read_text(encoding="utf-8"))
        configured = str(value.get("obsidian_vault") or "").strip()
        if configured:
            return Path(configured).expanduser()
    except (OSError, TypeError, ValueError):
        pass
    configured = str(
        CONFIG.get("obsidian", {}).get("vault", "") or ""
    ).strip()
    return Path(configured).expanduser() if configured else None


def _baokuan_prompt(transcript: str, title: str, uploader: str, source: str, meta: dict) -> str:
    src_cn = _SOURCE_CN.get(source, source)
    data_line = ""
    digg = meta.get("digg_count") or meta.get("digg")
    if digg or meta.get("comment_count"):
        data_line = (f"数据:点赞{digg or 0} 评论{meta.get('comment_count', 0)} "
                     f"收藏{meta.get('collect_count', 0)} 转发{meta.get('share_count', 0)}\n")
    comments = meta.get("comments") or []
    comment_block = ""
    comment_section = ""
    data_section = ""
    if digg or meta.get("comment_count"):
        data_section = ("## 数据解读\n\n"
                        "根据点赞/评论/收藏/转发的比例,分析这条内容的传播特征"
                        "(情绪型/干货型/社交货币型),以及哪个数据异常值得注意。\n\n")
    if comments:
        lines = "\n".join(f"{i+1}. (赞{c.get('digg',0)}) {c.get('text','')}"
                          for i, c in enumerate(comments[:15]))
        comment_block = f"\n热门评论(按点赞排序):\n{lines}\n"
        comment_section = ("## 评论区洞察\n\n"
                           "高赞评论暴露了观众的哪些真实痛点/共鸣点/争议点?"
                           "哪几条评论本身就是现成的二创切入角度?\n\n")
    return f"""请分析以下{src_cn}内容,做一份「爆款分析」,用于我的二次创作借鉴。

标题:{title}
作者:{uploader}
{data_line}{comment_block}原文/口播稿:
{transcript}

请严格按以下格式输出(每项 2-3 句,简洁有力):

## 快速标签

- **内容类型**:干货输出 / 故事叙事 / 情绪共鸣 / 争议观点(选一个)
- **钩子类型**:反常识 / 数字对比 / 悬念 / 身份背书 / 痛点直击(选一个)
- **变现路径**:引流私域 / 卖课 / 接广告 / 无变现(选一个)
- **精炼关键词**:用 6-10 个字概括核心内容(用于文件命名)

{data_section}## 选题角度

讲了什么话题?为什么这个话题能火?

## 钩子设计

开头前 3 句怎么抓注意力?用了什么技巧?

## 内容结构

整体框架拆解:分几个部分?每部分讲什么?逻辑线是什么?

## 表达技巧

口语化程度、节奏、情绪调动、金句摘录。

{comment_section}## 可借鉴点

哪些能直接用到我自己的创作里?给 2-3 个具体建议。

## 仿写模板

用同样结构讲一个新话题的填空式模板:
- 开头钩子怎么写(仿照原钩子类型)
- 中间分几段、每段功能是什么
- 结尾怎么收(留悬念 / 引导行动)"""


def _baokuan_tags(analysis: str):
    kw = ""
    m = re.search(r"精炼关键词[^\n]*?[：:]\s*(.+)", analysis)
    if m:
        kw = re.sub(r'[*「」“”<>:"/\\|?#]', "", m.group(1)).strip()[:20]
    ctype = next((t for t in ("干货输出", "故事叙事", "情绪共鸣", "争议观点")
                  if t in analysis[:600]), "")
    htype = next((t for t in ("反常识", "数字对比", "悬念", "身份背书", "痛点直击")
                  if t in analysis[:600]), "")
    return kw, ctype, htype


def _save_baokuan(source: str, url: str, title: str, uploader: str,
                  analysis: str, transcript: str, meta: dict = None) -> str:
    meta = meta or {}
    folder = _baokuan_folder()
    if folder is None:
        raise RuntimeError("未配置 obsidian vault,无法保存爆款分析")
    folder.mkdir(parents=True, exist_ok=True)
    kw, ctype, htype = _baokuan_tags(analysis)
    keyword = kw or title[:20]
    src_cn = _SOURCE_CN.get(source, source)
    date_str = dt.date.today().isoformat()
    safe_author = re.sub(r'[<>:"/\\|?*#]', "", uploader)[:10] or "未知"
    safe_keyword = re.sub(r'[<>:"/\\|?*#]', "", keyword)[:20] or "分析"
    type_label = ctype or "分析"
    filename = f"{date_str}-{safe_author}-{type_label}-{safe_keyword}.md"
    filepath = folder / filename
    tags = ["爆款分析", src_cn]
    if ctype:
        tags.append(ctype)
    if htype:
        tags.append(f"钩子_{htype}")
    digg = meta.get("digg_count", 0)
    stats_fm = ""
    stats_quote = ""
    if digg or meta.get("comment_count"):
        stats_fm = (f"digg: {digg}\n"
                    f"comment: {meta.get('comment_count', 0)}\n"
                    f"collect: {meta.get('collect_count', 0)}\n"
                    f"share: {meta.get('share_count', 0)}\n")
        stats_quote = (f"> 点赞 {digg} | 评论 {meta.get('comment_count', 0)} | "
                       f"收藏 {meta.get('collect_count', 0)} | 转发 {meta.get('share_count', 0)}\n")
    comments = meta.get("comments") or []
    comments_md = ""
    if comments:
        cl = "\n".join(f"- (赞{c.get('digg',0)}) {c.get('text','')}" for c in comments[:15])
        comments_md = f"## 热门评论\n\n{cl}\n\n---\n\n"
    body = (f"---\n"
            f"date: {date_str}\n"
            f"tags: [{', '.join(tags)}]\n"
            f"source: {url}\n"
            f"category: 爆款分析\n"
            f"author: {uploader}\n"
            f"来源: {src_cn}\n"
            f"{stats_fm}"
            f"---\n\n"
            f"# {title}\n\n"
            f"> 作者: **{uploader}** | 来源: {src_cn}\n"
            f"{stats_quote}\n"
            f"{analysis}\n\n"
            f"---\n\n"
            f"{comments_md}"
            f"## 原始内容\n\n"
            f"{transcript}\n\n"
            f"---\n"
            f"*由语音日记 · 贴链接爆款分析自动生成 · {_now_iso()}*\n")
    filepath.write_text(body, encoding="utf-8")
    log.info("[baokuan] 已保存爆款分析: %s", filepath)
    return str(filepath)


def _add_recreate_idea(source: str, url: str, title: str, uploader: str,
                       analysis: str, baokuan_path: str) -> None:
    """爆款分析存好后,把这条自动加进内容选题池(🔁二创)。
    工坊点开它会注入爆款分析全文,产「借结构换内核」的二创角度。失败不抛。"""
    try:
        import content_ideas
        # 查重:同一链接已有 open/drafting 的二创选题就不再加
        for i in content_ideas.list_open():
            if i.get("source_url") == url:
                log.info("[baokuan] 该链接已在选题池,跳过入池")
                return
        _, _, htype = _baokuan_tags(analysis)
        m = re.search(r"##\s*可借鉴点\s*\n+(.*?)(?=\n## |\Z)", analysis, re.DOTALL)
        jiejian = (m.group(1).strip()[:200] if m else "")
        hook = f"[{htype or '爆款'}] {jiejian or '借鉴这条爆款的结构做二创'}"
        r = content_ideas.add_ideas([{
            "title": f"二创 · {title[:40]}",
            "format": "shortvideo",
            "hook": hook,
            "origin": "external",
            "baokuan_path": baokuan_path,
            "source_url": url,
            "source_quotes": [f"来自 {uploader} 的{_SOURCE_CN.get(source, source)}爆款"],
        }], source_date=dt.date.today().isoformat())
        log.info("[baokuan] 二创选题入池: added=%s", r.get("added"))
    except Exception as e:
        log.warning("[baokuan] 二创选题入池失败(不影响): %s", e)


def _post_ingest_baokuan(*, source: str, url: str, title: str, uploader: str,
                         full_text: str, meta: dict = None,
                         on_progress: ProgressCB = NOOP_PROGRESS) -> "str | None":
    """贴链接 → 生成「爆款分析」独立文档(选题/钩子/结构/可借鉴/仿写),
    存到和会议助手同款的「爆款分析」文件夹,给二创用。失败不抛,只记日志。"""
    if not full_text or len(full_text.strip()) < 30:
        return None
    try:
        on_progress("baokuan", "AI 拆解爆款结构(GPT-5.5 生成中,约 1-3 分钟,可关窗后台继续)...")
        import content_radar
        prov = content_radar.CONFIG.get("content", {}).get("provider", "gpt")
        analysis = content_radar.call_llm(
            _BAOKUAN_SYSTEM,
            _baokuan_prompt(full_text, title, uploader, source, meta or {}),
            max_tokens=3000, temperature=0.5, force_provider=prov,
        )
        if not analysis or len(analysis.strip()) < 50:
            return None
        path = _save_baokuan(source, url, title, uploader, analysis, full_text, meta=meta)
        # 爆款分析存好 → 自动加进选题池(🔁二创),贴完链接就能在选题面板点「创作」
        _add_recreate_idea(source, url, title, uploader, analysis, path)
        return path
    except Exception as e:
        log.warning("[baokuan] 爆款分析失败: %s", e)
        return None


# ============================================================
# B 站：抓 CC 字幕（无字幕时回退到音频）
# ============================================================
def _cookie_browsers() -> list[str]:
    """缺 cookies 时从哪些浏览器按序尝试读取。config [ingest].cookies_browsers 可覆盖。"""
    if not automatic_browser_cookie_access_enabled(CONFIG):
        return []
    bs = CONFIG.get("ingest", {}).get("cookies_browsers")
    if bs:
        return bs if isinstance(bs, list) else [bs]
    return ["firefox", "edge", "chrome"]


def _cookies_via_rookiepy(domain: str = ".douyin.com") -> str | None:
    """用 rookiepy 读浏览器 cookies,写成 Netscape cookies.txt,返回路径(读不到返回 None)。
    rookiepy 能解 Chrome/Edge 127+ 的 App-Bound 加密——这是 yt-dlp --cookies-from-browser
    做不到的。所以抖音这种要 cookies 的站,只要用 Edge/Chrome 访问过该站,就能自动抓,
    不用装 Firefox 或手动导出 cookies.txt。"""
    if not automatic_browser_cookie_access_enabled(CONFIG):
        log.info("[cookies] 自动读取浏览器登录态已关闭")
        return None
    try:
        import rookiepy
    except ImportError:
        return None
    raw = None
    for fn_name in ("edge", "chrome", "brave", "firefox"):
        fn = getattr(rookiepy, fn_name, None)
        if not fn:
            continue
        try:
            cookies = fn(domains=[domain])
            if cookies:
                raw = cookies
                log.info("[cookies] rookiepy 从 %s 读到 %d 个 %s cookies",
                         fn_name, len(cookies), domain)
                break
        except Exception as e:
            log.debug("[cookies] rookiepy %s 读取失败: %s", fn_name, e)
    if not raw:
        return None
    out = ROOT / "runtime" / "cookies_auto.txt"   # runtime/ 已 gitignore,含登录态不外泄
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Netscape HTTP Cookie File", ""]
    for c in raw:
        dom = c.get("domain") or domain
        flag = "TRUE" if dom.startswith(".") else "FALSE"
        path = c.get("path") or "/"
        secure = "TRUE" if c.get("secure") else "FALSE"
        try:
            expires = str(int(c.get("expires") or 0))
        except (TypeError, ValueError):
            expires = "0"
        name = c.get("name") or ""
        value = c.get("value") or ""
        if name:
            lines.append(f"{dom}\t{flag}\t{path}\t{secure}\t{expires}\t{name}\t{value}")
    try:
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(out)
    except Exception as e:
        log.warning("[cookies] 写 cookies.txt 失败: %s", e)
        return None


def _run_yt_dlp(args: list[str], timeout: int = 300) -> tuple[int, str, str]:
    """执行 yt-dlp，返回 (returncode, stdout, stderr)。
    抖音等平台报缺 cookies 时,自动按浏览器顺序加 --cookies-from-browser 重试。"""
    # 优先用 venv 里装的，回退到 PATH
    yt = shutil_which_yt_dlp()

    def _exec(extra: list[str]) -> tuple[int, str, str]:
        cmd = [yt] + extra + args
        log.info("[yt-dlp] %s", " ".join(extra + args))
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace", creationflags=_NO_WINDOW,
            )
            return r.returncode, r.stdout, r.stderr
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"yt-dlp 超时（{timeout}s）")

    ing = CONFIG.get("ingest", {})
    cookies_file = (ing.get("cookies_file") or "").strip()
    use_file = bool(cookies_file and Path(cookies_file).exists())

    # 第一次:有手动 cookies.txt 就带上,否则裸跑(B 站等不需要 cookies)
    rc, out, err = _exec(["--cookies", cookies_file] if use_file else [])
    if rc == 0:
        return rc, out, err

    # 缺 cookies(抖音 / 部分需登录的 B 站),且没配手动 cookies.txt
    low = (err or "").lower()
    if any(k in low for k in ("cookies", "sign in", "log in", "登录")) and not use_file:
        last = (rc, out, err)
        url_arg = next((a for a in args if str(a).startswith("http")), "")
        allow_browser_access = automatic_browser_cookie_access_enabled(CONFIG)
        # ① rookiepy 自动读浏览器 cookies(能解 Edge/Chrome App-Bound 加密,抖音首选)
        if allow_browser_access and ing.get("use_rookiepy", False):
            domain = (".douyin.com" if "douyin.com" in url_arg else
                      ".bilibili.com" if "bilibili.com" in url_arg else None)
            auto = _cookies_via_rookiepy(domain) if domain else None
            if auto:
                log.info("[yt-dlp] rookiepy 读到 cookies,带 cookies 重试...")
                r2 = _exec(["--cookies", auto])
                if r2[0] == 0:
                    return r2
                last = r2
        # ② 兜底:yt-dlp --cookies-from-browser(Firefox 可读;Edge/Chrome 新版常失败)
        for browser in _cookie_browsers():
            log.info("[yt-dlp] 尝试从浏览器 %s 读取 cookies 重试...", browser)
            r3 = _exec(["--cookies-from-browser", browser])
            if r3[0] == 0:
                return r3
            last = r3
        rc, out, err = last
        if allow_browser_access:
            err = (err or "") + (
                "\n\n[提示] 需要浏览器 cookies，已按当前个人配置尝试读取但失败。"
            )
        else:
            err = (err or "") + (
                "\n\n[安全提示] 自动读取浏览器登录态已关闭；商业构建不支持需要登录态的抓取。"
            )
    return rc, out, err


def shutil_which_yt_dlp() -> str:
    """yt-dlp 可执行路径。"""
    import shutil
    # 优先 venv
    venv_yt = ROOT / ".venv" / "Scripts" / "yt-dlp.exe"
    if venv_yt.exists():
        return str(venv_yt)
    found = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
    if found:
        return found
    raise RuntimeError("没找到 yt-dlp，请运行：pip install yt-dlp")


def _bilibili_get_info(url: str) -> dict:
    """yt-dlp 抓视频元信息（标题/时长/字幕列表）。"""
    rc, out, err = _run_yt_dlp(["-J", "--no-warnings", url], timeout=60)
    if rc != 0:
        raise RuntimeError(f"yt-dlp 取信息失败:\n{err[-500:]}")
    return json.loads(out)


def _parse_vtt(vtt_text: str) -> list[dict]:
    """解析 WebVTT 字幕，返回 [{start, end, text}] 列表。"""
    segments = []
    blocks = re.split(r"\n\n+", vtt_text)
    for block in blocks:
        lines = [l.strip() for l in block.split("\n") if l.strip()]
        if not lines:
            continue
        # 找时间戳行 "00:00:01.000 --> 00:00:05.000"
        ts_line = None
        for i, line in enumerate(lines):
            if "-->" in line:
                ts_line = line
                text_lines = lines[i + 1:]
                break
        if not ts_line or not text_lines:
            continue
        # 解析时间戳
        m = re.match(
            r"(\d+:\d+:\d+[.,]\d+)\s*-->\s*(\d+:\d+:\d+[.,]\d+)", ts_line
        )
        if not m:
            continue
        start = _vtt_to_seconds(m.group(1))
        end = _vtt_to_seconds(m.group(2))
        # 清洗字幕文本（去标签）
        text = " ".join(text_lines)
        text = re.sub(r"<[^>]+>", "", text).strip()
        if text:
            segments.append({"start": start, "end": end, "text": text})
    return segments


def _vtt_to_seconds(ts: str) -> float:
    """00:00:01.000 → 1.0 秒。"""
    ts = ts.replace(",", ".")
    parts = ts.split(":")
    h, m, s = parts
    return int(h) * 3600 + int(m) * 60 + float(s)


def ingest_bilibili(url: str, on_progress: ProgressCB = NOOP_PROGRESS) -> dict:
    """B 站：优先抓 CC 字幕，没字幕就抓音频转写。"""
    on_progress("info", "获取视频信息...")
    info = _bilibili_get_info(url)
    title = info.get("title") or "B 站视频"
    duration = info.get("duration") or 0
    uploader = info.get("uploader") or "B 站博主"

    # 检查是否有字幕
    subtitles = info.get("subtitles") or {}
    auto_captions = info.get("automatic_captions") or {}

    # 优先级：手动 zh > auto zh > en
    sub_lang = None
    for lang_key in ("zh-Hans", "zh-CN", "zh", "zh-Hant"):
        if lang_key in subtitles:
            sub_lang = lang_key
            sub_source = "manual"
            break
    if not sub_lang:
        for lang_key in ("zh-Hans", "zh-CN", "zh", "zh-Hant"):
            if lang_key in auto_captions:
                sub_lang = lang_key
                sub_source = "auto"
                break

    segments: list[dict] = []

    if sub_lang:
        on_progress("subtitle", f"找到 {sub_source} 字幕（{sub_lang}），下载中...")
        # 下载字幕
        with tempfile.TemporaryDirectory() as tmpdir:
            args = [
                "--skip-download",
                "--write-sub" if sub_source == "manual" else "--write-auto-sub",
                "--sub-lang", sub_lang,
                "--sub-format", "vtt",
                "-o", str(Path(tmpdir) / "%(id)s.%(ext)s"),
                url,
            ]
            rc, _, err = _run_yt_dlp(args, timeout=180)
            if rc != 0:
                log.warning("字幕下载失败，回退到音频转写: %s", err[-300:])
                sub_lang = None  # 标记失败，走音频路径
            else:
                vtt_files = list(Path(tmpdir).glob("*.vtt"))
                if vtt_files:
                    vtt_text = vtt_files[0].read_text(encoding="utf-8")
                    segments = _parse_vtt(vtt_text)
                    log.info("字幕解析出 %d 段", len(segments))

    if not segments:
        # 字幕路径失败 / 无字幕 → 走音频转写
        on_progress("audio", "无字幕，下载音频用本地 ASR 转写...")
        segments = _download_and_transcribe(url, on_progress)

    # 外部内容只写 obsidian 输入档案,不进 transcripts(=不进实时滚动)
    full_text = " ".join(seg.get("text", "") for seg in segments).strip()
    # 看视频时被麦克风录进实时滚动的外放声音 → 清掉
    n_scrub = _scrub_recent_transcripts(full_text)
    if n_scrub:
        on_progress("info", f"已从实时滚动清除外放回录 {n_scrub} 段")
    anchor = _post_ingest_to_wiki(
        source="bilibili", url=url, title=title, uploader=uploader,
        full_text=full_text, record_count=len(segments),
        on_progress=on_progress,
    )
    bk = _post_ingest_baokuan(source="bilibili", url=url, title=title, uploader=uploader,
                              full_text=full_text, on_progress=on_progress)
    tail = "输入档案 + 爆款分析✓" if bk else "已存入输入档案"
    on_progress("done", f"B 站 · {title} · {tail}(不进实时滚动)")
    return {"source": "bilibili", "title": title, "count": len(segments), "url": url,
            "wiki_anchor": anchor, "baokuan_path": bk}


# ============================================================
# 抖音：公开分享页取视频信息 → 下载 → funasr 转写
#
# www.douyin.com 的详情 API 经常要求登录态；公开分享页
# www.iesdouyin.com/share/video/<id>/ 则会在 SSR 数据里提供公开视频信息。
# 默认只走公开分享页，不读取浏览器 Cookie，也不依赖开发机上的外部脚本。
# ============================================================
def _douyin_downloader_path() -> str:
    return CONFIG.get("ingest", {}).get(
        "douyin_downloader_path", "")


def _read_douyin_cookies() -> dict:
    """rookiepy 读浏览器抖音 cookies(dict)。能解 Edge/Chrome App-Bound 加密。"""
    if not automatic_browser_cookie_access_enabled(CONFIG):
        raise RuntimeError(
            "自动读取浏览器登录态已关闭；默认不支持需要登录态的抖音抓取"
        )
    try:
        import rookiepy
    except ImportError:
        raise RuntimeError("缺 rookiepy,请: pip install rookiepy")
    for fn_name in ("edge", "chrome", "brave"):
        fn = getattr(rookiepy, fn_name, None)
        if not fn:
            continue
        try:
            raw = fn(domains=[".douyin.com"])
            cookies = {c["name"]: c["value"] for c in raw}
            if cookies:
                log.info("[douyin] rookiepy 从 %s 读到 %d cookies", fn_name, len(cookies))
                return cookies
        except Exception as e:
            log.debug("[douyin] rookiepy %s 读取失败: %s", fn_name, e)
    raise RuntimeError("无法从浏览器读取抖音 cookies,请先用 Edge/Chrome 访问过抖音网页")


async def _douyin_download_async(url: str, ext_dir: str) -> tuple[str, dict]:
    """从抖音公开分享页下载视频，返回 ``(mp4 路径, meta)``。"""
    import aiohttp

    proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or None
    mobile_ua = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 Mobile/15E148"
    )
    timeout = aiohttp.ClientTimeout(total=60, sock_read=30)
    async with aiohttp.ClientSession(
        timeout=timeout,
        headers={
            "User-Agent": mobile_ua,
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
    ) as session:
        # 解析短链 v.douyin.com → 真实 URL。
        real_url = url
        if "v.douyin.com" in url:
            async with session.get(
                url, allow_redirects=True, proxy=proxy
            ) as resp:
                real_url = str(resp.url)

        m = re.search(r"/video/(\d+)", real_url) or re.search(r"modal_id=(\d+)", real_url)
        if not m:
            raise ValueError(f"无法提取视频 ID: {real_url}")
        aweme_id = m.group(1)

        share_url = f"https://www.iesdouyin.com/share/video/{aweme_id}/"
        async with session.get(share_url, proxy=proxy) as resp:
            if resp.status != 200:
                raise RuntimeError(f"获取抖音公开分享页失败: HTTP {resp.status}")
            share_html = await resp.text()
        detail = _parse_douyin_share_page(share_html, aweme_id)
        if not detail:
            raise RuntimeError(
                "获取抖音公开视频信息失败，可能链接已失效、视频非公开或抖音页面已调整"
            )

        title = detail.get("desc") or aweme_id
        author = (detail.get("author") or {}).get("nickname", "抖音博主")
        duration = (detail.get("video") or {}).get("duration", 0)
        stats = detail.get("statistics") or {}
        meta = {
            "title": title, "author": author, "aweme_id": aweme_id,
            "duration_sec": duration // 1000 if duration else 0, "source_url": url,
            "digg_count": stats.get("digg_count", 0),
            "comment_count": stats.get("comment_count", 0),
            "collect_count": stats.get("collect_count", 0),
            "share_count": stats.get("share_count", 0),
        }
        log.info("[douyin] %s: %s", author, title[:40])

        # 公开分享页不保证提供评论；不为评论读取浏览器登录态。
        meta["comments"] = []

        # 分享页通常给 playwm；同一公开端点的 play 是无水印版本。
        video = detail.get("video", {})
        play_addr = video.get("play_addr", {})
        url_list = [u for u in (play_addr.get("url_list") or []) if u]
        candidates = []
        for candidate in url_list:
            no_watermark = candidate.replace("/playwm/", "/play/")
            for value in (no_watermark, candidate):
                if value not in candidates:
                    candidates.append(value)
        if not candidates:
            raise RuntimeError("无法获取视频下载地址")

        video_path = os.path.join(ext_dir, f"douyin-{aweme_id}.mp4")
        dl_timeout = aiohttp.ClientTimeout(total=1800, sock_read=600)
        dl_headers = {
            "Referer": "https://www.iesdouyin.com/",
            "User-Agent": mobile_ua,
        }
        last_status = None
        async with aiohttp.ClientSession(
            timeout=dl_timeout, headers=dl_headers
        ) as dl_session:
            for video_url in candidates:
                async with dl_session.get(
                    video_url, allow_redirects=True, proxy=proxy
                ) as resp:
                    last_status = resp.status
                    if resp.status != 200:
                        continue
                    content_type = (resp.headers.get("Content-Type") or "").lower()
                    if "video" not in content_type and "octet-stream" not in content_type:
                        continue
                    with open(video_path, "wb") as f:
                        async for chunk in resp.content.iter_chunked(64 * 1024):
                            f.write(chunk)
                    break
            else:
                raise RuntimeError(f"下载失败: HTTP {last_status or '未知'}")

        log.info("[douyin] 视频已下载: %.1fMB", os.path.getsize(video_path) / 1024 / 1024)
        return video_path, meta


def _parse_douyin_share_page(html: str, aweme_id: str) -> dict | None:
    """从公开分享页的 ``window._ROUTER_DATA`` 中找到目标视频。"""
    match = re.search(
        r"window\._ROUTER_DATA\s*=\s*(\{.*?\})\s*</script>",
        html or "",
        flags=re.DOTALL,
    )
    if not match:
        return None
    try:
        router_data = json.loads(match.group(1))
    except (TypeError, ValueError):
        return None

    def find_item(value) -> dict | None:
        if isinstance(value, dict):
            items = value.get("item_list")
            if isinstance(items, list):
                for item in items:
                    if (
                        isinstance(item, dict)
                        and str(item.get("aweme_id") or "") == aweme_id
                    ):
                        return item
            for child in value.values():
                found = find_item(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = find_item(child)
                if found:
                    return found
        return None

    return find_item(router_data)


async def _fetch_douyin_comments(api, aweme_id: str, top_n: int = 15) -> list[dict]:
    """拉视频热门评论(按点赞取前 top_n)。走 api 的通用签名请求,抖音 web 标准评论端点。
    接口失败返回空列表,绝不抛。"""
    raw = await api._request_json("/aweme/v1/web/comment/list/", {
        "aweme_id": aweme_id, "cursor": 0, "count": 50,
        "item_type": 0, "insert_ids": "", "whale_cut_token": "",
        "cut_version": 1, "rcFT": "",
    }, suppress_error=True)
    out = []
    for c in (raw or {}).get("comments") or []:
        text = (c.get("text") or "").strip()
        if text:
            out.append({"text": text[:200], "digg": c.get("digg_count", 0)})
    out.sort(key=lambda x: -x["digg"])
    return out[:top_n]


def _download_douyin_via_api(url: str, on_progress: ProgressCB) -> tuple[str, dict]:
    """同步包装 _douyin_download_async。"""
    import asyncio
    ext_dir = day_dir("raw", dt.date.today()) / "external"
    ext_dir.mkdir(parents=True, exist_ok=True)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_douyin_download_async(url, str(ext_dir)))
    finally:
        loop.close()


def _funasr_python() -> str:
    """装了 funasr 的 python 路径。funasr/torch 太重,通常装在系统 python 而非 .venv,
    launcher 也是用系统 python 跑 transcriber 的。优先 config [transcriber].python,
    否则探测常见系统 python,最后退当前解释器。"""
    cfg_py = (CONFIG.get("transcriber", {}).get("python") or "").strip()
    if cfg_py and Path(cfg_py).exists():
        return cfg_py
    for cand in (r"C:\Program Files\Python312\python.exe",
                 r"C:\Program Files\Python311\python.exe",
                 r"C:\Program Files\Python310\python.exe"):
        if Path(cand).exists():
            return cand
    return sys.executable


def _transcribe_douyin_video(video_path: str) -> list[dict]:
    """ffmpeg 提音频 → funasr 转写,返回 [{start,end,text}](整段一条)。
    funasr 装在系统 python(不在 launcher 的 .venv),所以走子进程跑转写,
    用 <<<DOUYIN_TEXT>>> 标记分隔,避开 funasr 加载时打印的一堆日志。"""
    wav_path = str(Path(video_path).with_suffix(".wav"))
    subprocess.run(
        ["ffmpeg", "-y", "-i", video_path, "-vn", "-ar", "16000",
         "-ac", "1", "-acodec", "pcm_s16le", wav_path],
        capture_output=True, check=True, creationflags=_NO_WINDOW,
    )
    try:
        if getattr(sys, "frozen", False):
            # 冻结包的 transcriber 位于 EXE 内部 PYZ，系统 Python 无法 import。
            # 当前 ingest-url 本身已经是独立后台进程，直接使用包内离线模型。
            from transcriber import transcribe_wav

            text = transcribe_wav(Path(wav_path)) or ""
        else:
            py = _funasr_python()
            src_dir = str(Path(__file__).resolve().parent)
            code = (
                "import sys\n"
                "sys.stdout.reconfigure(encoding='utf-8')\n"
                f"sys.path.insert(0, r'{src_dir}')\n"
                "from pathlib import Path\n"
                "from transcriber import transcribe_wav\n"
                f"t = transcribe_wav(Path(r'{wav_path}')) or ''\n"
                "print('<<<DOUYIN_TEXT>>>')\n"
                "print(t)\n"
            )
            r = subprocess.run(
                [py, "-c", code], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=900,
                creationflags=_NO_WINDOW,
            )
            out = r.stdout or ""
            if "<<<DOUYIN_TEXT>>>" in out:
                text = out.split("<<<DOUYIN_TEXT>>>", 1)[1].strip()
            else:
                log.warning(
                    "[douyin] 转写子进程无输出,stderr: %s",
                    (r.stderr or "")[-300:],
                )
                text = ""
    finally:
        try:
            os.remove(wav_path)
        except Exception:
            pass
    if not text:
        return []
    return [{"start": 0, "end": 0, "text": text.strip()}]


# ============================================================
# 抖音：主入口
# ============================================================
def ingest_douyin(url: str, on_progress: ProgressCB = NOOP_PROGRESS) -> dict:
    """抖音：官方 API 下载视频(绕过 yt-dlp)→ funasr 转写 → 存 jsonl + wiki。"""
    on_progress("info", "用抖音 API 下载视频(绕过 yt-dlp)...")
    video_path, meta = _download_douyin_via_api(url, on_progress)
    title = meta.get("title") or "抖音视频"
    uploader = meta.get("author") or "抖音博主"

    on_progress("asr", "本地 ASR 转写中(首次加载模型约 30s)...")
    try:
        segments = _transcribe_douyin_video(video_path)
    finally:
        for f in [video_path, str(Path(video_path).with_suffix(".wav"))]:
            try:
                if os.path.exists(f):
                    os.remove(f)
            except Exception:
                pass

    if not segments:
        raise RuntimeError(
            "本地 ASR 没有生成文字，内容尚未入库，也没有生成爆款分析；请重试"
        )

    full_text = " ".join(seg.get("text", "") for seg in segments).strip()
    # 看视频时被麦克风录进实时滚动的外放声音 → 立刻清一次
    n_scrub = _scrub_recent_transcripts(full_text)
    if n_scrub:
        on_progress("info", f"已从实时滚动清除外放回录 {n_scrub} 段")
    anchor = _post_ingest_to_wiki(
        source="douyin", url=url, title=title, uploader=uploader,
        full_text=full_text, record_count=len(segments),
        on_progress=on_progress,
    )
    bk = _post_ingest_baokuan(source="douyin", url=url, title=title, uploader=uploader,
                              full_text=full_text, meta=meta, on_progress=on_progress)
    # 收尾再清一次:吃掉转写管线延迟漏网的回录段
    _scrub_recent_transcripts(full_text)
    tail = "输入档案 + 爆款分析✓" if bk else "已存入输入档案"
    on_progress("done", f"抖音 · {title[:30]}... · {tail}(不进实时滚动)")
    return {"source": "douyin", "title": title, "count": len(segments), "url": url,
            "wiki_anchor": anchor, "baokuan_path": bk}


# ============================================================
# 微信视频号：SnapAny 官方 API 解析 → 本地转写
# ============================================================
def ingest_wechat_channels(url: str, on_progress: ProgressCB = NOOP_PROGRESS) -> dict:
    """视频号公开分享链接：解析并下载临时视频 → 本地 funasr → 入输入档案。"""
    from snapany_client import download_wechat_channels

    ext_dir = day_dir("raw", dt.date.today()) / "external"
    video_path, meta = download_wechat_channels(
        url,
        ext_dir,
        on_progress=on_progress,
    )
    title = meta.get("title") or "视频号视频"
    uploader = meta.get("author") or "视频号作者"

    on_progress("asr", "视频已下载，本地 ASR 转写中（首次加载模型约 30s）...")
    try:
        segments = _transcribe_douyin_video(video_path)
    finally:
        # SnapAny 直链短时有效且不落档；原视频与抽出的 WAV 都只用于本次本地转写。
        for item in (video_path, str(Path(video_path).with_suffix(".wav"))):
            try:
                if os.path.exists(item):
                    os.remove(item)
            except Exception:
                pass

    if not segments:
        raise RuntimeError(
            "本地 ASR 没有生成文字，内容尚未入库，也没有生成爆款分析；请重试"
        )

    full_text = " ".join(seg.get("text", "") for seg in segments).strip()
    n_scrub = _scrub_recent_transcripts(full_text)
    if n_scrub:
        on_progress("info", f"已从实时滚动清除外放回录 {n_scrub} 段")
    anchor = _post_ingest_to_wiki(
        source="wechat_channels",
        url=url,
        title=title,
        uploader=uploader,
        full_text=full_text,
        record_count=len(segments),
        on_progress=on_progress,
    )
    bk = _post_ingest_baokuan(
        source="wechat_channels",
        url=url,
        title=title,
        uploader=uploader,
        full_text=full_text,
        meta=meta,
        on_progress=on_progress,
    )
    _scrub_recent_transcripts(full_text)
    tail = "输入档案 + 爆款分析✓" if bk else "已存入输入档案"
    on_progress("done", f"视频号 · {title[:30]}... · {tail}(不进实时滚动)")
    return {
        "source": "wechat_channels",
        "title": title,
        "count": len(segments),
        "url": url,
        "wiki_anchor": anchor,
        "baokuan_path": bk,
    }


def _download_and_transcribe(url: str, on_progress: ProgressCB) -> list[dict]:
    """yt-dlp 下载音频到 raw/<日期>/external/，调用 transcriber 转写。"""
    today = dt.date.today()
    ext_dir = day_dir("raw", today) / "external"
    ext_dir.mkdir(parents=True, exist_ok=True)

    # 文件名用时间戳避免冲突
    ts = dt.datetime.now().strftime("%H-%M-%S")
    out_template = str(ext_dir / f"{ts}-%(id)s.%(ext)s")

    rc, out, err = _run_yt_dlp([
        "-x",                       # extract audio
        "--audio-format", "wav",
        "--audio-quality", "0",
        "-o", out_template,
        url,
    ], timeout=600)
    if rc != 0:
        raise RuntimeError(f"yt-dlp 下载音频失败:\n{err[-500:]}")

    # 找最新生成的 wav
    wavs = sorted(ext_dir.glob(f"{ts}-*.wav"), key=lambda p: p.stat().st_mtime)
    if not wavs:
        raise RuntimeError("yt-dlp 完成但找不到 wav 文件")
    wav_path = wavs[-1]
    log.info("音频下载完成: %s (%.1f MB)", wav_path.name,
             wav_path.stat().st_size / 1024 / 1024)

    # 调 transcriber 走转写
    on_progress("asr", "本地 ASR 转写中（首次加载模型约 30s）...")
    from transcriber import transcribe_wav
    text = transcribe_wav(wav_path)
    if not text:
        return []
    # 整段作为一条（外部内容不细切）
    return [{"start": 0, "end": 0, "text": text.strip()}]


# ============================================================
# 公众号：trafilatura 抽正文
# ============================================================
def ingest_wechat(url: str, on_progress: ProgressCB = NOOP_PROGRESS) -> dict:
    """微信公众号：抓正文 + 标题 + 作者，拆段入 jsonl。"""
    on_progress("fetch", "下载公众号文章...")
    try:
        import trafilatura
    except ImportError:
        raise RuntimeError("缺少 trafilatura，请运行：pip install trafilatura")

    # trafilatura.fetch_url 内置 UA + 重定向处理
    html = trafilatura.fetch_url(url)
    if not html:
        raise RuntimeError("文章下载失败（可能被反爬或链接失效）")

    on_progress("parse", "抽取正文...")
    # 用 trafilatura.extract 取干净正文
    text = trafilatura.extract(
        html,
        include_comments=False,
        include_tables=False,
        favor_precision=True,
        deduplicate=True,
    )
    if not text or len(text.strip()) < 50:
        raise RuntimeError("抽到的正文太短，可能是订阅页或图文")

    # 拿标题 + 作者
    meta = trafilatura.extract_metadata(html)
    title = (meta.title if meta else None) or "公众号文章"
    author = (meta.author if meta else None) or "公众号作者"

    # 按段落拆分（trafilatura 输出已经是\n\n 分段）
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    # 太短的合并到下一段
    merged: list[str] = []
    buf = ""
    for p in paragraphs:
        if len(p) < 40 and buf:
            buf += " " + p
        else:
            if buf:
                merged.append(buf)
            buf = p
    if buf:
        merged.append(buf)

    # 外部内容只写 obsidian 输入档案,不进 transcripts(=不进实时滚动)
    full_text = "\n\n".join(merged)
    anchor = _post_ingest_to_wiki(
        source="wechat", url=url, title=title, uploader=author,
        full_text=full_text, record_count=len(merged),
        on_progress=on_progress,
    )
    bk = _post_ingest_baokuan(source="wechat", url=url, title=title, uploader=author,
                              full_text=full_text, on_progress=on_progress)
    tail = "输入档案 + 爆款分析✓" if bk else "已存入输入档案"
    on_progress("done", f"公众号 · {title[:30]} · {tail}(不进实时滚动)")
    return {"source": "wechat", "title": title, "count": len(merged), "url": url,
            "wiki_anchor": anchor, "baokuan_path": bk}


# ============================================================
# 主入口
# ============================================================
def ingest_url(url: str, on_progress: ProgressCB = NOOP_PROGRESS) -> dict:
    """识别 URL 类型并分派到对应抓取器。返回元信息。"""
    url = url.strip()
    if not url:
        raise ValueError("空 URL")
    url = _extract_url(url)   # 从分享文本(口令/标题/#标签)里抠出真链接
    src = detect_source(url)
    log.info("[ingest] %s → %s", src, url)
    if src == "douyin":
        return ingest_douyin(url, on_progress)
    if src == "bilibili":
        return ingest_bilibili(url, on_progress)
    if src == "wechat_channels":
        return ingest_wechat_channels(url, on_progress)
    if src == "wechat":
        return ingest_wechat(url, on_progress)
    raise RuntimeError(
        f"不支持的 URL 来源（{src}）：{url}\n目前只支持：抖音 / B 站 / 视频号 / 公众号"
    )


def main():
    import argparse
    import json as _json
    parser = argparse.ArgumentParser(description="抓取 URL 内容入语料库")
    parser.add_argument("url", help="抖音 / B 站 / 视频号 / 公众号 URL")
    parser.add_argument("--ipc", action="store_true",
                        help="结构化输出(供 launcher 子进程解析进度/结果)")
    args = parser.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    if args.ipc:
        # launcher 子进程模式:进度/结果/错误都用标记单行输出,父进程解析
        def progress(stage: str, msg: str):
            print(f"@@PROGRESS@@\t{stage}\t{msg}", flush=True)
        try:
            result = ingest_url(args.url, on_progress=progress)
            print("@@RESULT@@\t" + _json.dumps(result, ensure_ascii=False), flush=True)
        except Exception as e:
            import traceback
            payload = f"{e}\n{traceback.format_exc()[-1200:]}".replace("\n", "\\n")
            print("@@ERROR@@\t" + payload, flush=True)
            sys.exit(1)
    else:
        def progress(stage: str, msg: str):
            print(f"[{stage}] {msg}", flush=True)
        try:
            result = ingest_url(args.url, on_progress=progress)
            print(f"\n[ok] {result}")
        except Exception as e:
            print(f"\n[error] {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
