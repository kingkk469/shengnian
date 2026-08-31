"""content_ideas 选题池 — 跨天累积的"内容选题候选"清单。

数据存储:notes/content_ideas.json (单文件)
状态机:open → drafting(创作中,有草稿) → done(成稿,落地内容产出) / dismissed(不做)

被 content_radar.py 调用:add_ideas() 把雷达扫出的选题加进池(自动去重)
被 launcher.py 调用:list_open() / save_draft() / mark_done() / mark_dismissed()

idea 跟 open_questions 的 question 同构,多两个字段:
- format: "shortvideo"(短视频口播) | "article"(公众号长文)
- hook: 抓手(为什么这个点有内容潜力)
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, setup_logger
from onboarding_profile import load_user_profile

log = setup_logger("content-ideas")

POOL_PATH = ROOT / "notes" / "content_ideas.json"
DEFAULT_MAX_OPEN_IDEAS = 20


def max_open_ideas() -> int:
    """读取当前用户的候选池上限；异常或未配置时使用宽松默认值 20。"""
    try:
        workflow = load_user_profile(ROOT).get("workflow_preferences") or {}
        content = workflow.get("content") or {}
        return max(
            1,
            min(20, int(content.get("max_items_per_run", DEFAULT_MAX_OPEN_IDEAS))),
        )
    except (TypeError, ValueError, AttributeError):
        return DEFAULT_MAX_OPEN_IDEAS


# ============================================================
# 内部:读写池子
# ============================================================
def _load() -> dict:
    if not POOL_PATH.exists():
        return {"version": 1, "ideas": [], "archive": []}
    try:
        data = json.loads(POOL_PATH.read_text(encoding="utf-8"))
        data.setdefault("version", 1)
        data.setdefault("ideas", [])
        data.setdefault("archive", [])
        return data
    except Exception as e:
        log.warning("[pool] 读 content_ideas.json 失败: %s,从空池开始", e)
        return {"version": 1, "ideas": [], "archive": []}


def _save(data: dict) -> None:
    POOL_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = POOL_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(POOL_PATH)


def _now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _make_id(title: str, source_date: str) -> str:
    h = hashlib.sha1(title.encode("utf-8")).hexdigest()[:6]
    return f"idea-{source_date}-{h}"


def _normalize(title: str) -> str:
    """标题归一化用于去重。"""
    import re
    s = re.sub(r"[^\w一-鿿]+", "", title.lower())
    return s


# ============================================================
# 公开 API
# ============================================================
def list_open(format_filter: str | None = None) -> list[dict]:
    """返回所有 status=open 的选题,按 created_at 倒序。
    format_filter: 'shortvideo' / 'article' / None(全部)。"""
    data = _load()
    opens = [i for i in data["ideas"] if i.get("status") in ("open", "drafting")]
    if format_filter:
        opens = [i for i in opens if i.get("format") == format_filter]
    opens.sort(key=lambda i: i.get("created_at", ""), reverse=True)
    return opens


def list_open_titles(include_archived: bool = True) -> list[str]:
    """所有 open 选题的归一化标题,去重用。"""
    data = _load()
    titles = [_normalize(i.get("title", "")) for i in data["ideas"]]
    if include_archived:
        titles += [_normalize(i.get("title", "")) for i in data["archive"]]
    return titles


def add_ideas(new_ideas: list[dict], source_date: str) -> dict:
    """添加一批选题到池子,自动去重(归一化标题匹配)。
    每个 new_idea 应含: title / format / hook / source_quotes / (source_concepts)
    返回 {added, skipped, ids}"""
    data = _load()
    existing = set(_normalize(i.get("title", "")) for i in data["ideas"])
    archived = set(_normalize(i.get("title", "")) for i in data["archive"])
    incoming_formats = {
        raw.get("format", "shortvideo")
        if raw.get("format", "shortvideo") in ("shortvideo", "article")
        else "shortvideo"
        for raw in new_ideas
    }
    format_scope = next(iter(incoming_formats)) if len(incoming_formats) == 1 else None
    open_count = sum(1 for i in data["ideas"]
                     if i.get("status") in ("open", "drafting")
                     and (format_scope is None or i.get("format") == format_scope))
    open_limit = max_open_ideas()
    available_slots = max(0, open_limit - open_count)

    added_ids, skipped = [], 0
    for raw in new_ideas:
        title = (raw.get("title") or "").strip()
        if not title:
            continue
        nt = _normalize(title)
        if nt in existing:
            skipped += 1
            log.info("[pool] 跳过重复(open):%s", title[:30])
            continue
        if nt in archived:
            skipped += 1
            log.info("[pool] 跳过重复(archive):%s", title[:30])
            continue
        if len(added_ids) >= available_slots:
            skipped += 1
            log.info("[pool] 选题池已达上限 %d，跳过:%s",
                     open_limit, title[:30])
            continue
        fmt = raw.get("format", "shortvideo")
        if fmt not in ("shortvideo", "article"):
            fmt = "shortvideo"
        iid = _make_id(title, source_date)
        if any(i.get("id") == iid for i in data["ideas"]):
            iid = iid + "-" + _now_iso()[-5:]
        entry = {
            "id": iid,
            "title": title,
            "format": fmt,
            "hook": raw.get("hook", ""),
            "source_quotes": raw.get("source_quotes") or [],
            "source_concepts": raw.get("source_concepts") or [],
            # 公众号长文专属(短视频留空,无害):5 原型 / 主题内核 / 为什么现在写
            "prototype": raw.get("prototype", ""),
            "theme": raw.get("theme", ""),
            "why_now": raw.get("why_now", ""),
            # 二创专属(贴链接进来的外部爆款):origin="external" + 爆款分析文档路径 + 原链接
            "origin": raw.get("origin", ""),
            "baokuan_path": raw.get("baokuan_path", ""),
            "source_url": raw.get("source_url", ""),
            "status": "open",
            "created_at": _now_iso(),
            "source_date": source_date,
            "last_worked_at": None,
            "draft": None,
        }
        data["ideas"].append(entry)
        existing.add(nt)
        added_ids.append(iid)

    if added_ids or skipped:
        _save(data)
        log.info("[pool] 新增 %d 条选题 / 跳过重复 %d 条", len(added_ids), skipped)
    open_after = open_count + len(added_ids)
    return {
        "added": len(added_ids),
        "skipped": skipped,
        "ids": added_ids,
        "limit": open_limit,
        "limit_reached": open_after >= open_limit,
        "remaining_slots": max(0, open_limit - open_after),
    }


def find(idea_id: str) -> dict | None:
    data = _load()
    for i in data["ideas"]:
        if i.get("id") == idea_id:
            return i
    for i in data["archive"]:
        if i.get("id") == idea_id:
            return i
    return None


def save_draft(idea_id: str, stage: str, messages: list[dict]) -> bool:
    """暂存工坊草稿。stage 短视频:angle/script;公众号:frame/detail/write/check。"""
    data = _load()
    for i in data["ideas"]:
        if i.get("id") == idea_id and i.get("status") in ("open", "drafting"):
            i["status"] = "drafting"
            i["draft"] = {
                "stage": stage,
                "messages": messages,
                "saved_at": _now_iso(),
            }
            i["last_worked_at"] = _now_iso()
            _save(data)
            log.info("[pool] 暂存 %s 草稿(stage=%s,%d 条消息)",
                     idea_id, stage, len(messages))
            return True
    return False


def mark_done(idea_id: str, final_content: str, messages: list[dict]) -> dict | None:
    """标记成稿。把成品 + 对话归到 archive。返回完整记录(供 launcher 落地存档)。"""
    data = _load()
    for idx, i in enumerate(data["ideas"]):
        if i.get("id") == idea_id and i.get("status") in ("open", "drafting"):
            i["status"] = "done"
            i["done_at"] = _now_iso()
            i["final_content"] = final_content
            i["final_messages"] = messages
            i["draft"] = None
            data["archive"].append(i)
            data["ideas"].pop(idx)
            _save(data)
            log.info("[pool] done: %s", idea_id)
            return i
    return None


def mark_dismissed(idea_id: str, reason: str = "") -> bool:
    """标记不做。从 open 移到 archive。"""
    data = _load()
    for idx, i in enumerate(data["ideas"]):
        if i.get("id") == idea_id and i.get("status") in ("open", "drafting"):
            i["status"] = "dismissed"
            i["dismissed_at"] = _now_iso()
            i["dismiss_reason"] = reason
            i["draft"] = None
            data["archive"].append(i)
            data["ideas"].pop(idx)
            _save(data)
            log.info("[pool] dismissed: %s", idea_id)
            return True
    return False


# ============================================================
# CLI:python -m content_ideas [list / show / stats]
# ============================================================
def main():
    import argparse
    parser = argparse.ArgumentParser(description="content_ideas 选题池管理")
    parser.add_argument("cmd", choices=["list", "show", "stats"])
    parser.add_argument("--id", help="show 时指定 id")
    parser.add_argument("--format", help="list 时按形态过滤 shortvideo/article")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    if args.cmd == "list":
        opens = list_open(format_filter=args.format)
        print(f"open 选题数:{len(opens)}\n")
        for i in opens:
            badge = "📱短视频" if i["format"] == "shortvideo" else "📄公众号"
            draft_mark = " [有草稿]" if i.get("draft") else ""
            print(f"  [{badge}] {i['title']}{draft_mark}")
            print(f"     抓手:{i.get('hook', '')[:50]}")
            print(f"     {i.get('source_date', '?')} · id={i['id']}\n")
    elif args.cmd == "show":
        if not args.id:
            print("需要 --id")
            return
        i = find(args.id)
        print(json.dumps(i, ensure_ascii=False, indent=2) if i else "找不到")
    elif args.cmd == "stats":
        data = _load()
        opens = sum(1 for i in data["ideas"] if i.get("status") in ("open", "drafting"))
        sv = sum(1 for i in data["ideas"] if i.get("format") == "shortvideo"
                 and i.get("status") in ("open", "drafting"))
        art = opens - sv
        done = sum(1 for i in data["archive"] if i.get("status") == "done")
        dismissed = sum(1 for i in data["archive"] if i.get("status") == "dismissed")
        print(f"open:{opens} (短视频 {sv} / 公众号 {art})\ndone:{done}\ndismissed:{dismissed}")


if __name__ == "__main__":
    main()
