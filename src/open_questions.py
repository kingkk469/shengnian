"""open_questions 池子 — 跨天累积的"还没想清楚的问题"清单。

数据存储:notes/open_questions.json (单文件,不再按日期拆)
状态机:open → resolved (想清楚了,结论入 Wiki) / dismissed (不重要,删掉)

被 daily_summary.py 调用:add_questions() 把 review 生成的新问题加进池子(自动去重)
被 launcher.py 调用:list_open() / mark_resolved() / mark_dismissed() / save_draft()
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, setup_logger

log = setup_logger("open-questions")


POOL_PATH = ROOT / "notes" / "open_questions.json"
DISCUSS_ARCHIVE = ROOT / "notes" / "discuss-archive"   # 单次讨论原始对话留底
MAX_OPEN_QUESTIONS = 3


# ============================================================
# 内部:读写池子
# ============================================================
def _load() -> dict:
    if not POOL_PATH.exists():
        return {"version": 1, "questions": [], "archive": []}
    try:
        data = json.loads(POOL_PATH.read_text(encoding="utf-8"))
        # 兜底字段
        data.setdefault("version", 1)
        data.setdefault("questions", [])
        data.setdefault("archive", [])
        return data
    except Exception as e:
        log.warning("[pool] 读 open_questions.json 失败: %s,从空池开始", e)
        return {"version": 1, "questions": [], "archive": []}


def _save(data: dict) -> None:
    POOL_PATH.parent.mkdir(parents=True, exist_ok=True)
    # 原子写:先写 tmp 再 rename
    tmp = POOL_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(POOL_PATH)


def _now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _make_id(title: str, source_date: str) -> str:
    """生成稳定 id:日期 + title 哈希前 6 位。"""
    h = hashlib.sha1(title.encode("utf-8")).hexdigest()[:6]
    return f"q-{source_date}-{h}"


def _normalize(title: str) -> str:
    """标题归一化用于去重 — 去标点、空格、统一大小写。"""
    import re
    s = re.sub(r"[^\w一-鿿]+", "", title.lower())
    return s


# ============================================================
# 公开 API
# ============================================================
def list_open() -> list[dict]:
    """返回所有 status=open 的问题,按 created_at 倒序(最新在前)。"""
    data = _load()
    opens = [q for q in data["questions"] if q.get("status") == "open"]
    opens.sort(key=lambda q: q.get("created_at", ""), reverse=True)
    return opens


def list_all_titles(include_archived: bool = False) -> list[str]:
    """返回所有 open 问题的归一化标题,用于去重比较。"""
    data = _load()
    titles = [_normalize(q.get("title", "")) for q in data["questions"]
              if q.get("status") == "open"]
    if include_archived:
        titles += [_normalize(q.get("title", "")) for q in data["archive"]]
    return titles


def add_questions(new_questions: list[dict], source_date: str) -> dict:
    """添加一批 question 到池子。自动去重(同 source_date 内 title 相同跳过;
    跨日期内容相似度高也跳过 — 简单做:归一化标题完全匹配视为重复)。

    返回 {added: int, skipped: int, ids: list[str]}
    """
    data = _load()
    existing_titles = set(_normalize(q.get("title", "")) for q in data["questions"])
    archived_titles = set(_normalize(q.get("title", "")) for q in data["archive"])
    open_count = sum(1 for q in data["questions"] if q.get("status") == "open")
    available_slots = max(0, MAX_OPEN_QUESTIONS - open_count)

    added_ids = []
    skipped = 0
    for raw in new_questions:
        title = (raw.get("title") or "").strip()
        if not title:
            continue
        nt = _normalize(title)
        if nt in existing_titles:
            skipped += 1
            log.info("[pool] 跳过重复(在 open 里):%s", title[:30])
            continue
        if nt in archived_titles:
            skipped += 1
            log.info("[pool] 跳过重复(在 archive 里 — 之前已 resolved/dismissed):%s",
                     title[:30])
            continue
        if len(added_ids) >= available_slots:
            skipped += 1
            log.info("[pool] 待讨论已达上限 %d，跳过:%s",
                     MAX_OPEN_QUESTIONS, title[:30])
            continue
        qid = _make_id(title, source_date)
        # 兜底 id 冲突 — 加 random 后缀
        if any(q.get("id") == qid for q in data["questions"]):
            qid = qid + "-" + _now_iso()[-5:]
        entry = {
            "id": qid,
            "title": title,
            "context": raw.get("context", ""),
            "why_matters": raw.get("why_matters", ""),
            "related_quotes": raw.get("related_quotes") or [],
            "status": "open",
            "created_at": _now_iso(),
            "source_date": source_date,
            "last_discussed_at": None,
            "draft": None,
        }
        data["questions"].append(entry)
        existing_titles.add(nt)
        added_ids.append(qid)

    if added_ids or skipped:
        _save(data)
        log.info("[pool] 新增 %d 条 / 跳过重复 %d 条", len(added_ids), skipped)
    return {"added": len(added_ids), "skipped": skipped, "ids": added_ids}


def find(qid: str) -> dict | None:
    """按 id 找 question(无论 status)。"""
    data = _load()
    for q in data["questions"]:
        if q.get("id") == qid:
            return q
    for q in data["archive"]:
        if q.get("id") == qid:
            return q
    return None


def save_draft(qid: str, framework: str, messages: list[dict]) -> bool:
    """暂存对话草稿。下次点同一个 question 可恢复。"""
    data = _load()
    for q in data["questions"]:
        if q.get("id") == qid and q.get("status") == "open":
            q["draft"] = {
                "framework": framework,
                "messages": messages,
                "saved_at": _now_iso(),
            }
            q["last_discussed_at"] = _now_iso()
            _save(data)
            log.info("[pool] 已暂存 %s 的草稿(%d 条消息)", qid, len(messages))
            return True
    return False


def mark_resolved(qid: str, conclusion: str, framework: str,
                  messages: list[dict]) -> dict | None:
    """标记某个问题为 resolved。
    返回 archive 后的完整记录(供 launcher 写入第二大脑/讨论档案.md)。
    """
    data = _load()
    for i, q in enumerate(data["questions"]):
        if q.get("id") == qid and q.get("status") == "open":
            q["status"] = "resolved"
            q["resolved_at"] = _now_iso()
            q["conclusion"] = conclusion
            q["framework_used"] = framework
            q["final_messages"] = messages
            q["draft"] = None
            # 移到 archive
            data["archive"].append(q)
            data["questions"].pop(i)
            _save(data)
            log.info("[pool] resolved: %s", qid)
            return q
    return None


def mark_dismissed(qid: str, reason: str = "") -> bool:
    """标记某个问题为 dismissed(没意义/删掉)。从 open 移到 archive。"""
    data = _load()
    for i, q in enumerate(data["questions"]):
        if q.get("id") == qid and q.get("status") == "open":
            q["status"] = "dismissed"
            q["dismissed_at"] = _now_iso()
            q["dismiss_reason"] = reason
            q["draft"] = None
            data["archive"].append(q)
            data["questions"].pop(i)
            _save(data)
            log.info("[pool] dismissed: %s", qid)
            return True
    return False


def migrate_legacy_questions() -> int:
    """一次性:把现有的 notes/YYYY-MM-DD-questions.json 全部迁移到 pool。
    迁完后旧文件改名为 .migrated 留底,不删。
    返回迁入的 question 数。
    """
    notes = ROOT / "notes"
    total = 0
    for p in sorted(notes.glob("*-questions.json")):
        # 跳过自己(pool 文件名不一样,不会撞)
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            log.warning("[migrate] 读 %s 失败,跳过", p.name)
            continue
        day = raw.get("day") or p.stem.replace("-questions", "")
        qs = raw.get("questions") or []
        if not qs:
            continue
        r = add_questions(qs, source_date=day)
        total += r["added"]
        # 改名留底
        backup = p.with_suffix(".json.migrated")
        try:
            p.replace(backup)
            log.info("[migrate] %s -> %s (迁入 %d 条)", p.name, backup.name, r["added"])
        except Exception as e:
            log.warning("[migrate] 改名失败 %s: %s", p.name, e)
    return total


# ============================================================
# CLI:python -m open_questions [list / migrate / show <id>]
# ============================================================
def main():
    import argparse
    parser = argparse.ArgumentParser(description="open_questions 池子管理")
    parser.add_argument("cmd", choices=["list", "migrate", "show", "stats"],
                        help="list:列 open / migrate:迁旧文件 / show <id>:看详情 / stats:统计")
    parser.add_argument("--id", help="show 时指定 id")
    args = parser.parse_args()

    if args.cmd == "list":
        opens = list_open()
        print(f"open 问题数:{len(opens)}\n")
        for q in opens:
            print(f"  [{q['id']}] {q['title']}")
            print(f"     {q.get('source_date', '?')} 创建,"
                  f"上次讨论:{q.get('last_discussed_at') or '从未'}\n")
    elif args.cmd == "migrate":
        n = migrate_legacy_questions()
        print(f"已迁入 {n} 条到 open_questions.json")
    elif args.cmd == "show":
        if not args.id:
            print("需要 --id <qid>")
            return
        q = find(args.id)
        if q:
            print(json.dumps(q, ensure_ascii=False, indent=2))
        else:
            print("找不到")
    elif args.cmd == "stats":
        data = _load()
        opens = sum(1 for q in data["questions"] if q.get("status") == "open")
        resolved = sum(1 for q in data["archive"] if q.get("status") == "resolved")
        dismissed = sum(1 for q in data["archive"] if q.get("status") == "dismissed")
        print(f"open:{opens}\nresolved:{resolved}\ndismissed:{dismissed}")


if __name__ == "__main__":
    main()
