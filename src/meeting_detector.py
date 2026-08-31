"""会议候选检测 — 扫最近 N 天的转写,识别"看起来像会议的段落区间"。

判断条件(必须同时满足才算候选):
  ① 持续 ≥ 15 分钟(start 到 end 跨度)
  ② 段数密集:平均每 2 分钟 ≥ 1 段(避免稀疏自言自语)
  ③ 多说话人:有 ≥ 2 个不同的 speaker_name,且非 king 的占比 ≥ 30%
  ④ 字数足:总字数 ≥ 1500

输出:候选区间列表,launcher 可以提醒用户"5/30 14:00-14:45 看起来是一场会议,要生成纪要吗?"

被 launcher 调:meeting_detector.find_candidates(lookback_days=7)
被 CLI 调:python -m meeting_detector
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, configured_owner_name, setup_logger

log = setup_logger("meeting-detector")


STATE_PATH = ROOT / "runtime" / "meeting_detector_state.json"


# ============================================================
# 状态:已经被忽略或已经生成过纪要的区间不再提醒
# ============================================================
def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {"dismissed": [], "exported": []}
    try:
        d = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        d.setdefault("dismissed", [])
        d.setdefault("exported", [])
        return d
    except Exception:
        return {"dismissed": [], "exported": []}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def mark_dismissed(day_str: str, start: str, end: str) -> None:
    """用户点「忽略」 → 这个区间不再提醒。"""
    s = _load_state()
    key = f"{day_str}|{start}|{end}"
    if key not in s["dismissed"]:
        s["dismissed"].append(key)
        _save_state(s)


def mark_exported(day_str: str, start: str, end: str) -> None:
    """已经生成过纪要 → 不再提醒。"""
    s = _load_state()
    key = f"{day_str}|{start}|{end}"
    if key not in s["exported"]:
        s["exported"].append(key)
        _save_state(s)


def _is_processed(day_str: str, start: str, end: str) -> bool:
    s = _load_state()
    key = f"{day_str}|{start}|{end}"
    return key in s["dismissed"] or key in s["exported"]


# ============================================================
# 核心:扫一天的 jsonl,找候选区间
# ============================================================
MIN_DURATION_MIN = 15        # 至少 15 分钟
MAX_DURATION_MIN = 120       # 超过 2 小时大概率是直播/录课
MIN_TOTAL_CHARS = 2000
MIN_DENSITY = 0.8
MIN_NON_KING_RATIO = 0.30    # 非 king 段 ≥ 30%
MAX_KING_RATIO = 0.65        # king 段 ≤ 65% — 严格点排除"king 主讲+对方少量插话"
MIN_OTHER_AVG_CHARS = 8      # 对方平均每段 ≥ 8 字(弹幕都很短:"哈哈"/"对"/"嗯")
GAP_THRESHOLD_MIN = 8        # 段之间 > 8 分钟就切

# 关键词分两组:强(必须命中)+ 弱(辅助提分)
STRONG_MEETING_KW = [
    "咨询", "面谈", "面试", "签合同",
    "您这边", "你这边", "谈一下", "对接", "复盘",
    "评估", "汇报", "请教", "合作", "聊一下", "会议", "讨论",
]
# 直播/课程关键词:命中就降分(避免误报为会议)
LIVESTREAM_KW = [
    "大家好", "Hello", "hello", "各位", "小伙伴",
    "本次直播", "我们今天的课", "这节课", "这门课",
    "你们看到", "点关注", "弹幕",
]


def _parse_ts(s: str) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None


def _find_blocks_in_day(day: dt.date) -> list[dict]:
    """把一天的转写按时间间隔切成 blocks(相邻段间隔 > 8 分钟切开)。"""
    tp = ROOT / "transcripts" / f"{day.isoformat()}.jsonl"
    if not tp.exists():
        return []

    recs = []
    for line in tp.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        # 排除贴链接抓回来的(source: douyin/bilibili/wechat)
        if r.get("source") in ("douyin", "bilibili", "wechat"):
            continue
        ts = _parse_ts(r.get("start"))
        if ts is None:
            continue
        recs.append({
            "ts": ts,
            "text": r.get("text") or "",
            "speaker": r.get("speaker_name") or "未知",
            "duration_sec": r.get("duration_sec") or 0,
            "raw_idx": len(recs),
        })
    if not recs:
        return []

    recs.sort(key=lambda x: x["ts"])

    blocks = []
    cur = [recs[0]]
    for r in recs[1:]:
        gap = (r["ts"] - cur[-1]["ts"]).total_seconds() / 60
        if gap > GAP_THRESHOLD_MIN:
            blocks.append(cur)
            cur = [r]
        else:
            cur.append(r)
    blocks.append(cur)
    return blocks


def _is_meeting_like(block: list[dict]) -> dict | None:
    """判断一个 block 是否像会议。返回 None 不像,返回 dict 像(含统计信息)。"""
    if not block:
        return None
    start_ts = block[0]["ts"]
    end_ts = block[-1]["ts"]
    duration_min = (end_ts - start_ts).total_seconds() / 60
    if duration_min < MIN_DURATION_MIN:
        return None
    if duration_min > MAX_DURATION_MIN:
        return None   # 太长:大概率是直播/录课/整天对话累积

    total_chars = sum(len(r["text"]) for r in block)
    if total_chars < MIN_TOTAL_CHARS:
        return None

    density = len(block) / max(duration_min, 1)
    if density < MIN_DENSITY:
        return None

    speakers = {}
    speaker_chars = {}   # 每个 speaker 总字数,用于算"对方平均段长"
    for r in block:
        sp = (r["speaker"] or "未知").strip()
        speakers[sp] = speakers.get(sp, 0) + 1
        speaker_chars[sp] = speaker_chars.get(sp, 0) + len(r["text"])
    if len(speakers) < 2:
        return None

    owner_aliases = {configured_owner_name().casefold(), "king", "我"}
    king_count = sum(
        v for k, v in speakers.items() if str(k).casefold() in owner_aliases
    )
    non_king = sum(
        v for k, v in speakers.items() if str(k).casefold() not in owner_aliases
    )
    non_king_ratio = non_king / max(len(block), 1)
    if non_king_ratio < MIN_NON_KING_RATIO:
        return None
    king_ratio = king_count / max(len(block), 1)
    if king_ratio > MAX_KING_RATIO:
        return None

    # 对方平均每段字数(过滤直播 — 弹幕几个字)
    other_chars_total = sum(
        c for sp, c in speaker_chars.items()
        if str(sp).casefold() not in owner_aliases
    )
    other_count_total = sum(
        c for sp, c in speakers.items()
        if str(sp).casefold() not in owner_aliases
    )
    other_avg = other_chars_total / max(other_count_total, 1)
    if other_avg < MIN_OTHER_AVG_CHARS:
        return None   # 对方都是短句 → 弹幕/插话,不是会议

    text_blob = " ".join(r["text"] for r in block)
    kw_hits = sum(1 for kw in STRONG_MEETING_KW if kw in text_blob)
    if kw_hits == 0:
        return None
    # 直播关键词命中 → 直接判定不是会议(更严格)
    live_hits = sum(1 for kw in LIVESTREAM_KW if kw in text_blob)
    if live_hits >= 2:
        return None

    # 给个粗略的"会议感"分数 0-100
    score = min(100, int(
        duration_min * 1.5      # 时长加权
        + density * 10          # 密度加权
        + non_king_ratio * 50   # 对话感加权
        + kw_hits * 5           # 关键词
    ))

    # 取一个简短摘要(前 3 段文本)
    snippet = " / ".join(r["text"][:30] for r in block[:3] if r["text"])

    return {
        "start": start_ts.isoformat(timespec="seconds"),
        "end": end_ts.isoformat(timespec="seconds"),
        "duration_min": round(duration_min, 1),
        "segments": len(block),
        "total_chars": total_chars,
        "speakers": speakers,
        "non_king_ratio": round(non_king_ratio, 2),
        "score": score,
        "snippet": snippet,
        "kw_hits": kw_hits,
    }


# ============================================================
# 公开 API
# ============================================================
def _days_with_existing_meeting_md(lookback_days: int) -> set[str]:
    """扫 meetings/ 目录,看哪些天已经有过会议纪要文件。
    返回 {day_str, ...}。
    """
    from common import CONFIG as _CFG
    meetings_dir = ROOT / "meetings"
    found = set()
    if meetings_dir.exists():
        for p in meetings_dir.glob("*.md"):
            # 文件名形如 2026-05-28-会议-主题.md → 提取前 10 字
            name = p.name
            if len(name) >= 10 and name[4] == "-" and name[7] == "-":
                found.add(name[:10])
    # 也看 Obsidian vault/第二大脑/会议纪要/
    try:
        vault = _CFG.get("obsidian", {}).get("vault", "")
        if vault:
            vault_meetings = Path(vault) / "第二大脑" / "会议纪要"
            if vault_meetings.exists():
                for p in vault_meetings.glob("*.md"):
                    name = p.name
                    if len(name) >= 10 and name[4] == "-" and name[7] == "-":
                        found.add(name[:10])
    except Exception:
        pass
    return found


def find_candidates(lookback_days: int = 7,
                    exclude_today: bool = True,
                    hide_days_with_existing: bool = True) -> list[dict]:
    """扫最近 N 天,返回所有"看起来是会议"的候选区间。

    hide_days_with_existing: 当天已经有任何会议纪要 → 跳过这天所有候选
                              (假设你那天该做的会议都做了。误判时可关掉)
    已被 dismissed / exported 的不返回。

    返回:[{day, start, end, duration_min, segments, speakers, score, snippet}, ...]
           按 score 倒序
    """
    today = dt.date.today()
    skip_days = (_days_with_existing_meeting_md(lookback_days)
                 if hide_days_with_existing else set())

    candidates = []
    for i in range(lookback_days):
        d = today - dt.timedelta(days=i)
        if exclude_today and d == today:
            continue
        if d.isoformat() in skip_days:
            continue   # 这天已经有过会议纪要 → 跳过
        blocks = _find_blocks_in_day(d)
        for b in blocks:
            info = _is_meeting_like(b)
            if info is None:
                continue
            if _is_processed(d.isoformat(), info["start"], info["end"]):
                continue
            info["day"] = d.isoformat()
            candidates.append(info)
    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates


def main():
    """CLI:python -m meeting_detector [--days N]"""
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    cands = find_candidates(lookback_days=args.days)
    if not cands:
        print(f"[OK] 最近 {args.days} 天没有候选会议")
        return
    print(f"[扫描] 最近 {args.days} 天找到 {len(cands)} 个会议候选\n")
    for c in cands:
        print(f"  📍 {c['day']} {c['start'][11:16]}-{c['end'][11:16]} "
              f"({c['duration_min']:.0f} 分 · {c['segments']} 段 · "
              f"{c['total_chars']} 字 · score={c['score']})")
        spk = ", ".join(f"{k}:{v}" for k, v in c["speakers"].items())
        print(f"     说话人:{spk}  关键词命中:{c['kw_hits']}")
        print(f"     片段:{c['snippet'][:100]}")
        print()


if __name__ == "__main__":
    main()
