"""月度体检（C3）：搭语音日记 daily run 便车，跨月跑一次第二大脑全库 lint。

- 确定性检查（不调 LLM、不推企微）：失效 [[链接]] / 孤页 / 过时 synthesis(>90 天)
- 只读扫描 + 写报告，不擅自修复
- 报告写 第二大脑/_lint/YYYY-MM-体检.md；index 顶部由 wiki_appender 自动挂提醒
- king 处理完删掉报告文件，提醒随之消失

被 daily_summary.summarize_day 在每天跑完时调 run_if_due()，只有跨月才真跑。
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, configured_obsidian_vault, knowledge_dir, setup_logger

log = setup_logger("monthly-lint")
STATE_PATH = ROOT / "runtime" / "monthly_lint_state.json"

_WIKILINK = re.compile(r"\[\[([^\]|#]+)")


def _strip_code(text: str) -> str:
    """去掉代码块/行内代码，避免把示意性的 `[[双链]]` 当成真失效链接。"""
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    return re.sub(r"`[^`]*`", "", text)


def _vault_root() -> Path:
    vault = configured_obsidian_vault()
    return vault if vault else (ROOT / "notes")


def _brain_root() -> Path:
    return knowledge_dir()


def _due(day: dt.date) -> bool:
    """跨月（或从未跑过）→ 到期。"""
    if not STATE_PATH.exists():
        return True
    try:
        last = json.loads(STATE_PATH.read_text(encoding="utf-8")).get("last_month", "")
    except Exception:
        return True
    return last != day.strftime("%Y-%m")


def _save_state(day: dt.date) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps({"last_month": day.strftime("%Y-%m"), "ran_at": day.isoformat()},
                   ensure_ascii=False),
        encoding="utf-8",
    )


def _section(title: str, items: list[str]) -> list[str]:
    out = [f"## {title}（{len(items)}）", ""]
    out += items if items else ["（无）"]
    out += [""]
    return out


def _lint(vault: Path, brain: Path, day: dt.date) -> str:
    # 全库 .md 基名（判失效 [[链接]] 用——Obsidian 按文件名跨库解析）
    basenames = {p.stem for p in vault.rglob("*.md")}

    # 只体检"知识层"：方法论 concepts/entities/synthesis + 子弹库
    targets: list[Path] = []
    for sub in ("concepts", "entities", "synthesis", "子弹库"):
        d = brain / sub
        if d.exists():
            targets += sorted(d.glob("*.md"))

    out_links: dict[str, list[str]] = {}
    in_count: dict[str, int] = {p.stem: 0 for p in targets}
    broken: list[tuple[str, str]] = []

    for p in targets:
        text = _strip_code(p.read_text(encoding="utf-8", errors="ignore"))
        links = [m.group(1).strip() for m in _WIKILINK.finditer(text)]
        out_links[p.stem] = links
        for l in links:
            if l not in basenames:
                broken.append((p.relative_to(brain).as_posix(), l))
            if l in in_count:
                in_count[l] += 1

    orphan = [
        p.relative_to(brain).as_posix()
        for p in targets
        if not out_links.get(p.stem) and in_count.get(p.stem, 0) == 0
    ]

    stale: list[str] = []
    syn = brain / "synthesis"
    if syn.exists():
        cutoff = day - dt.timedelta(days=90)
        for p in sorted(syn.glob("*.md")):
            try:
                mtime = dt.date.fromtimestamp(p.stat().st_mtime)
                if mtime < cutoff:
                    stale.append(f"- {p.stem}（最后更新 {mtime.isoformat()}）")
            except Exception:
                pass

    lines = [
        f"# {day.strftime('%Y-%m')} 体检报告",
        f"> 自动生成 {day.isoformat()} · 只列问题、不擅自修 · 处理完删掉本文件，index 提醒随之消失",
        "",
        f"扫描知识层 **{len(targets)}** 页。",
        "",
    ]
    lines += _section("⚠️ 失效链接", [f"- `{s}` → `[[{t}]]`（目标不存在）" for s, t in broken[:80]])
    lines += _section("🏝️ 孤页 · 无任何出入链", [f"- {o}" for o in orphan[:80]])
    lines += _section("⏳ 过时 synthesis · >90 天未更新", stale)
    return "\n".join(lines)


def run_if_due(day: dt.date | None = None) -> bool:
    """daily run 调它；只有跨月才真跑。返回是否跑了。"""
    if day is None:
        day = dt.date.today()
    brain = _brain_root()
    vault = _vault_root()
    if brain is None or vault is None or not brain.exists():
        return False
    if not _due(day):
        return False

    log.info("[lint] 跨月，跑月度体检 %s", day.strftime("%Y-%m"))
    try:
        report = _lint(vault, brain, day)
    except Exception as e:
        log.warning("[lint] 体检失败: %s", e)
        return False

    out_dir = brain / "_lint"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{day.strftime('%Y-%m')}-体检.md").write_text(report, encoding="utf-8")
    _save_state(day)
    log.info("[lint] 报告已写 _lint/%s-体检.md", day.strftime("%Y-%m"))
    return True


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD，默认今天")
    ap.add_argument("--force", action="store_true", help="忽略'跨月才跑'，强制跑一次")
    a = ap.parse_args()
    d = dt.date.fromisoformat(a.date) if a.date else dt.date.today()
    if a.force and STATE_PATH.exists():
        STATE_PATH.unlink()
    print("体检已跑（看 第二大脑/_lint/）" if run_if_due(d) else "未到期 / 跳过")
