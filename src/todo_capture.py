"""把用户口述的今日安排整理成可确认、可排序的执行计划。"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass


_LEADING_FILLER = re.compile(
    r"^(?:我(?:今天|今日)?(?:要|想|得|需要|计划|打算|准备|安排)?|"
    r"今天(?:要|需要|计划|安排)?|今日(?:要|需要|计划|安排)?|"
    r"然后|再|还要|另外|以及|并且|还有|先|请帮我)\s*"
)
_TOP_LEVEL = re.compile(
    r"^\s*(?:\d+[.、．)]|[一二三四五六七八九十]+[.、．)])\s*(.+?)\s*$"
)
_SUB_LEVEL = re.compile(
    r"^\s*(?:[（(]?[a-zA-Z][）).、．]|[（(][一二三四五六七八九十]+[）)])\s*(.+?)\s*$"
)


@dataclass(frozen=True, slots=True)
class PlannedTask:
    title: str
    priority: str
    start_time: str
    end_time: str
    duration_minutes: int
    reason: str
    fixed: bool = False

    @property
    def planned_window(self) -> str:
        if self.start_time and self.end_time:
            return f"{self.start_time}-{self.end_time}"
        return ""


def _clean_item(value: str) -> str:
    item = str(value or "").strip(" -—：:，,。；;！!？?\t")
    previous = None
    while item and item != previous:
        previous = item
        item = _LEADING_FILLER.sub("", item).strip()
    item = re.sub(r"\s+", " ", item).strip("，,。；;！!？?")
    item = item.replace("尽可能多拍一些", "集中拍摄")
    item = re.sub(
        r"^确认如果今天投放短视频，?如何设置才能不让它在主页显示$",
        "确认短视频投放设置，确保投放内容不在主页显示",
        item,
    )
    meeting_purchase = re.search(
        r"(?:晚上)?(?:我们)?内部分享.*?(?:我要|需要)(?:去)?买(?:一个)?腾讯会议(?:的)?账号",
        item,
    )
    if meeting_purchase:
        item = "购买腾讯会议账号（用于今晚内部分享）"
    item = re.sub(r"^把脚本再打磨一遍$", "打磨明天直播脚本", item)
    item = re.sub(r"^再?熟悉一下明天的脚本$", "熟悉明天直播脚本", item)
    item = re.sub(
        r"^研究一下短视频怎么发（?不在主页显示，但能发出去）?$",
        "研究短视频发布设置：不在主页显示但可正常发布",
        item,
    )
    item = re.sub(r"^研究一下\s*", "研究", item)
    return item


def _combine_heading(heading: str, child: str) -> str:
    heading = _clean_item(heading)
    child = _clean_item(child)
    if not heading:
        return child
    if not child:
        return heading
    if "明天直播" in heading:
        if "脚本" in child:
            return child.replace("明天的脚本", "明天直播脚本")
        return child
    if "账号" in heading and re.match(r"^(?:重新)?注册(?:一个|账号)?", child):
        tail = re.sub(r"^(?:重新)?注册(?:一个|账号)?\s*", "", child)
        tail = re.sub(r"^[，,]\s*", "", tail)
        tail = re.sub(r"以便继续(.+)$", r"恢复\1", tail)
        return f"重新注册 {heading}" + (f"，{tail}" if tail else "")
    shared_terms = (
        "账号", "短视频", "投放", "拍摄", "客户", "课程", "直播", "内容", "注册"
    )
    if any(term in heading and term in child for term in shared_terms):
        return child
    return f"{heading}：{child}"


def _outline_tasks(message: str) -> list[str]:
    text = str(message or "").strip()
    if not text:
        return []
    # 兼容把多个编号写在同一行的情况。
    text = re.sub(r"(?<!^)\s+(?=\d+[.、．)]\s*)", "\n", text)
    raw_lines = [line.strip() for line in re.split(r"[\r\n；;。！？]+", text) if line.strip()]
    sections: list[tuple[str, list[str]]] = []
    loose: list[str] = []
    current_heading = ""
    current_children: list[str] = []

    def flush() -> None:
        nonlocal current_heading, current_children
        if current_heading:
            sections.append((current_heading, list(current_children)))
        current_heading = ""
        current_children = []

    for line in raw_lines:
        top = _TOP_LEVEL.match(line)
        if top:
            flush()
            current_heading = top.group(1)
            continue
        sub = _SUB_LEVEL.match(line)
        if sub:
            if current_heading:
                current_children.append(sub.group(1))
            else:
                loose.append(sub.group(1))
            continue
        if current_heading:
            current_children.append(line)
        else:
            loose.append(line)
    flush()

    tasks: list[str] = []
    for line in loose:
        # 普通口述按行动承接词和顿号拆分；含逗号的完整描述则尽量保留。
        expanded = re.sub(r"(?:然后|再|还要|另外|以及|并且|还有)\s*", "\n", line)
        expanded = re.sub(r"、", "\n", expanded)
        expanded = re.sub(
            r"[，,]\s*(?=(?:下午|上午|早上|晚上|中午|先|把|给|去|跟|和|完成|处理|整理|联系|回复|发布|录制))",
            "\n",
            expanded,
        )
        tasks.extend(_clean_item(part) for part in expanded.splitlines())
    for heading, children in sections:
        if children:
            tasks.extend(_combine_heading(heading, child) for child in children)
        else:
            tasks.append(_clean_item(heading))
    return [task for task in tasks if len(task) >= 2]


def extract_today_tasks(message: str, *, limit: int = 12) -> list[str]:
    """提取并合并标题/子任务，保留为向后兼容的纯文本列表。"""
    tasks: list[str] = []
    seen: set[str] = set()
    for task in _outline_tasks(message):
        if task in seen:
            continue
        seen.add(task)
        tasks.append(task)
        if len(tasks) >= max(1, limit):
            break
    return tasks


_FIXED_EVENT_RANGE = re.compile(
    r"(?P<label>(?:我们)?[^，。；;\n]{0,18}?(?:内部分享|分享|会议|直播|上课|见面|培训|通话|吃饭|忙|有事))"
    r"(?:大概)?(?:是)?(?:安排)?(?:在)?(?P<period>晚上|下午|上午|早上|中午)?(?:的)?\s*"
    r"(?P<start>\d{1,2})\s*点(?:钟)?\s*(?:到|至|—|-|~|～)\s*"
    r"(?:(?P<end_period>晚上|下午|上午|早上|中午)?(?:的)?\s*)?"
    r"(?P<end>\d{1,2})\s*点(?:钟)?"
)
_FIXED_EVENT_TIME_FIRST = re.compile(
    r"(?P<period>晚上|下午|上午|早上|中午)?(?:的)?\s*"
    r"(?P<start>\d{1,2})\s*点(?:钟)?\s*(?:到|至|—|-|~|～)\s*"
    r"(?:(?P<end_period>晚上|下午|上午|早上|中午)?(?:的)?\s*)?"
    r"(?P<end>\d{1,2})\s*点(?:钟)?\s*"
    r"(?P<label>(?:我|我们)?[^，。；;\n]{0,18}?(?:内部分享|分享|会议|直播|上课|见面|培训|通话|吃饭|忙|有事))"
)


def _clock_hour(hour: int, period: str, *, after: int | None = None) -> int:
    value = max(0, min(23, int(hour)))
    if period in {"下午", "晚上"} and value < 12:
        value += 12
    elif period == "中午" and value < 11:
        value += 12
    elif period in {"上午", "早上"} and value == 12:
        value = 0
    if after is not None and value <= after and not period and value < 12:
        value += 12
    return min(value, 23)


def _event_title(label: str) -> str:
    text = re.sub(r"^(?:我|我们)", "", str(label or "").strip())
    text = text.replace("的", "").strip()
    if "内部" in text and "分享" in text:
        return "内部分享"
    if text in {"我忙", "忙", "我有事", "有事"}:
        return "已安排事项"
    return text or "已安排事项"


def extract_fixed_events(message: str) -> list[PlannedTask]:
    """提取用户明确说出的忙碌时间，作为不可重叠的固定事件。"""
    events: list[PlannedTask] = []
    matches = [
        *list(_FIXED_EVENT_RANGE.finditer(str(message or ""))),
        *list(_FIXED_EVENT_TIME_FIRST.finditer(str(message or ""))),
    ]
    seen_windows: set[tuple[int, int, str]] = set()
    for match in sorted(matches, key=lambda item: item.start()):
        period = str(match.group("period") or "")
        start_hour = _clock_hour(int(match.group("start")), period)
        end_period = str(match.group("end_period") or period)
        end_hour = _clock_hour(
            int(match.group("end")), end_period, after=start_hour
        )
        if end_hour <= start_hour:
            continue
        title = _event_title(match.group("label"))
        signature = (start_hour, end_hour, title)
        if signature in seen_windows:
            continue
        seen_windows.add(signature)
        events.append(
            PlannedTask(
                title=title,
                priority="固定",
                start_time=f"{start_hour:02d}:00",
                end_time=f"{end_hour:02d}:00",
                duration_minutes=(end_hour - start_hour) * 60,
                reason="已明确占用时间，不安排其他任务",
                fixed=True,
            )
        )
    return events


def _remove_fixed_event_phrases(message: str) -> str:
    text = _FIXED_EVENT_RANGE.sub("", str(message or ""))
    return _FIXED_EVENT_TIME_FIRST.sub("", text)


def _priority_for(title: str) -> tuple[str, int, str]:
    score = 0
    reason = "今天可推进事项"
    if re.search(r"必须|截止|立刻|马上|紧急|今天一定", title):
        score += 8
        reason = "有明确的今天节点"
    tentative_account_research = bool(
        re.search(r"研究.*账号.*(?:不太清楚|不清楚|怎么)", title)
    )
    if (
        re.search(r"注册|登录|账号|恢复|阻塞|才能|以便继续", title)
        and not tentative_account_research
    ):
        score += 6
        reason = "会阻塞后续事项，先处理"
    if re.search(r"确认.*设置|设置.*确认|规则|权限|主页显示", title):
        score += 5
        reason = "先确认规则，避免后续返工"
    if re.search(r"客户|付款|合同|会议|直播", title):
        score += 3
        if reason == "今天可推进事项":
            reason = "涉及对外协作或承诺"
    if re.search(r"发布|投放", title):
        score += 2
    if re.search(r"明天直播", title):
        score += 2
        if reason == "今天可推进事项":
            reason = "服务于已确定的近期安排"
    if re.search(r"今晚内部分享|腾讯会议账号", title):
        score += 4
        reason = "必须在今晚固定安排前完成"
    if re.search(r"拍摄|录制|批量|集中", title):
        score += 2
        if reason == "今天可推进事项":
            reason = "适合安排完整时间块集中处理"
    priority = "P0" if score >= 7 else "P1" if score >= 3 else "P2"
    return priority, score, reason


def _duration_for(title: str) -> int:
    if re.search(r"研究.*账号", title):
        return 40
    if re.search(r"打磨.*脚本|拍摄|录制|剪辑|写稿|课件|PPT", title, re.IGNORECASE):
        return 60
    if re.search(r"熟悉.*脚本", title):
        return 30
    if re.search(r"注册|登录|回复|联系|确认|设置|付款|购买|腾讯会议", title):
        return 20
    if re.search(r"整理|分析|规划|复盘", title):
        return 40
    return 30


def _round_up_10(value: dt.datetime) -> dt.datetime:
    value = value.replace(second=0, microsecond=0)
    remainder = value.minute % 10
    if remainder:
        value += dt.timedelta(minutes=10 - remainder)
    return value


def _busy_intervals(day: dt.date, events: list[PlannedTask]) -> list[tuple[dt.datetime, dt.datetime]]:
    intervals = [
        (dt.datetime.combine(day, dt.time(12, 0)), dt.datetime.combine(day, dt.time(13, 30))),
        (dt.datetime.combine(day, dt.time(18, 0)), dt.datetime.combine(day, dt.time(19, 30))),
    ]
    for event in events:
        try:
            start_hour, start_minute = map(int, event.start_time.split(":"))
            end_hour, end_minute = map(int, event.end_time.split(":"))
            intervals.append(
                (
                    dt.datetime.combine(day, dt.time(start_hour, start_minute)),
                    dt.datetime.combine(day, dt.time(end_hour, end_minute)),
                )
            )
        except (TypeError, ValueError):
            continue
    return sorted(intervals)


def _advance_past_busy(cursor: dt.datetime, busy: list[tuple[dt.datetime, dt.datetime]]) -> dt.datetime:
    result = cursor
    changed = True
    while changed:
        changed = False
        for start, end in busy:
            if start <= result < end:
                result = end
                changed = True
                break
    return result


def _available_before_busy(cursor: dt.datetime, busy: list[tuple[dt.datetime, dt.datetime]]) -> int | None:
    future = [start for start, _end in busy if start > cursor]
    if not future:
        return None
    return max(0, int((min(future) - cursor).total_seconds() // 60))


def plan_today_tasks(
    message: str,
    *,
    now: dt.datetime | None = None,
    occupied_until: str = "",
    limit: int = 12,
) -> list[PlannedTask]:
    """按依赖、优先级和当前时间生成今日执行顺序。"""
    current = now or dt.datetime.now()
    start = _round_up_10(current)
    if occupied_until:
        try:
            hour, minute = map(int, occupied_until.split(":", 1))
            occupied = dt.datetime.combine(current.date(), dt.time(hour, minute))
            if occupied > start:
                start = occupied
        except (TypeError, ValueError):
            pass

    events = extract_fixed_events(message)
    task_message = _remove_fixed_event_phrases(message)
    ranked: list[tuple[int, int, str, str, int, str]] = []
    for index, title in enumerate(extract_today_tasks(task_message, limit=limit)):
        priority, score, reason = _priority_for(title)
        ranked.append((-score, index, title, priority, _duration_for(title), reason))
    ranked.sort(key=lambda item: (item[0], item[1]))

    plans: list[PlannedTask] = []
    cursor = start
    busy = _busy_intervals(current.date(), events)
    remaining = list(ranked)
    work_end = dt.datetime.combine(current.date(), dt.time(23, 0))
    while remaining:
        cursor = _advance_past_busy(cursor, busy)
        if cursor >= work_end:
            break
        available = _available_before_busy(cursor, busy)
        candidate_index = 0
        if available is not None:
            fitting = [
                index for index, item in enumerate(remaining)
                if item[4] <= available
            ]
            if not fitting:
                future_busy = [(s, e) for s, e in busy if s > cursor]
                if future_busy:
                    cursor = min(future_busy)[1]
                    continue
            else:
                candidate_index = fitting[0]
        _negative_score, _index, title, priority, duration, reason = remaining.pop(candidate_index)
        end = cursor + dt.timedelta(minutes=duration)
        plans.append(
            PlannedTask(
                title=title,
                priority=priority,
                start_time=cursor.strftime("%H:%M"),
                end_time=end.strftime("%H:%M"),
                duration_minutes=duration,
                reason=reason,
            )
        )
        cursor = end

    # 今天合理工作时段放不下的事项明确顺延，不把深夜排满。
    tomorrow_cursor = dt.datetime.combine(current.date() + dt.timedelta(days=1), dt.time(9, 30))
    for _negative_score, _index, title, priority, duration, reason in remaining:
        end = tomorrow_cursor + dt.timedelta(minutes=duration)
        plans.append(
            PlannedTask(
                title=title,
                priority=priority,
                start_time=f"明天 {tomorrow_cursor:%H:%M}",
                end_time=f"明天 {end:%H:%M}",
                duration_minutes=duration,
                reason=f"今日可用时间不足，建议顺延；{reason}",
            )
        )
        tomorrow_cursor = end

    plans.extend(events)

    def chronological(item: PlannedTask) -> tuple[int, str, int]:
        tomorrow = item.start_time.startswith("明天 ")
        clock = item.start_time.replace("明天 ", "")
        priority_rank = {"固定": 0, "P0": 1, "P1": 2, "P2": 3}.get(item.priority, 4)
        return (1 if tomorrow else 0, clock, priority_rank)

    return sorted(plans, key=chronological)
