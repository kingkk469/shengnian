from __future__ import annotations

import datetime as dt
import unittest

from todo_capture import extract_fixed_events, extract_today_tasks, plan_today_tasks


class TodoCaptureTests(unittest.TestCase):
    def test_extracts_common_spoken_plan(self):
        self.assertEqual(
            extract_today_tasks("我今天要给客户回消息、整理课程 PPT，然后下午跟小王确认直播流程"),
            ["给客户回消息", "整理课程 PPT", "下午跟小王确认直播流程"],
        )

    def test_merges_outline_headings_and_subtasks(self):
        message = """1. X 账号
重新注册一个，以便继续发布内容
2. 短视频投放与拍摄
(a) 确认如果今天投放短视频，如何设置才能不让它在主页显示
(b) 尽可能多拍一些用于投放的短视频"""
        self.assertEqual(
            extract_today_tasks(message),
            [
                "重新注册 X 账号，恢复发布内容",
                "确认短视频投放设置，确保投放内容不在主页显示",
                "集中拍摄用于投放的短视频",
            ],
        )

    def test_plans_blockers_first_from_current_time(self):
        message = """1. X 账号
重新注册一个，以便继续发布内容
2. 短视频投放与拍摄
(a) 确认如果今天投放短视频，如何设置才能不让它在主页显示
(b) 尽可能多拍一些用于投放的短视频"""
        plans = plan_today_tasks(message, now=dt.datetime(2026, 8, 9, 16, 38))
        self.assertEqual([item.priority for item in plans], ["P0", "P0", "P1"])
        self.assertEqual(plans[0].start_time, "16:40")
        self.assertEqual(plans[0].title, "重新注册 X 账号，恢复发布内容")
        self.assertEqual(plans[2].start_time, "19:30")

    def test_keeps_ambiguous_sentence_as_one_task(self):
        self.assertEqual(
            extract_today_tasks("今天把新课的结构想清楚"),
            ["把新课的结构想清楚"],
        )

    def test_ignores_empty_input(self):
        self.assertEqual(extract_today_tasks("   "), [])

    def test_fixed_busy_window_blocks_schedule_and_overflow_moves_to_tomorrow(self):
        message = """1. 晚上我们内部分享，我要去买一个腾讯会议的账号。
2. 研究明天直播的事：
(a) 把脚本再打磨一遍
(b) 研究一下短视频怎么发（不在主页显示，但能发出去）
(c) 再熟悉一下明天的脚本
3. 研究一下 X 账号怎么注册（目前不太清楚）
我们内部的分享大概是在晚上的 8 点钟到 10 点钟"""
        events = extract_fixed_events(message)
        self.assertEqual(
            [(event.title, event.start_time, event.end_time) for event in events],
            [("内部分享", "20:00", "22:00")],
        )
        plans = plan_today_tasks(message, now=dt.datetime(2026, 8, 9, 17, 3))
        windows = [(item.title, item.priority, item.planned_window) for item in plans]
        self.assertIn(
            ("购买腾讯会议账号（用于今晚内部分享）", "P0", "17:10-17:30"),
            windows,
        )
        self.assertIn(("内部分享", "固定", "20:00-22:00"), windows)
        self.assertFalse(
            any(
                not item.fixed
                and not item.start_time.startswith("明天 ")
                and item.start_time < "22:00" < item.end_time
                for item in plans
            )
        )

    def test_understands_time_first_busy_phrase(self):
        events = extract_fixed_events("晚上 8 点到 10 点我有事，其他任务帮我往前后排。")
        self.assertEqual(
            [(event.title, event.start_time, event.end_time) for event in events],
            [("已安排事项", "20:00", "22:00")],
        )
