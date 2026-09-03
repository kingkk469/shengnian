"""voice-journal 启动器 · PySide6 版 · Apple 暗色风格。

跑法:
    cd D:\\voice-journal
    .venv\\Scripts\\pythonw.exe src\\launcher.py
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import signal
import sys
import threading
import time
import webbrowser
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal, QObject, QDate
from PySide6.QtGui import QColor, QFont, QPainter, QBrush, QPen, QAction, QKeySequence, QShortcut, QTextCharFormat, QPixmap
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QFrame, QPlainTextEdit, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QSplitter, QListWidget, QListWidgetItem,
    QMessageBox, QDialog, QLineEdit, QComboBox, QFormLayout, QSizePolicy,
    QMenu, QGraphicsDropShadowEffect, QScrollArea, QCheckBox, QSlider,
    QCalendarWidget, QTabWidget, QInputDialog, QDateTimeEdit,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (
    CONFIG, RESOURCE_ROOT, ROOT, configured_obsidian_vault, delete_segments,
    list_history_days, load_speakers, knowledge_dir, note_path, pause_flag,
    read_jsonl, read_recorder_status, save_speakers, transcript_path,
)
from runtime_profile import feature_enabled, is_commercial_mode
from platform_support import (
    RoleLock, configure_qt_environment, locked_role_pid, microphone_permission_hint,
    open_path, pid_exists, source_python,
)
import runtime_profile as _runtime_profile
from brief_presenter import render_today_brief, render_yesterday_review
from brief_sources import (
    pending_hint,
    resolve_today_brief,
    resolve_yesterday_brief,
)
from ui_preferences import (
    FONT_SCALES,
    load_font_scale,
    save_font_scale,
    scale_stylesheet_font_sizes,
)
from todo_capture import plan_today_tasks


# ROOT 是用户数据目录，源码运行环境始终位于 RESOURCE_ROOT。
# 两者在数据迁移到其他磁盘后不再相同，不能再从 ROOT 拼虚拟环境路径。
VENV_PY = source_python(RESOURCE_ROOT)
SRC = RESOURCE_ROOT / "src"
MOMENTS_ASSET_DIR = RESOURCE_ROOT / "prompts" / "moments-workflow"
_moments_workflow_override = os.environ.get(
    "VOICE_JOURNAL_MOMENTS_WORKFLOW_DIR", ""
).strip()
MOMENTS_WORKFLOW_DIR = (
    Path(_moments_workflow_override).expanduser().resolve()
    if _moments_workflow_override
    else (ROOT / "moments-workflow" if is_commercial_mode() else MOMENTS_ASSET_DIR)
)
_moments_script_override = os.environ.get("VOICE_JOURNAL_MOMENTS_SCRIPT", "").strip()
MOMENTS_SCRIPT = (
    Path(_moments_script_override).expanduser().resolve()
    if _moments_script_override
    else MOMENTS_ASSET_DIR / "scripts" / "generate_moments_from_journal.py"
)
MOMENTS_OUTPUT_LAYOUT = os.environ.get(
    "VOICE_JOURNAL_MOMENTS_OUTPUT_LAYOUT", "flat"
).strip().lower()
MOMENTS_STATUS_PATH = MOMENTS_WORKFLOW_DIR / "运行输出" / "朋友圈发布状态.json"
MOMENTS_PUBLISH_RECORD_PATH = MOMENTS_WORKFLOW_DIR / "05-发布记录.md"
# 子进程不弹控制台黑窗:launcher 是 pythonw(无窗口),子进程跑 python.exe 默认会闪黑框
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
APP_VERSION = "0.3.0"


def _role_command(role: str, *args: str) -> list[str]:
    """源码模式调用脚本；冻结模式复用同一个签名 EXE 的内部 role。"""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--role", role, *map(str, args)]
    if role == "recorder":
        return [str(VENV_PY), str(SRC / "recorder.py"), *map(str, args)]
    if role == "transcriber":
        return [str(VENV_PY), str(SRC / "transcriber.py"), *map(str, args)]
    if role == "daily-summary":
        return [str(VENV_PY), str(SRC / "daily_summary.py"), *map(str, args)]
    if role == "ingest-url":
        return [str(VENV_PY), "-X", "utf8", str(SRC / "ingest_url.py"), *map(str, args)]
    if role == "moments":
        return [str(VENV_PY), str(MOMENTS_SCRIPT), *map(str, args)]
    raise ValueError(f"未知内部 role: {role}")


def _friendly_moments_error(error: str) -> str:
    """把子进程的技术日志换成用户可执行的错误说明。"""
    raw = (error or "").strip()
    lowered = raw.lower()
    network_markers = (
        "unexpected_eof_while_reading",
        "handshake operation timed out",
        "ssl handshake",
        "urlopen error",
        "无法连接账号服务",
        "connection timed out",
        "connecttimeout",
        "readtimeout",
    )
    if any(marker in lowered for marker in network_markers):
        return (
            "暂时无法连接你配置的 AI 服务。\n\n"
            "请检查网络、代理设置和 API 地址后重试。\n\n"
            "本地录音和转写已保留，不会丢失。"
        )
    if "401" in lowered or "unauthorized" in lowered:
        return "API Key 无效或已过期，请检查本机配置后重试。"
    if "429" in lowered or "额度不足" in raw or "token 不足" in lowered:
        return "你的 API 账户额度不足或请求过于频繁，请检查服务商控制台后重试。"
    first = raw.splitlines()[0][:180] if raw else "未知错误"
    return f"朋友圈暂时生成失败：{first}\n\n本地语音资料不会丢失，可稍后重试。"


def _decode_moments_output(raw: bytes) -> str:
    """Decode frozen child output from either UTF-8 or the Windows GBK code page."""
    for encoding in ("utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


# ============================================================
# Claude Light · 暖白底 + 黑字 + Claude 橙口音
# 致敬 claude.ai 的设计语言
# ============================================================
QSS = """
* {
    font-family: "Inter", "PingFang SC", "Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei";
    font-size: 15px;
    font-weight: 500;
    outline: none;
}

/* ─────────── 容器（背景图由 QPalette 注入，不在 QSS 里画） ─────────── */
QDialog {
    background-color: #faf9f5;
}

/*
 * Windows 深色系统主题会把 QMessageBox 正文的系统调色板设为白色。
 * 声年又把对话框背景固定成暖白色，若不显式覆盖，确认提示就会变成白底白字。
 */
QMessageBox {
    background-color: #faf9f5;
}

QMessageBox QLabel {
    background-color: transparent;
    color: #1c1b18;
}

QInputDialog {
    background-color: #faf9f5;
}

QInputDialog QLabel {
    background-color: transparent;
    color: #1c1b18;
}

/* ─────────── 顶部栏 · 半透明覆盖在背景图上 ─────────── */
QFrame#topbar {
    background-color: rgba(250, 249, 245, 0.92);
    border-bottom: 1px solid rgba(28, 27, 24, 0.08);
}

QLabel#brandTitle {
    color: #1c1b18;
    font-family: "Inter SemiBold", "Inter", "Segoe UI Semibold", "Segoe UI";
    font-size: 21px;
    font-weight: 600;
    letter-spacing: 0.3px;
}

QLabel#brandSub {
    color: #5a564e;
    font-size: 11px;
    letter-spacing: 4px;
    font-weight: 500;
}

QLabel#clock {
    color: #5a564e;
    font-family: "Cascadia Mono", "Consolas";
    font-size: 13px;
    letter-spacing: 0.5px;
}

QLabel#signalText {
    color: #3a3833;
    font-size: 14px;
}

/* ─────────── 警报横幅 ─────────── */
QFrame#alertBanner {
    background-color: rgba(176, 64, 48, 0.16);
    border: 1px solid rgba(176, 64, 48, 0.42);
    border-radius: 6px;
    margin: 4px 20px;
}

QLabel#alertText {
    color: #993828;
    font-size: 13px;
    font-weight: 600;
    padding: 4px 8px;
}

/* ─────────── 卡片层（半透明覆盖在背景图上，模拟磨砂玻璃） ─────────── */
QFrame#statusCard {
    background-color: rgba(255, 255, 255, 0.94);
    border: 1px solid rgba(28, 27, 24, 0.08);
    border-radius: 10px;
}

QFrame#infoPanel {
    background-color: rgba(255, 255, 255, 0.94);
    border: 1px solid rgba(28, 27, 24, 0.08);
    border-radius: 10px;
}

/* ─────────── 按钮：默认 ─────────── */
QPushButton {
    background-color: #ffffff;
    color: #1c1b18;
    border: 1px solid rgba(28, 27, 24, 0.08);
    border-radius: 8px;
    padding: 8px 16px;
    font-size: 14.5px;
    font-weight: 500;
}

QPushButton:hover {
    background-color: #f5f3ed;
    color: #1c1b18;
    border-color: rgba(28, 27, 24, 0.20);
}

QPushButton:pressed {
    background-color: #ebe7df;
}

QPushButton:disabled {
    background-color: #ffffff;
    color: #b8b3a8;
    border-color: rgba(28, 27, 24, 0.04);
}

/* ─────────── 主按钮 · 柔和陶土 ─────────── */
QPushButton#primaryBtn {
    background-color: #cc785c;
    color: #faf9f5;
    border: none;
    border-radius: 8px;
    font-weight: 700;
    font-size: 13px;
    padding: 8px 18px;
}

QPushButton#primaryBtn:hover {
    background-color: #d88a6f;
}

QPushButton#primaryBtn:pressed {
    background-color: #b4664e;
}

QPushButton#primaryBtn:disabled {
    background-color: rgba(204, 120, 92, 0.30);
    color: rgba(42, 39, 34, 0.55);
}

/* ─────────── 危险按钮 ─────────── */
QPushButton#dangerBtn {
    background-color: #ffffff;
    color: #b04030;
    border: 1px solid rgba(176, 64, 48, 0.35);
    border-radius: 8px;
    font-weight: 600;
}

QPushButton#dangerBtn:hover {
    background-color: rgba(176, 64, 48, 0.14);
    color: #993828;
    border-color: rgba(176, 64, 48, 0.60);
}

QPushButton#dangerBtn:pressed {
    background-color: rgba(176, 64, 48, 0.22);
}

/* ─────────── 幽灵按钮 ─────────── */
QPushButton#ghostBtn {
    background-color: transparent;
    color: #5a564e;
    border: none;
    border-radius: 6px;
    padding: 3px 10px;
    font-size: 11px;
}

QPushButton#ghostBtn:hover {
    color: #cc785c;
    background-color: rgba(204, 120, 92, 0.10);
}

QPushButton#ghostBtn:pressed {
    background-color: rgba(204, 120, 92, 0.18);
}

/* ─────────── 今日统计大数字 ─────────── */
QLabel#todayNum {
    color: #1c1b18;
    font-family: "Inter SemiBold", "Inter", "Segoe UI Semibold", "Segoe UI";
    font-size: 28px;
    font-weight: 700;
    letter-spacing: 0.5px;
}

/* ─────────── 文本编辑区 · 跟卡片同底，无黑框 ─────────── */
QPlainTextEdit {
    background-color: transparent;
    color: #1c1b18;
    border: none;
    padding: 8px 4px;
    font-size: 15.5px;
    line-height: 1.75;
    selection-background-color: rgba(204, 120, 92, 0.45);
    selection-color: #1c1b18;
}

QPlainTextEdit:focus {
    background-color: rgba(28, 27, 24, 0.04);
    border: none;
}

/* ─────────── 复选框 ─────────── */
QCheckBox {
    color: #1c1b18;
    font-size: 15.5px;
    spacing: 10px;
    padding: 5px 0;
}

QCheckBox::indicator {
    width: 18px;
    height: 18px;
    border: 2px solid #a8a39a;
    border-radius: 5px;
    background-color: #ffffff;
}

QCheckBox::indicator:hover {
    border-color: #cc785c;
    background-color: rgba(204, 120, 92, 0.08);
}

QCheckBox::indicator:checked {
    background-color: #cc785c;
    border-color: #b4664e;
    image: none;
}

QCheckBox:checked {
    color: #8e8a82;
    text-decoration: line-through;
}

/* ─────────── 已完成标题 ─────────── */
QLabel#doneHeader {
    color: #8e8a82;
    font-size: 12px;
    letter-spacing: 2px;
    padding: 4px 0 2px 0;
    font-weight: 600;
}

/* ─────────── 滚动区 ─────────── */
QScrollArea {
    background-color: transparent;
    border: none;
}

QScrollArea > QWidget > QWidget {
    background-color: transparent;
}

/* ─────────── 底部状态条 ─────────── */
QLabel#bottomStatus {
    color: #5a564e;
    font-size: 12.5px;
}

/* ─────────── 滚动条 ─────────── */
QScrollBar:vertical {
    background: transparent;
    width: 8px;
    margin: 2px;
}
QScrollBar::handle:vertical {
    background: rgba(28, 27, 24, 0.20);
    min-height: 32px;
    border-radius: 3px;
}
QScrollBar::handle:vertical:hover {
    background: rgba(204, 120, 92, 0.55);
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal {
    background: transparent;
    height: 8px;
    margin: 2px;
}
QScrollBar::handle:horizontal {
    background: rgba(28, 27, 24, 0.20);
    min-width: 32px;
    border-radius: 3px;
}
QScrollBar::handle:horizontal:hover {
    background: rgba(204, 120, 92, 0.55);
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }

/* ─────────── 表格 ─────────── */
QTableWidget {
    background-color: #ffffff;
    color: #1c1b18;
    border: 1px solid rgba(28, 27, 24, 0.08);
    border-radius: 8px;
    gridline-color: rgba(28, 27, 24, 0.06);
    selection-background-color: rgba(204, 120, 92, 0.24);
    selection-color: #1c1b18;
}

QHeaderView::section {
    background-color: #f5f3ed;
    color: #5a564e;
    padding: 8px 10px;
    border: none;
    border-right: 1px solid rgba(28, 27, 24, 0.05);
    border-bottom: 1px solid rgba(204, 120, 92, 0.20);
    font-weight: 600;
    letter-spacing: 0.8px;
    font-size: 11px;
    text-transform: uppercase;
}

QTableWidget::item {
    padding: 7px 8px;
    border-bottom: 1px solid rgba(28, 27, 24, 0.05);
}

QTableWidget::item:selected {
    background-color: rgba(204, 120, 92, 0.20);
    color: #1c1b18;
}

/* ─────────── 列表 ─────────── */
QListWidget {
    background-color: #ffffff;
    color: #1c1b18;
    border: 1px solid rgba(28, 27, 24, 0.08);
    border-radius: 8px;
    padding: 4px;
}
QListWidget::item {
    padding: 9px 12px;
    border-radius: 5px;
}
QListWidget::item:hover {
    background-color: rgba(28, 27, 24, 0.05);
}
QListWidget::item:selected {
    background-color: rgba(204, 120, 92, 0.24);
    color: #1c1b18;
}

/* ─────────── 输入框 / 下拉 ─────────── */
QLineEdit, QComboBox {
    background-color: #ffffff;
    color: #1c1b18;
    border: 1px solid rgba(28, 27, 24, 0.08);
    border-radius: 7px;
    padding: 7px 11px;
    font-size: 13px;
}
QLineEdit:focus, QComboBox:focus {
    background-color: #fafafa;
    border: 1px solid rgba(204, 120, 92, 0.60);
}
QLineEdit::placeholder { color: #b8b3a8; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView {
    background-color: #ffffff;
    color: #1c1b18;
    border: 1px solid rgba(28, 27, 24, 0.08);
    selection-background-color: rgba(204, 120, 92, 0.30);
    padding: 4px;
    border-radius: 7px;
}

/* ─────────── 菜单 ─────────── */
QMenu {
    background-color: #ffffff;
    color: #1c1b18;
    border: 1px solid rgba(255, 255, 255, 0.10);
    border-radius: 8px;
    padding: 5px;
}
QMenu::item { padding: 7px 22px; border-radius: 5px; }
QMenu::item:selected {
    background-color: rgba(204, 120, 92, 0.30);
    color: #1c1b18;
}
QMenu::separator {
    height: 1px;
    background: rgba(28, 27, 24, 0.10);
    margin: 4px 8px;
}

/* ─────────── 分隔线 ─────────── */
QFrame#divider {
    background-color: rgba(28, 27, 24, 0.06);
    max-height: 1px;
    min-height: 1px;
    border: none;
}

/* ─────────── 工具提示 ─────────── */
QToolTip {
    background-color: #ffffff;
    color: #1c1b18;
    border: 1px solid rgba(28, 27, 24, 0.10);
    border-radius: 6px;
    padding: 5px 9px;
    font-size: 12px;
}

/* ─────────── 滑杆 (顶栏透明度调节) ─────────── */
QSlider::groove:horizontal {
    background: rgba(28, 27, 24, 0.12);
    height: 4px;
    border-radius: 2px;
}
QSlider::sub-page:horizontal {
    background: #cc785c;
    height: 4px;
    border-radius: 2px;
}
QSlider::handle:horizontal {
    background: #ffffff;
    border: 1.5px solid #cc785c;
    width: 14px;
    height: 14px;
    margin: -6px 0;
    border-radius: 8px;
}
QSlider::handle:horizontal:hover {
    background: #fff8f3;
    border-color: #b4664e;
}
"""


# ============================================================
# 已办事项列表组件
# ============================================================
class DoneWidget(QScrollArea):
    """展示已办条目，每条右侧有「还原」和「删除」按钮。"""

    MAX_VISIBLE = 10

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QScrollArea.NoFrame)   # 去掉 sunken 凹槽边框（模糊源）
        self.verticalScrollBar().setSingleStep(20)  # 每格 20px，滚动更平滑
        self._md_path: Path | None = None
        self._done: list[dict] = []
        self._todo_widget = None  # 还原时通知待办区刷新

        self._container = QWidget()
        self._container.setStyleSheet("background: transparent;")
        self._vbox = QVBoxLayout(self._container)
        self._vbox.setContentsMargins(4, 4, 4, 4)
        self._vbox.setSpacing(2)
        self._vbox.addStretch(1)
        self.setWidget(self._container)

    def set_todo_widget(self, w):
        self._todo_widget = w

    def load(self, md_path: Path):
        self._md_path = md_path
        self._parse()
        self._render()

    def refresh(self):
        if self._md_path:
            self.load(self._md_path)

    def _parse(self):
        import re
        self._done = []
        if not self._md_path or not self._md_path.exists():
            return
        lines = self._md_path.read_text(encoding="utf-8").splitlines()
        section = None
        for line in lines:
            s = line.strip()
            if re.search(r"^##.*已完成", s):
                section = "done"
            elif s.startswith("##"):
                section = None
            elif section == "done" and (s.startswith("- [x]") or s.startswith("- [X]")):
                text = s[5:].strip()
                # 提取完成日期
                m_done = re.search(r"\(完成[：:](.+?)\)", text)
                done_date = m_done.group(1).strip() if m_done else ""
                # 去掉注解只保留正文
                clean = re.sub(r"\s*\(来源[：:]?.+?\)", "", text)
                clean = re.sub(r"\s*\(完成[：:].+?\)", "", clean).strip()
                clean = re.sub(r"\s*\(priority[：:]\s*(?:P[0-2]|固定)\s*\)", "", clean, flags=re.I)
                clean = re.sub(r"\s*\(planned[：:]\s*(?:明天 )?\d{2}:\d{2}-(?:明天 )?\d{2}:\d{2}\s*\)", "", clean, flags=re.I)
                clean = re.sub(r"\s*\(duration[：:]\s*\d+m\s*\)", "", clean, flags=re.I).strip()
                self._done.append({"text": clean, "done_date": done_date, "raw": line})

        # 启动器只展示最近记录，完整历史仍保留在 Markdown 里。
        self._done = self._done[:self.MAX_VISIBLE]

    def _render(self):
        while self._vbox.count() > 1:
            item = self._vbox.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not self._done:
            lbl = QLabel("暂无已完成事项")
            lbl.setStyleSheet("color:#8e8a82; font-size:14px; font-style:italic; padding:8px 4px;")
            self._vbox.insertWidget(0, lbl)
            return

        for i, item in enumerate(self._done):
            row = QWidget()
            row.setStyleSheet("background: transparent;")
            hl = QHBoxLayout(row)
            hl.setContentsMargins(0, 2, 0, 2)
            hl.setSpacing(5)

            check_lbl = QLabel("✓")
            check_lbl.setFixedWidth(14)
            check_lbl.setStyleSheet("color:#8ca36a; font-size:11px;")

            text_lbl = QLabel(item["text"])
            text_lbl.setStyleSheet(
                "color:#8e8a82; font-size:14px; text-decoration: line-through;")
            text_lbl.setWordWrap(True)

            # 还原按钮
            restore_btn = QPushButton("还原")
            restore_btn.setFixedWidth(38)
            restore_btn.setStyleSheet(
                "QPushButton{color:#8ca36a; border:1px solid rgba(140,163,106,0.32);"
                "border-radius:4px; padding:1px 4px; font-size:10px; background:transparent;}"
                "QPushButton:hover{color:#a3b87f; border-color:rgba(140,163,106,0.65);}")
            restore_btn.clicked.connect(lambda _, it=item: self._restore_item(it))

            # 删除按钮
            del_btn = QPushButton("删除")
            del_btn.setFixedWidth(38)
            del_btn.setStyleSheet(
                "QPushButton{color:#b04030; border:1px solid rgba(168,92,76,0.32);"
                "border-radius:4px; padding:1px 4px; font-size:10px; background:transparent;}"
                "QPushButton:hover{color:#993828; border-color:rgba(168,92,76,0.65);}")
            del_btn.clicked.connect(lambda _, it=item: self._delete_item(it))

            hl.addWidget(check_lbl)
            hl.addWidget(text_lbl, 1)
            hl.addWidget(restore_btn)
            hl.addWidget(del_btn)
            self._vbox.insertWidget(i, row)

    def _restore_item(self, item: dict):
        """将已办条目还原回待完成区（[x] → [ ]，移回待完成 section）。"""
        if not self._md_path or not self._todo_widget:
            return
        try:
            lines = self._md_path.read_text(encoding="utf-8").splitlines(keepends=True)
            old_raw = item["raw"]

            # 构造还原行：去掉 (完成:...) 注解，[x] 改 [ ]
            import re
            restored = old_raw.replace("- [x]", "- [ ]", 1).replace("- [X]", "- [ ]", 1)
            restored = re.sub(r"\s*\(完成:.+?\)", "", restored.rstrip("\n\r")) + "\n"

            # 从已完成区删除
            new_lines = []
            removed = False
            for ln in lines:
                if not removed and ln.rstrip("\n\r") == old_raw.rstrip("\n\r"):
                    removed = True
                    continue
                new_lines.append(ln)

            # 插回待完成区末尾
            pending_header_idx = None
            for i, ln in enumerate(new_lines):
                if re.search(r"^##.*待完成", ln.strip()) or re.search(r"^##.*待办", ln.strip()):
                    pending_header_idx = i
                    break

            if pending_header_idx is not None:
                # 找到下一个 ## 之前的最后一行，插在那里
                insert_at = pending_header_idx + 1
                while insert_at < len(new_lines) and not new_lines[insert_at].strip().startswith("##"):
                    insert_at += 1
                new_lines.insert(insert_at, restored)
            else:
                new_lines.append(restored)

            self._md_path.write_text("".join(new_lines), encoding="utf-8")
        except Exception:
            pass

        def _refresh_both():
            self.refresh()
            if self._todo_widget:
                self._todo_widget.refresh()
        QTimer.singleShot(150, _refresh_both)

    def _delete_item(self, item: dict):
        """从 MD 文件彻底删除该已办条目。"""
        if not self._md_path:
            return
        try:
            lines = self._md_path.read_text(encoding="utf-8").splitlines(keepends=True)
            new_lines = [ln for ln in lines
                         if ln.rstrip("\n\r") != item["raw"].rstrip("\n\r")]
            self._md_path.write_text("".join(new_lines), encoding="utf-8")
        except Exception:
            pass
        QTimer.singleShot(100, self.refresh)


# ============================================================
# 可交互待办列表组件
# ============================================================
class TodoCaptureDialog(QDialog):
    """把自然语言安排整理为有优先级和时间块的今日执行计划。"""

    tasksConfirmed = Signal(list)

    def __init__(self, parent=None, *, occupied_until: str = ""):
        super().__init__(parent)
        self._occupied_until = occupied_until
        self.setWindowTitle("对话添加今日待办")
        self.resize(620, 540)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        root.setSpacing(10)

        title = QLabel("和声年说说你今天要做什么")
        title.setStyleSheet("font-size:20px; font-weight:700; color:#1c1b18;")
        root.addWidget(title)
        hint = QLabel(
            "例如：\u201c我今天要给客户回消息、整理课程 PPT，下午和小王确认直播流程。\u201d\n"
            "声年会合并标题和子任务，判断阻塞关系，并按当前时间安排 P0/P1/P2；"
            "只有你点击保存后，才会写入本地待办。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#6f6a61; line-height:1.5;")
        root.addWidget(hint)

        self.conversation = QPlainTextEdit()
        self.conversation.setReadOnly(True)
        self.conversation.setMaximumHeight(125)
        self.conversation.setPlainText("声年：告诉我你今天想完成什么，我来帮你整理成可勾选的待办。")
        root.addWidget(self.conversation)

        self.input = QPlainTextEdit()
        self.input.setPlaceholderText("输入今天的安排…")
        self.input.setMaximumHeight(100)
        root.addWidget(self.input)

        parse_button = QPushButton("整理并安排今日任务")
        parse_button.setObjectName("primaryBtn")
        parse_button.clicked.connect(self._parse)
        root.addWidget(parse_button)

        self.task_list = QListWidget()
        self.task_list.setMinimumHeight(150)
        self.task_list.setToolTip("P0 立即处理，P1 今天重点，P2 有余力再做；取消勾选即可不保存。")
        root.addWidget(self.task_list, 1)

        actions = QHBoxLayout()
        self.save_button = QPushButton("保存到今日待办")
        self.save_button.setObjectName("primaryBtn")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self._save)
        cancel_button = QPushButton("取消")
        cancel_button.setObjectName("ghostBtn")
        cancel_button.clicked.connect(self.reject)
        actions.addWidget(self.save_button)
        actions.addStretch(1)
        actions.addWidget(cancel_button)
        root.addLayout(actions)

    def _parse(self):
        message = self.input.toPlainText().strip()
        plans = plan_today_tasks(message, occupied_until=self._occupied_until)
        if not plans:
            QMessageBox.information(self, "还没有任务", "请说说今天准备做什么，声年才能帮你整理。")
            return
        current_time = dt.datetime.now().strftime("%H:%M")
        fixed_windows = [
            f"{plan.start_time}-{plan.end_time} {plan.title}"
            for plan in plans if plan.fixed
        ]
        fixed_text = (
            "我已先锁定固定时间：" + "；".join(fixed_windows) + "。"
            if fixed_windows else ""
        )
        self.conversation.appendPlainText(
            f"\n你：{message}\n\n"
            f"声年：现在是 {current_time}。{fixed_text}"
            "我先安排会阻塞后续的事情，再用空档填入今天重点；今天装不下的事项会明确顺延。"
            f"以下 {len(plans)} 件任务已按建议顺序排列；保存后可以逐项勾选完成。"
        )
        self.task_list.clear()
        for plan in plans:
            prefix = f"{plan.priority} · {plan.start_time}-{plan.end_time} · "
            item = QListWidgetItem(prefix + plan.title)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEditable)
            item.setCheckState(Qt.Checked)
            item.setData(Qt.UserRole, {
                "title": plan.title,
                "priority": plan.priority,
                "planned": plan.planned_window,
                "duration": plan.duration_minutes,
                "reason": plan.reason,
                "prefix": prefix,
            })
            item.setToolTip(f"{plan.reason} · 建议用时 {plan.duration_minutes} 分钟")
            self.task_list.addItem(item)
        self.save_button.setEnabled(True)

    def _save(self):
        tasks = []
        for i in range(self.task_list.count()):
            item = self.task_list.item(i)
            if item.checkState() != Qt.Checked or not item.text().strip():
                continue
            payload = dict(item.data(Qt.UserRole) or {})
            display = item.text().strip()
            prefix = str(payload.pop("prefix", "") or "")
            payload["title"] = display[len(prefix):].strip() if prefix and display.startswith(prefix) else display
            tasks.append(payload)
        if not tasks:
            QMessageBox.information(self, "未选择任务", "请至少选择一项要保存的待办。")
            return
        self.tasksConfirmed.emit(tasks)
        self.accept()


class TodoWidget(QScrollArea):
    """
    从 待办总览.md 读取待办条目，展示为可勾选列表。
    勾选后自动：① 移动到已办区  ② 同步写回 MD 文件
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QScrollArea.NoFrame)   # 去掉 sunken 凹槽边框（模糊源）
        self.verticalScrollBar().setSingleStep(20)  # 每格 20px，滚动更平滑

        self._md_path: Path | None = None
        self._pending: list[dict] = []
        self._done_widget: DoneWidget | None = None   # 勾选后通知已办区刷新

        # 内容容器
        self._container = QWidget()
        self._container.setObjectName("todoContainer")
        self._container.setStyleSheet("background: transparent;")
        self._vbox = QVBoxLayout(self._container)
        self._vbox.setContentsMargins(8, 6, 8, 6)
        self._vbox.setSpacing(2)
        self._vbox.addStretch(1)
        self.setWidget(self._container)

    def set_done_widget(self, w: "DoneWidget"):
        self._done_widget = w

    def load(self, md_path: Path):
        self._md_path = md_path
        self._parse()
        self._render()

    def refresh(self):
        if self._md_path:
            self.load(self._md_path)

    # ── 解析 ──────────────────────────────────────
    def _parse(self):
        import re
        self._pending = []
        if not self._md_path or not self._md_path.exists():
            return
        lines = self._md_path.read_text(encoding="utf-8").splitlines()
        section = None
        for line in lines:
            s = line.strip()
            if re.search(r"^##.*待完成", s) or re.search(r"^##.*待办", s):
                section = "pending"
            elif s.startswith("##"):
                section = None
            elif section == "pending" and s.startswith("- [ ]"):
                text = s[5:].strip()
                # 检测置顶标记 📌
                pinned = "📌" in text
                # 提取 deadline:YYYY-MM-DD（新格式）
                m_dl = re.search(r"\(?\s*deadline[：:]\s*(\d{4}-\d{2}-\d{2})\s*\)?", text)
                deadline = m_dl.group(1) if m_dl else ""
                # 保留老格式 来源:YYYY-MM-DD 但不展示
                m_src = re.search(r"\(来源[：:]?(.+?)\)", text)
                source = m_src.group(1).strip() if m_src else ""
                m_priority = re.search(r"\(priority[：:]\s*(P[0-2]|固定)\s*\)", text, re.I)
                priority = m_priority.group(1).upper() if m_priority and m_priority.group(1).upper().startswith("P") else (m_priority.group(1) if m_priority else "")
                m_planned = re.search(
                    r"\(planned[：:]\s*((?:明天 )?\d{2}:\d{2}-(?:明天 )?\d{2}:\d{2})\s*\)", text, re.I
                )
                planned = m_planned.group(1) if m_planned else ""
                m_duration = re.search(r"\(duration[：:]\s*(\d+)m\s*\)", text, re.I)
                duration = int(m_duration.group(1)) if m_duration else 0
                # 清洗显示文本
                clean = text
                clean = re.sub(r"\(?\s*deadline[：:]\s*\d{4}-\d{2}-\d{2}\s*\)?", "", clean)
                clean = re.sub(r"\s*\(来源[：:]?.+?\)", "", clean)
                clean = re.sub(r"\s*\(priority[：:]\s*(?:P[0-2]|固定)\s*\)", "", clean, flags=re.I)
                clean = re.sub(r"\s*\(planned[：:]\s*(?:明天 )?\d{2}:\d{2}-(?:明天 )?\d{2}:\d{2}\s*\)", "", clean, flags=re.I)
                clean = re.sub(r"\s*\(duration[：:]\s*\d+m\s*\)", "", clean, flags=re.I)
                clean = clean.replace("📌", "").strip()
                self._pending.append({
                    "text": clean,
                    "deadline": deadline,
                    "source": source,
                    "priority": priority,
                    "planned": planned,
                    "duration": duration,
                    "raw": line,
                    "pinned": pinned,
                })

        # 排序逻辑：
        # ① 置顶的永远在最上面
        # ② 有 deadline 的按 deadline 升序（越近越前）
        # ③ 无 deadline 的排最后
        def _sort_key(it):
            if it["pinned"]:
                return (0, 0, "", 0)
            priority_rank = {"固定": 0, "P0": 1, "P1": 2, "P2": 3}.get(it.get("priority", ""), 4)
            if it.get("planned"):
                planned = it["planned"]
                tomorrow_rank = 1 if planned.startswith("明天 ") else 0
                clock = planned.replace("明天 ", "")[:5]
                return (1, tomorrow_rank, clock, priority_rank)
            if it["deadline"]:
                return (2, 0, it["deadline"], priority_rank)
            return (3, 0, "", priority_rank)
        self._pending.sort(key=_sort_key)

    # ── 渲染 ──────────────────────────────────────
    def _render(self):
        while self._vbox.count() > 1:
            item = self._vbox.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not self._pending:
            lbl = QLabel("暂无待办  ✓")
            lbl.setStyleSheet("color:#8ca36a; font-size:14px; font-style:italic; padding:8px 4px;")
            self._vbox.insertWidget(0, lbl)
            return

        for i, item in enumerate(self._pending):
            row = QWidget()
            row.setStyleSheet("background: transparent;")
            hl = QHBoxLayout(row)
            hl.setContentsMargins(0, 1, 0, 1)
            hl.setSpacing(6)

            # 图钉按钮（置顶切换）
            pin_btn = QPushButton("📌" if item["pinned"] else "📍")
            pin_btn.setFixedWidth(26)
            pin_btn.setFlat(True)
            pin_btn.setCursor(Qt.PointingHandCursor)
            pin_btn.setToolTip("置顶 / 取消置顶")
            if item["pinned"]:
                pin_btn.setStyleSheet(
                    "QPushButton { background: transparent; border: none; "
                    "color: #cc785c; font-size: 14px; padding: 2px; }"
                    "QPushButton:hover { background: rgba(204,120,92,0.10); border-radius: 4px; }"
                )
            else:
                pin_btn.setStyleSheet(
                    "QPushButton { background: transparent; border: none; "
                    "color: #c8c0b0; font-size: 14px; padding: 2px; }"
                    "QPushButton:hover { color: #cc785c; background: rgba(204,120,92,0.08); border-radius: 4px; }"
                )
            pin_btn.clicked.connect(lambda _=None, it=item: self._toggle_pin(it))

            # 执行计划优先展示“优先级 + 建议开始时间”；旧待办仍展示 deadline。
            dl_str = item.get("deadline", "")
            dl_text = ""
            dl_color = "#8e8a82"
            priority = item.get("priority", "")
            planned = item.get("planned", "")
            if priority:
                if planned.startswith("明天 "):
                    start_time = "明天\n" + planned.replace("明天 ", "")[:5]
                else:
                    start_time = planned[:5] if planned else ""
                dl_text = f"{priority}\n{start_time}".strip()
                dl_color = {"固定": "#75528f", "P0": "#cc4c3a", "P1": "#d47a32", "P2": "#6f7f5c"}.get(
                    priority, "#8e8a82"
                )
            elif dl_str:
                try:
                    dl_date = dt.date.fromisoformat(dl_str)
                    delta = (dl_date - dt.date.today()).days
                    if delta < 0:
                        dl_text = f"逾期{-delta}天"
                        dl_color = "#cc4c3a"   # 红
                    elif delta == 0:
                        dl_text = "今天"
                        dl_color = "#cc785c"   # Claude 橙
                    elif delta == 1:
                        dl_text = "明天"
                        dl_color = "#cc785c"
                    elif delta <= 3:
                        dl_text = f"{delta}天后"
                        dl_color = "#d9913a"   # 暖橙
                    elif delta <= 7:
                        dl_text = f"{delta}天后"
                        dl_color = "#8e8a82"
                    else:
                        # 远期的，显示 MM-DD
                        dl_text = dl_str[5:] if len(dl_str) == 10 else dl_str
                        dl_color = "#aeaeb2"
                except Exception:
                    dl_text = dl_str
            date_lbl = QLabel(dl_text)
            date_lbl.setFixedWidth(58)
            date_lbl.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
            date_lbl.setToolTip(
                "固定：不可占用时间\nP0：立即处理\nP1：今天重点\nP2：有余力再做"
                if priority else ""
            )
            date_lbl.setStyleSheet(
                f"color:{dl_color}; font-size:12px; font-family:'Cascadia Mono','Consolas'; "
                "letter-spacing:0.3px;"
            )

            # 裸 checkbox(只有方框,文本另起 QLabel,因为 QCheckBox 自带 label 不能 wordwrap)
            cb = QCheckBox("")
            cb.setChecked(False)
            cb.toggled.connect(lambda checked, it=item: self._on_check(it, checked))

            # 文本 label,自动换行
            text_lbl = QLabel(item["text"])
            text_lbl.setWordWrap(True)
            text_lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            text_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            if item["pinned"]:
                text_lbl.setStyleSheet(
                    "QLabel { font-weight: 600; color:#1c1b18; "
                    "background:transparent; line-height:1.5; padding:2px 0; }"
                )
            else:
                text_lbl.setStyleSheet(
                    "QLabel { color:#1c1b18; background:transparent; "
                    "line-height:1.5; padding:2px 0; }"
                )
            # 点击文本也能勾选 — 给 row 装一个 mousePress 转发
            def _row_click(ev, _cb=cb):
                _cb.toggle()
            row.mousePressEvent = _row_click

            hl.addWidget(pin_btn, 0, Qt.AlignTop)
            hl.addWidget(date_lbl, 0, Qt.AlignTop)
            hl.addWidget(cb, 0, Qt.AlignTop)
            hl.addWidget(text_lbl, 1)
            self._vbox.insertWidget(i, row)

    def _toggle_pin(self, item: dict):
        """切换该条待办的置顶状态。直接改 MD 文件。"""
        if not self._md_path or not self._md_path.exists():
            return
        try:
            lines = self._md_path.read_text(encoding="utf-8").splitlines(keepends=True)
            new_lines = []
            for ln in lines:
                if ln.rstrip("\n") == item["raw"]:
                    if item["pinned"]:
                        # 去掉 📌
                        new_ln = ln.replace("📌", "").replace("  ", " ")
                    else:
                        # 加 📌 在 "- [ ] " 后面、文本前
                        new_ln = ln.replace("- [ ] ", "- [ ] 📌 ", 1)
                    new_lines.append(new_ln)
                else:
                    new_lines.append(ln)
            self._md_path.write_text("".join(new_lines), encoding="utf-8")
        except Exception:
            return
        # 重新渲染
        self.refresh()

    # ── 勾选 → 写回 MD ────────────────────────────
    def _on_check(self, item: dict, checked: bool):
        if not checked or not self._md_path:
            return
        try:
            lines = self._md_path.read_text(encoding="utf-8").splitlines(keepends=True)
            old_raw = item["raw"]
            today = dt.date.today().isoformat()

            # 构造已完成行
            done_line = old_raw.replace("- [ ]", "- [x]", 1).rstrip("\n\r")
            if "(完成:" not in done_line:
                done_line += f"  (完成:{today})"
            done_line += "\n"

            # 从原位置删除该行
            new_lines = []
            removed = False
            for ln in lines:
                if not removed and ln.rstrip("\n\r") == old_raw.rstrip("\n\r"):
                    removed = True   # 跳过（删除）
                    continue
                new_lines.append(ln)

            # 找到「已完成」section，追加进去；没有则自动创建
            done_header_idx = None
            for i, ln in enumerate(new_lines):
                if ln.strip().startswith("## ") and "已完成" in ln:
                    done_header_idx = i
                    break

            if done_header_idx is not None:
                # 插在 ## 已完成 的下一行（跳过紧跟的空行）
                insert_at = done_header_idx + 1
                while insert_at < len(new_lines) and new_lines[insert_at].strip() == "":
                    insert_at += 1
                new_lines.insert(insert_at, done_line)
            else:
                # 没有已完成 section，末尾追加
                if new_lines and new_lines[-1].strip() != "":
                    new_lines.append("\n")
                new_lines.append("## 本周已完成\n")
                new_lines.append(done_line)

            self._md_path.write_text("".join(new_lines), encoding="utf-8")
        except Exception:
            pass

        # 刷新待办列表，并通知已办区更新
        def _refresh_both():
            self.refresh()
            if self._done_widget:
                self._done_widget.refresh()
        QTimer.singleShot(150, _refresh_both)


# ============================================================
# 信号灯(自定义绘制圆点 + 呼吸效果)
# ============================================================
class SignalDot(QWidget):
    """24×24 圆形信号灯,带柔和发光。"""

    COLORS = {
        "off": QColor("#f5f3ed"),     # 暖灰：未启动
        "warn": QColor("#d9a865"),    # 蜂蜜：警告
        "good": QColor("#9cb087"),    # 鼠尾草绿：正常
        "pause": QColor("#a890b8"),   # 紫罗兰：暂停
        "bad": QColor("#b04030"),     # 柔砖红：故障
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(24, 24)
        self._color = self.COLORS["off"]

    def set_state(self, state: str):
        color = self.COLORS.get(state, self.COLORS["off"])
        if color != self._color:
            self._color = color
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        # 外晕
        halo = QColor(self._color)
        halo.setAlpha(60)
        p.setBrush(QBrush(halo))
        p.setPen(Qt.NoPen)
        p.drawEllipse(2, 2, 20, 20)
        # 实心
        p.setBrush(QBrush(self._color))
        p.drawEllipse(6, 6, 12, 12)


class RecordingTimeDialog(QDialog):
    """只在录音时间不可靠时出现的极简确认窗口。"""

    def __init__(self, guess, parent=None):
        super().__init__(parent)
        self.setWindowTitle("确认录音时间")
        self.setMinimumWidth(560)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 22)
        root.setSpacing(14)

        title = QLabel("这段录音应该放进哪一天？")
        title.setStyleSheet("font-size:20px; font-weight:700; color:#1c1b18;")
        root.addWidget(title)
        detail = QLabel(
            f"{guess.path.name}\n\n"
            f"声年只读取到：{guess.source_label}。复制或下载文件后，这个时间可能变化，"
            "请确认一次。"
        )
        detail.setWordWrap(True)
        detail.setStyleSheet("color:#6f6a61; line-height:1.6;")
        root.addWidget(detail)

        quick = QHBoxLayout()
        today = QPushButton("今天")
        yesterday = QPushButton("昨天")
        detected = QPushButton("使用检测时间")
        quick.addWidget(today)
        quick.addWidget(yesterday)
        quick.addWidget(detected)
        root.addLayout(quick)

        self.editor = QDateTimeEdit()
        self.editor.setCalendarPopup(True)
        self.editor.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.editor.setDateTime(guess.recorded_at)
        self.editor.setMinimumHeight(42)
        root.addWidget(self.editor)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("取消这段")
        confirm = QPushButton("确认并导入")
        confirm.setObjectName("primaryBtn")
        cancel.clicked.connect(self.reject)
        confirm.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(confirm)
        root.addLayout(buttons)

        def set_day(day: dt.date):
            current = self.editor.dateTime().toPython()
            self.editor.setDateTime(dt.datetime.combine(day, current.time()))

        today.clicked.connect(lambda: set_day(dt.date.today()))
        yesterday.clicked.connect(
            lambda: set_day(dt.date.today() - dt.timedelta(days=1))
        )
        detected.clicked.connect(
            lambda: self.editor.setDateTime(guess.recorded_at)
        )

    def selected_datetime(self) -> dt.datetime:
        return self.editor.dateTime().toPython().replace(
            tzinfo=None, microsecond=0
        )


# ============================================================
# 进程封装
# ============================================================
class ProcessHandle:
    def __init__(self, name: str, cmd: list[str], cwd: Path):
        self.name = name
        self.cmd = cmd
        self.cwd = cwd
        self.proc: subprocess.Popen | None = None
        self.last_error = ""

    def _process_markers(self) -> list[str]:
        """返回脚本版与安装版都能识别的命令行特征。"""
        markers: list[str] = []
        script = Path(self.cmd[-1]).name
        if script:
            markers.append(script)
        if self.name in {"recorder", "transcriber"}:
            markers.append(f"--role {self.name}")
        return list(dict.fromkeys(markers))

    def _matching_process_ids(self) -> list[int]:
        """查找当前角色对应的 Windows 进程，兼容源码脚本与冻结安装版。"""
        found: list[int] = []
        if sys.platform != "win32":
            pid = locked_role_pid(ROOT, self.name)
            return [pid] if pid else []
        try:
            import subprocess as _sp

            for marker in self._process_markers():
                result = _sp.run(
                    [
                        "wmic",
                        "process",
                        "where",
                        f"CommandLine like '%{marker}%'",
                        "get",
                        "ProcessId",
                        "/value",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    creationflags=NO_WINDOW,
                )
                for line in result.stdout.splitlines():
                    if not line.startswith("ProcessId="):
                        continue
                    pid_str = line.split("=", 1)[1].strip()
                    if pid_str:
                        found.append(int(pid_str))
        except Exception:
            pass
        return list(dict.fromkeys(found))

    def _kill_stale(self) -> None:
        """杀掉同角色的孤儿进程，但跳过当前已在管理的 PID。"""
        if sys.platform != "win32":
            return  # POSIX workers use a persistent advisory lock.
        my_pid = self.proc.pid if (self.proc and self.proc.poll() is None) else None
        try:
            import subprocess as _sp
            for pid in self._matching_process_ids():
                if pid != my_pid:          # 不杀自己管的进程
                    _sp.run(
                        ["taskkill", "/F", "/T", "/PID", str(pid)],
                        capture_output=True,
                        timeout=5,
                        creationflags=NO_WINDOW,
                    )
        except Exception:
            pass

    def adopt_running(self) -> bool:
        """查找已运行的同角色进程，接管 PID（兼容脚本版和冻结安装版）。"""
        if self.is_running():
            return True
        for pid in self._matching_process_ids():
            if pid != os.getpid() and self._pid_exists(pid):
                self._adopted_pid = pid
                return True
        return False

    def _pid_exists(self, pid: int) -> bool:
        if sys.platform != "win32":
            return locked_role_pid(ROOT, self.name) == pid
        return pid_exists(pid)

    def is_running(self) -> bool:
        # 优先检查 Popen 句柄
        if self.proc is not None and self.proc.poll() is None:
            return True
        # 其次检查 adopt 接管的 PID
        if hasattr(self, "_adopted_pid") and self._adopted_pid:
            if self._pid_exists(self._adopted_pid):
                return True
            else:
                self._adopted_pid = None
        return False

    def start(self) -> bool:
        self.last_error = ""
        if self.is_running():
            return True
        # launcher 更新或重启时优先复用旧版后台角色，避免重复录音/转写。
        if self.adopt_running():
            return True
        # 启动前先杀掉可能残留的孤儿进程
        self._kill_stale()
        log_path = ROOT / "logs" / f"{self.name}-launcher.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = open(log_path, "ab")
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NO_WINDOW
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            self.proc = subprocess.Popen(
                self.cmd, cwd=str(self.cwd),
                stdout=self._log, stderr=subprocess.STDOUT,
                creationflags=flags, env=env,
            )
        except Exception as exc:
            self.proc = None
            self.last_error = f"{self.name} 启动失败：{exc}"
            try:
                self._log.close()
            except Exception:
                pass
            return False
        return True

    def stop(self) -> None:
        adopted_pid = getattr(self, "_adopted_pid", None)
        if self.proc and self.proc.poll() is None:
            if sys.platform == "win32":
                # taskkill /F /T 杀整棵进程树
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                        capture_output=True, timeout=5, creationflags=NO_WINDOW,
                    )
                except Exception:
                    self.proc.kill()
            else:
                self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        elif adopted_pid and self._pid_exists(adopted_pid):
            try:
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(adopted_pid)],
                        capture_output=True, timeout=5, creationflags=NO_WINDOW,
                    )
                else:
                    os.kill(adopted_pid, signal.SIGTERM)
                    deadline = time.monotonic() + 5
                    while self._pid_exists(adopted_pid) and time.monotonic() < deadline:
                        time.sleep(0.1)
                    if self._pid_exists(adopted_pid):
                        os.kill(adopted_pid, signal.SIGKILL)
            except Exception:
                pass
        if hasattr(self, "_log") and self._log:
            try:
                self._log.close()
            except Exception:
                pass
        self.proc = None
        self._adopted_pid = None


# ============================================================
# 主窗口
# ============================================================
class Launcher(QMainWindow):
    moments_done = Signal(str, str)
    moments_failed = Signal(str)
    moments_progress = Signal(str)
    moments_diary_ready = Signal(str)
    background_status = Signal(str)
    background_refresh = Signal(str)
    summary_finished = Signal(bool, str)
    mini_summary_finished = Signal(bool, str, str)
    card_brief_finished = Signal(str, bool, str)
    update_available = Signal(dict)
    update_progress = Signal(str)
    update_ready = Signal(str)
    update_failed = Signal(str)
    update_check_finished = Signal(str, bool)
    audio_probe_finished = Signal(object)
    audio_import_finished = Signal(object)
    audio_import_failed = Signal(str)

    def __init__(self):
        super().__init__()
        title = f"声年｜你的 AI 语音知识库 · 开源版 {APP_VERSION}"
        if sys.platform == "darwin":
            title += " · Mac 测试版 1"
        self.setWindowTitle(title)
        self.setAcceptDrops(True)
        # 默认尺寸：贴合大多数 16:9 屏幕，留 60px 边距
        self.resize(1900, 920)
        self.setMinimumSize(1100, 620)
        if sys.platform == "darwin" and self.screen():
            area = self.screen().availableGeometry()
            self.resize(min(1440, int(area.width() * 0.92)), min(920, int(area.height() * 0.88)))
        self._aspect_ratio = 1900 / 920   # 锁定的宽高比
        self._resizing_internal = False   # 防止 resize 事件递归
        self._font_scale = load_font_scale(ROOT)
        self._font_scale_actions: dict[float, QAction] = {}

        self.recorder = ProcessHandle(
            "recorder", _role_command("recorder"), ROOT)
        self.transcriber = ProcessHandle(
            "transcriber", _role_command("transcriber"), ROOT)

        self._alert_armed_at: float | None = None
        self._mini_summary_running: bool = False
        self._card_todo_generation_pending: bool = False
        self._audio_import_running: bool = False
        self._last_mini_at: float = 0.0  # epoch 秒，用于展示上次小结时间
        # 阶段小结改为「按钟点」触发(到 9/12/15/18/21 点就跑),不按启动器运行时长
        self._mini_hours = sorted(set(CONFIG["summary"].get("mini_clock_hours", [9, 12, 15, 18, 21])))
        self._mini_fired: set = set()  # 今天已触发的钟点 "2026-06-05-9",防重复

        self.moments_done.connect(lambda day, path: self._on_moments_done(day, Path(path)))
        self.moments_failed.connect(self._on_moments_failed)
        self.moments_progress.connect(lambda text: self.bottom_status.setText(text))
        self.moments_diary_ready.connect(self._generate_moments_for_day)
        self.background_status.connect(self._on_background_status)
        self.background_refresh.connect(self._on_background_refresh)
        self.summary_finished.connect(self._on_summary_finished)
        self.mini_summary_finished.connect(self._on_mini_summary_finished)
        self.card_brief_finished.connect(self._on_card_brief_finished)
        self.update_available.connect(self._offer_update)
        self.update_progress.connect(lambda text: self.bottom_status.setText(text))
        self.update_ready.connect(self._launch_downloaded_update)
        self.update_failed.connect(self._on_update_failed)
        self.update_check_finished.connect(self._on_update_check_finished)
        self.audio_probe_finished.connect(self._on_audio_probe_finished)
        self.audio_import_finished.connect(self._on_audio_import_finished)
        self.audio_import_failed.connect(self._on_audio_import_failed)

        self._build_ui()

        # 清理上次崩溃残留的「贴链接自动暂停」标记(否则录音一直显示暂停)
        self._clear_ingest_pause()

        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._tick)
        self.timer.start()

        # 启动时自动接管已在系统中运行的 recorder/transcriber（开机自启场景）
        QTimer.singleShot(500, self._adopt_existing)
        self._update_check_running = False
        if is_commercial_mode() and str(
            CONFIG.get("account", {}).get("update_manifest_url", "")
        ).strip():
            QTimer.singleShot(3000, self._check_update_async)
            self.update_timer = QTimer(self)
            self.update_timer.setInterval(4 * 60 * 60 * 1000)
            self.update_timer.timeout.connect(self._check_update_async)
            self.update_timer.start()

        self.summary_timer = QTimer(self)
        self.summary_timer.setInterval(60 * 60 * 1000)  # 每小时检查一次
        self.summary_timer.timeout.connect(self._auto_summary_check)
        self.summary_timer.start()

        # 阶段小结:按钟点触发(每分钟检查,到 9/12/15/18/21 点跑一次),不按启动器运行时长
        # —— 这样简报/项目追踪每天固定钟点更新到当天,不再落后一天
        self.mini_timer = QTimer(self)
        self.mini_timer.setInterval(60 * 1000)  # 每分钟检查一次钟点
        self.mini_timer.timeout.connect(self._check_mini_schedule)
        self.mini_timer.start()
        # 历史 AI 补跑可能一次触发多天、多个模型请求。默认关闭，避免每次启动
        # 软件都产生用户没有主动确认的费用；需要时仍可手动生成。
        if CONFIG.get("summary", {}).get("startup_ai_backfill", False):
            QTimer.singleShot(25000, self._check_mini_startup)
            QTimer.singleShot(5000, self._backfill_wiki_async)
        # 启动 8 秒后:扫会议候选,有就在顶部提醒
        QTimer.singleShot(8000, self._scan_meeting_candidates_async)

        # 自定义定时卡片只在用户明确选择“每天自动更新”后运行。
        # 首次检查放到索引预热之后，随后每小时检查；数据库保证每天最多一次。
        self.card_schedule_timer = QTimer(self)
        self.card_schedule_timer.setInterval(60 * 60 * 1000)
        self.card_schedule_timer.timeout.connect(self._run_due_cards)
        self.card_schedule_timer.start()
        QTimer.singleShot(120_000, self._run_due_cards)

    def _check_update_async(self, manual: bool = False):
        manifest_url = str(CONFIG.get("account", {}).get("update_manifest_url", "")).strip()
        if not manifest_url:
            if manual:
                self.update_check_finished.emit("当前安装包没有配置更新地址，请安装最新完整版本。", True)
            return
        if self._update_check_running:
            if manual:
                self.update_check_finished.emit("正在检查更新，请稍候。", False)
            return
        self._update_check_running = True
        if manual:
            self.bottom_status.setText("正在检查软件更新…")

        def work():
            try:
                from updater import check_for_update
                payload = check_for_update(
                    manifest_url,
                    APP_VERSION,
                    suppress_errors=not manual,
                )
                if payload:
                    self.update_available.emit(payload)
                elif manual:
                    self.update_check_finished.emit(f"当前已经是最新版本 {APP_VERSION}。", False)
            except Exception as exc:
                # 自动检查失败不打扰录音；手动检查必须告诉用户具体原因。
                if manual:
                    self.update_check_finished.emit(f"检查更新失败：{exc}", True)
            finally:
                self._update_check_running = False

        threading.Thread(target=work, daemon=True, name="update-check").start()

    def _on_update_check_finished(self, message: str, failed: bool):
        self.bottom_status.setText(message)
        if failed:
            QMessageBox.warning(self, "检查更新", message[:500])
        elif message.startswith("当前已经"):
            QMessageBox.information(self, "检查更新", message)

    def _offer_update(self, payload: dict):
        version = str(payload.get("version", "新版本"))
        notes = str(payload.get("notes", "")).strip()
        message = f"发现声年 {version}。是否在后台下载并启动安装？"
        if notes:
            message += f"\n\n{notes[:500]}"
        if QMessageBox.question(self, "软件更新", message) != QMessageBox.StandardButton.Yes:
            return

        def work():
            try:
                from updater import download_and_launch
                path = download_and_launch(
                    payload,
                    progress=lambda done, total: self.update_progress.emit(
                        f"正在下载更新 {done * 100 // max(total, 1)}% · 可断点续传"
                    ),
                    launch=False,
                )
                self.update_ready.emit(str(path))
            except Exception as exc:
                self.update_failed.emit(str(exc))

        threading.Thread(target=work, daemon=True, name="update-download").start()
        QMessageBox.information(self, "软件更新", "安装包正在后台下载，可断点续传。校验签名和 SHA-256 通过后会自动关闭录音与转写，并启动安装程序。")

    def _launch_downloaded_update(self, path: str):
        try:
            self._stop_all()
            subprocess.Popen([path], close_fds=True)
        except Exception as exc:
            self._on_update_failed(str(exc))
            return
        QApplication.instance().quit()

    def _on_update_failed(self, detail: str):
        self.bottom_status.setText("更新失败，下次启动会重试")
        QMessageBox.warning(self, "软件更新失败", f"新版下载或校验失败，不影响当前版本继续使用。\n\n{detail[:500]}")

    # ---------- UI ----------
    def _build_ui(self):
        central = QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)

        # 注入背景图：优先用户自定义的 custom-bg.png，否则默认山脉湖泊
        custom_bg = ROOT / "assets" / "backgrounds" / "custom-bg.png"
        default_bg = RESOURCE_ROOT / "assets" / "backgrounds" / "mountains-lake-light.png"
        bg_path = custom_bg if custom_bg.exists() else default_bg
        if bg_path.exists():
            # 用 paintEvent 自己画背景图（不用 QPalette，避免 setStyleSheet 时被 polish 覆盖）
            from PySide6.QtGui import QPixmap
            self._bg_pixmap = QPixmap(str(bg_path))
            # 让 central 透明，露出主窗口背景
            central.setStyleSheet("QWidget#central { background: transparent; }")
        else:
            self._bg_pixmap = None

        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 顶部栏
        root.addWidget(self._build_topbar())

        # ── 警报横幅（全宽，默认隐藏）──
        self.alert_frame = QFrame()
        self.alert_frame.setObjectName("alertBanner")
        al = QHBoxLayout(self.alert_frame)
        al.setContentsMargins(20, 6, 20, 6)
        self.alert_label = QLabel("")
        self.alert_label.setObjectName("alertText")
        al.addWidget(self.alert_label, 1)
        dismiss = QPushButton("知道了")
        dismiss.clicked.connect(self._dismiss_alert)
        dismiss.setMaximumWidth(80)
        al.addWidget(dismiss)
        self.alert_frame.hide()
        root.addWidget(self.alert_frame)

        # ── 会议候选提醒条(独立于 alert,放最上方)──
        self.meeting_hint_frame = QFrame()
        # 强对比配色:深咖色背景 + 米白字 + 厚边框,在浅色背景上一眼看到
        self.meeting_hint_frame.setStyleSheet(
            "QFrame { background:#3a2a18; "
            "border-top:3px solid #cc785c; border-bottom:3px solid #cc785c; }"
        )
        self.meeting_hint_frame.setMinimumHeight(72)   # 强制至少 72px 高,不会被压扁
        mh = QHBoxLayout(self.meeting_hint_frame)
        mh.setContentsMargins(20, 10, 20, 10)
        mh.setSpacing(10)
        self.meeting_hint_label = QLabel("")
        self.meeting_hint_label.setStyleSheet(
            "color:#faf5e8; font-size:13px; font-weight:600; background:transparent;"
        )
        self.meeting_hint_label.setWordWrap(True)
        mh.addWidget(self.meeting_hint_label, 1)
        # 预览按钮:让你先看完整内容再决定
        self.btn_meeting_preview = QPushButton("👁 预览内容")
        self.btn_meeting_preview.setStyleSheet(
            "QPushButton { background:#faf5e8; color:#3a2a18; "
            "border:1px solid #cc785c; border-radius:6px; "
            "padding:7px 12px; font-size:13px; font-weight:700; }"
            "QPushButton:hover { background:#fff; }"
        )
        self.btn_meeting_preview.setMaximumWidth(110)
        self.btn_meeting_preview.setToolTip("打开预览窗口看完整对话内容,再决定是不是会议")
        self.btn_meeting_preview.clicked.connect(self._on_meeting_hint_preview)
        mh.addWidget(self.btn_meeting_preview)
        self.btn_meeting_make = QPushButton("生成纪要")
        self.btn_meeting_make.setObjectName("primaryBtn")
        self.btn_meeting_make.setMaximumWidth(96)
        self.btn_meeting_make.clicked.connect(self._on_meeting_hint_make)
        mh.addWidget(self.btn_meeting_make)
        self.btn_meeting_skip = QPushButton("跳过")
        self.btn_meeting_skip.setStyleSheet(
            "QPushButton { background:transparent; color:#8e6840; "
            "border:1px solid rgba(204,120,92,0.4); border-radius:6px; "
            "padding:6px 10px; font-size:12px; }"
            "QPushButton:hover { color:#b94a4a; }"
        )
        self.btn_meeting_skip.setMaximumWidth(72)
        self.btn_meeting_skip.clicked.connect(self._on_meeting_hint_skip)
        mh.addWidget(self.btn_meeting_skip)
        self.btn_meeting_dismiss_all = QPushButton("全部忽略")
        self.btn_meeting_dismiss_all.setStyleSheet(self.btn_meeting_skip.styleSheet())
        self.btn_meeting_dismiss_all.setMaximumWidth(80)
        self.btn_meeting_dismiss_all.setToolTip("把检测到的所有候选都标记为忽略,本次会话不再提醒")
        self.btn_meeting_dismiss_all.clicked.connect(self._on_meeting_hint_dismiss_all)
        mh.addWidget(self.btn_meeting_dismiss_all)
        self.meeting_hint_frame.hide()
        self._meeting_candidates: list[dict] = []
        self._meeting_cur_idx = 0
        root.addWidget(self.meeting_hint_frame)

        # ── 左右主分栏（水平 QSplitter） ──
        h_splitter = QSplitter(Qt.Horizontal)
        h_splitter.setStyleSheet("""
            QSplitter::handle:horizontal {
                background: rgba(28,27,24,0.12);
                width: 8px;
                margin: 0 1px;
                border-radius: 4px;
            }
            QSplitter::handle:horizontal:hover {
                background: rgba(200,150,104,0.55);
            }
            QSplitter::handle:vertical {
                background: rgba(28,27,24,0.12);
                height: 8px;
                margin: 1px 0;
                border-radius: 4px;
            }
            QSplitter::handle:vertical:hover {
                background: rgba(200,150,104,0.55);
            }
        """)

        # ════════════════════════════
        # 左列：操作区 + 实时转写
        # ════════════════════════════
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(20, 16, 10, 12)
        left_layout.setSpacing(12)

        # 状态卡
        left_layout.addWidget(self._build_status_card())

        # 主操作按钮行
        left_layout.addLayout(self._build_action_row())

        # 今日统计 + 总结按钮
        left_layout.addWidget(self._build_today_card())

        # ── 实时转写 + 待讨论 横向分栏 ──
        live_discuss_splitter = QSplitter(Qt.Horizontal)
        live_discuss_splitter.setStyleSheet(h_splitter.styleSheet())

        # 实时转写（左半,~60%）
        preview_wrap = QFrame()
        preview_wrap.setObjectName("infoPanel")
        pwl = QVBoxLayout(preview_wrap)
        pwl.setContentsMargins(14, 10, 14, 10)
        pwl.setSpacing(6)
        preview_lbl = QLabel("最 近 50 段 · 实时滚动")
        preview_lbl.setStyleSheet(
            "color:#1c1b18; font-size:14px; letter-spacing:4px; font-weight:800;")
        pwl.addWidget(preview_lbl)
        sep_pv = QFrame(); sep_pv.setObjectName("divider"); sep_pv.setFrameShape(QFrame.HLine)
        pwl.addWidget(sep_pv)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFrameShape(QPlainTextEdit.NoFrame)
        self.preview.setMinimumHeight(80)
        pwl.addWidget(self.preview, 1)
        live_discuss_splitter.addWidget(preview_wrap)

        # 待讨论(右半,~40%)
        openq_panel = QFrame()
        openq_panel.setObjectName("infoPanel")
        opl = QVBoxLayout(openq_panel)
        opl.setContentsMargins(14, 10, 14, 10)
        opl.setSpacing(6)
        # 头部:标题 + 日期切换(用昨天的 questions)
        oqhdr = QHBoxLayout()
        self.openq_title = QLabel("⚑ 待 讨 论")
        # 黑字 + 加粗,跟其他面板标题视觉一致;前面 ⚑ 用 Claude 橙做点缀
        self.openq_title.setStyleSheet(
            "color:#1c1b18; font-size:14px; letter-spacing:4px; font-weight:800;"
        )
        oqhdr.addWidget(self.openq_title, 1)
        # 刷新按钮 — 用户改了 questions.json 后立即生效
        btn_oq_refresh = QPushButton("刷新")
        btn_oq_refresh.setObjectName("ghostBtn")
        btn_oq_refresh.setMaximumWidth(48)
        btn_oq_refresh.setToolTip("重新读 questions.json")
        btn_oq_refresh.clicked.connect(
            lambda: self._reload_open_questions(dt.date.today() - dt.timedelta(days=1))
        )
        oqhdr.addWidget(btn_oq_refresh)
        opl.addLayout(oqhdr)
        sep_oq = QFrame(); sep_oq.setObjectName("divider"); sep_oq.setFrameShape(QFrame.HLine)
        opl.addWidget(sep_oq)

        # 滚动区,装问题卡片
        self.openq_scroll = QScrollArea()
        self.openq_scroll.setWidgetResizable(True)
        self.openq_scroll.setFrameShape(QScrollArea.NoFrame)
        self.openq_scroll.setStyleSheet("background:transparent; border:none;")
        self.openq_container = QWidget()
        self.openq_container.setStyleSheet("background:transparent;")
        self.openq_layout = QVBoxLayout(self.openq_container)
        self.openq_layout.setContentsMargins(0, 4, 0, 4)
        self.openq_layout.setSpacing(8)
        # 空状态占位
        self.openq_empty = QLabel(
            "暂无待讨论 — AI 会在每天复盘后\n自动把你「没想清楚的事」列在这里"
        )
        self.openq_empty.setWordWrap(True)
        self.openq_empty.setAlignment(Qt.AlignCenter)
        self.openq_empty.setStyleSheet(
            "color:#8e8a82; font-size:12px; padding:24px 8px; line-height:1.7;"
        )
        self.openq_layout.addWidget(self.openq_empty)
        self.openq_layout.addStretch(1)
        self.openq_scroll.setWidget(self.openq_container)
        opl.addWidget(self.openq_scroll, 1)

        live_discuss_splitter.addWidget(openq_panel)
        if not feature_enabled("deep_discussion"):
            # 商业 V1 不包含深度讨论：保留个人数据和代码，但隐藏全部入口。
            openq_panel.setVisible(False)
            live_discuss_splitter.setSizes([700, 0])
            live_discuss_splitter.setCollapsible(1, True)
        else:
            live_discuss_splitter.setSizes([420, 280])   # 60 / 40 比例
            # 个人版待讨论面板可以缩,但不能完全收掉
            live_discuss_splitter.setCollapsible(1, False)
        live_discuss_splitter.setCollapsible(0, False)

        left_layout.addWidget(live_discuss_splitter, 1)

        h_splitter.addWidget(left_widget)

        # ════════════════════════════
        # 右列：简报（上）+ 待办（下），垂直可拖
        # ════════════════════════════
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(10, 16, 20, 12)
        right_layout.setSpacing(0)

        v_splitter = QSplitter(Qt.Vertical)
        v_splitter.setStyleSheet(h_splitter.styleSheet())

        # ---- 简报区：昨日（左）+ 今日（右）水平分栏 ----
        brief_splitter = QSplitter(Qt.Horizontal)
        brief_splitter.setStyleSheet(h_splitter.styleSheet())

        # 昨日简报（只读）
        yest_panel = QFrame()
        yest_panel.setObjectName("infoPanel")
        ywl = QVBoxLayout(yest_panel)
        ywl.setContentsMargins(14, 10, 14, 10)
        ywl.setSpacing(6)
        yest_hdr = QHBoxLayout()
        yest_title = QLabel("昨 日 简 报")
        yest_title.setStyleSheet(
            "color:#1c1b18; font-size:14px; letter-spacing:4px; font-weight:800;")
        yest_hdr.addWidget(yest_title, 1)
        btn_open_yest = QPushButton("打开")
        btn_open_yest.setObjectName("ghostBtn")
        btn_open_yest.setMaximumWidth(48)
        btn_open_yest.clicked.connect(self._open_yest_md)
        yest_hdr.addWidget(btn_open_yest)
        ywl.addLayout(yest_hdr)
        sep_y = QFrame(); sep_y.setObjectName("divider"); sep_y.setFrameShape(QFrame.HLine)
        ywl.addWidget(sep_y)
        self.yest_text = QPlainTextEdit()
        self.yest_text.setReadOnly(True)
        self.yest_text.setFrameShape(QPlainTextEdit.NoFrame)
        self.yest_text.setPlaceholderText("昨日还没有总结…")
        self.yest_text.setMinimumHeight(80)
        self.yest_text.setStyleSheet("border: none; padding: 4px 2px;")
        ywl.addWidget(self.yest_text, 1)

        # (待讨论列表已移到左列「实时滚动」右侧,这里不再放)

        # 今日简报（可编辑）
        brief_panel = QFrame()
        brief_panel.setObjectName("infoPanel")
        bwl = QVBoxLayout(brief_panel)
        bwl.setContentsMargins(14, 10, 14, 10)
        bwl.setSpacing(6)
        brief_hdr = QHBoxLayout()
        brief_title = QLabel("今 日 简 报")
        brief_title.setStyleSheet(
            "color:#1c1b18; font-size:14px; letter-spacing:4px; font-weight:800;")
        brief_hdr.addWidget(brief_title, 1)
        btn_open_brief = QPushButton("打开")
        btn_open_brief.setObjectName("ghostBtn")
        btn_open_brief.setMaximumWidth(48)
        btn_open_brief.clicked.connect(self._open_brief_md)
        brief_hdr.addWidget(btn_open_brief)
        bwl.addLayout(brief_hdr)
        sep0 = QFrame(); sep0.setObjectName("divider"); sep0.setFrameShape(QFrame.HLine)
        bwl.addWidget(sep0)
        self.brief_text = QPlainTextEdit()
        self.brief_text.setReadOnly(True)   # 只读，避免显示版覆盖 MD 源
        self.brief_text.setFrameShape(QPlainTextEdit.NoFrame)
        self.brief_text.setPlaceholderText("运行一次「立即总结今天」后自动生成。点“打开”查看本地 Markdown 文件。")
        self.brief_text.setMinimumHeight(80)
        self.brief_text.setStyleSheet("border: none; padding: 4px 2px;")
        bwl.addWidget(self.brief_text, 1)

        # 今日在左，昨日在右
        brief_splitter.addWidget(brief_panel)
        brief_splitter.addWidget(yest_panel)

        brief_splitter.setSizes([300, 300])
        v_splitter.addWidget(brief_splitter)

        # ---- 待办 + 已办 水平分栏 ----
        todo_done_widget = QWidget()
        td_layout = QHBoxLayout(todo_done_widget)
        td_layout.setContentsMargins(0, 0, 0, 0)
        td_layout.setSpacing(0)

        todo_done_splitter = QSplitter(Qt.Horizontal)
        todo_done_splitter.setStyleSheet(h_splitter.styleSheet())

        # 左：待办
        todo_panel = QFrame()
        todo_panel.setObjectName("infoPanel")
        twl = QVBoxLayout(todo_panel)
        twl.setContentsMargins(14, 10, 14, 10)
        twl.setSpacing(6)
        todo_hdr = QHBoxLayout()
        todo_title = QLabel("待 办 事 项")
        todo_title.setStyleSheet(
            "color:#1c1b18; font-size:14px; letter-spacing:4px; font-weight:800;")
        todo_hdr.addWidget(todo_title, 1)
        btn_capture_todo = QPushButton("对话添加")
        btn_capture_todo.setObjectName("ghostBtn")
        btn_capture_todo.setToolTip("说说今天要做什么，声年会先整理成待办供你确认")
        btn_capture_todo.clicked.connect(self._open_todo_capture_dialog)
        todo_hdr.addWidget(btn_capture_todo)
        btn_open_todo = QPushButton("打开")
        btn_open_todo.setObjectName("ghostBtn")
        btn_open_todo.setMaximumWidth(48)
        btn_open_todo.clicked.connect(self._open_todo_md)
        todo_hdr.addWidget(btn_open_todo)
        twl.addLayout(todo_hdr)
        sep1 = QFrame(); sep1.setObjectName("divider"); sep1.setFrameShape(QFrame.HLine)
        twl.addWidget(sep1)
        self.todo_widget = TodoWidget()
        self.todo_widget.setMinimumHeight(80)
        twl.addWidget(self.todo_widget, 1)
        # 新增待办输入框
        add_row = QHBoxLayout()
        add_row.setSpacing(6)
        self.todo_input = QLineEdit()
        self.todo_input.setPlaceholderText("+ 新增待办，按 Enter（输入 @ 给现有待办加 deadline）")
        self.todo_input.setToolTip(
            "@ 的用法：\n"
            "  在输入框打 @ → 弹现有待办列表\n"
            "  选一条 → 弹日历选日期 → 自动加 deadline\n"
            "\n"
            "新建待办：直接输入文字按 Enter（不用 @）"
        )
        self.todo_input.setStyleSheet("font-size:13px; padding:4px 8px;")
        self.todo_input.returnPressed.connect(self._add_todo_item)
        self.todo_input.textChanged.connect(self._on_todo_text_changed)
        add_row.addWidget(self.todo_input)

        # 弹出式待办列表（输入 @ 时弹出）
        self.todo_picker = QListWidget(self)
        self.todo_picker.setWindowFlags(Qt.Popup)
        self.todo_picker.setMaximumHeight(280)
        self.todo_picker.setMinimumWidth(360)
        self.todo_picker.itemClicked.connect(self._on_todo_picker_picked)
        self.todo_picker.setStyleSheet("""
            QListWidget {
                background: #ffffff;
                color: #1c1b18;
                border: 1px solid rgba(28,27,24,0.14);
                border-radius: 8px;
                padding: 4px;
                font-size: 13px;
            }
            QListWidget::item { padding: 8px 12px; border-radius: 5px; }
            QListWidget::item:hover { background: rgba(204,120,92,0.10); }
            QListWidget::item:selected { background: #cc785c; color: #ffffff; }
        """)

        # 弹出式日历（选完待办后弹出）
        self.todo_calendar = QCalendarWidget(self)
        self.todo_calendar.setWindowFlags(Qt.Popup)
        self.todo_calendar.setGridVisible(True)
        self.todo_calendar.setVerticalHeaderFormat(QCalendarWidget.NoVerticalHeader)
        self.todo_calendar.clicked.connect(self._on_todo_calendar_picked)
        self.todo_calendar.setStyleSheet("""
            QCalendarWidget QWidget { alternate-background-color: #faf9f5; }
            QCalendarWidget QAbstractItemView:enabled {
                color: #1c1b18;
                background: #ffffff;
                selection-background-color: #cc785c;
                selection-color: #ffffff;
            }
            QCalendarWidget QAbstractItemView:disabled { color: #c8c0b0; }
            QCalendarWidget QToolButton {
                color: #1c1b18;
                background: transparent;
                font-size: 13px;
                font-weight: 600;
                icon-size: 18px;
                padding: 4px;
            }
            QCalendarWidget QToolButton:hover { background: rgba(204,120,92,0.12); border-radius: 4px; }
            QCalendarWidget QSpinBox { color: #1c1b18; }
            QCalendarWidget QMenu { background: #ffffff; color: #1c1b18; }
        """)
        # 记录"当前正在编辑 deadline 的那条待办"
        self._editing_todo_raw: str | None = None
        twl.addLayout(add_row)
        todo_done_splitter.addWidget(todo_panel)

        # 右：已办
        done_panel = QFrame()
        done_panel.setObjectName("infoPanel")
        dwl = QVBoxLayout(done_panel)
        dwl.setContentsMargins(14, 10, 14, 10)
        dwl.setSpacing(6)
        done_hdr = QHBoxLayout()
        done_title = QLabel("已 办 · 最 近 10 条")
        done_title.setStyleSheet(
            "color:#1c1b18; font-size:14px; letter-spacing:4px; font-weight:800;")
        done_hdr.addWidget(done_title, 1)
        dwl.addLayout(done_hdr)
        sep2 = QFrame(); sep2.setObjectName("divider"); sep2.setFrameShape(QFrame.HLine)
        dwl.addWidget(sep2)
        self.done_widget = DoneWidget()
        self.done_widget.setMinimumHeight(80)
        dwl.addWidget(self.done_widget, 1)
        todo_done_splitter.addWidget(done_panel)

        todo_done_splitter.setSizes([300, 300])
        td_layout.addWidget(todo_done_splitter)
        v_splitter.addWidget(todo_done_widget)

        # ---- 内容选题面板（全宽，可滚动）----
        content_panel = QFrame()
        content_panel.setObjectName("infoPanel")
        cpl = QVBoxLayout(content_panel)
        cpl.setContentsMargins(14, 10, 14, 10)
        cpl.setSpacing(6)
        ci_hdr = QHBoxLayout()
        self.content_title = QLabel("✍ 内容选题 · 0")
        self.content_title.setStyleSheet(
            "color:#1c1b18; font-size:14px; letter-spacing:4px; font-weight:800;")
        ci_hdr.addWidget(self.content_title, 1)
        self.btn_find_ideas = QPushButton("找选题")
        self.btn_find_ideas.setObjectName("ghostBtn")
        self.btn_find_ideas.setMaximumWidth(64)
        self.btn_find_ideas.setToolTip("从今天的本人语音里寻找短视频选题素材")
        self.btn_find_ideas.clicked.connect(self._on_find_content_ideas)
        ci_hdr.addWidget(self.btn_find_ideas)
        cpl.addLayout(ci_hdr)
        sep_ci = QFrame(); sep_ci.setObjectName("divider"); sep_ci.setFrameShape(QFrame.HLine)
        cpl.addWidget(sep_ci)
        self.content_scroll = QScrollArea()
        self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setFrameShape(QScrollArea.NoFrame)
        self.content_scroll.setStyleSheet("background:transparent; border:none;")
        self.content_container = QWidget()
        self.content_container.setStyleSheet("background:transparent;")
        self.content_layout = QVBoxLayout(self.content_container)
        self.content_layout.setContentsMargins(0, 4, 0, 4)
        self.content_layout.setSpacing(8)
        self.content_layout.addStretch(1)
        self.content_scroll.setWidget(self.content_container)
        cpl.addWidget(self.content_scroll, 1)
        v_splitter.addWidget(content_panel)

        # 互相关联：TodoWidget ↔ DoneWidget
        self.todo_widget.set_done_widget(self.done_widget)
        self.done_widget.set_todo_widget(self.todo_widget)

        v_splitter.setSizes([220, 260, 200])
        right_layout.addWidget(v_splitter, 1)

        # 新卡片工作台先在本机体验；经典视图完整保留作为兼容回退。
        self.right_tabs = QTabWidget()
        self.right_tabs.setObjectName("workspaceTabs")
        card_workspace = self._build_card_workspace()
        if card_workspace is not None:
            self.right_tabs.addTab(card_workspace, "卡片工作台")
        self.right_tabs.addTab(right_widget, "经典视图")
        self.classic_workspace = right_widget
        h_splitter.addWidget(self.right_tabs)

        # 左（操作+转写）: 右（简报+待办+已办） = 38:62
        h_splitter.setSizes([380, 620])

        root.addWidget(h_splitter, 1)

        # 初始加载简报和 TODO
        self._reload_yest()
        self._reload_content_ideas()
        self._reload_brief()
        self._reload_todo()
        self._sync_card_workspace_from_legacy()

        # 与上面的启动补跑开关保持一致；跨天时的正常自动复盘不受影响。
        if CONFIG.get("summary", {}).get("startup_ai_backfill", False):
            QTimer.singleShot(2000, self._auto_backfill_yest_review)

        # 底部状态条
        bottom = QFrame()
        bottom.setStyleSheet(
            "background-color:rgba(250,249,245,0.92); border-top:1px solid rgba(28,27,24,0.12);")
        bl2 = QHBoxLayout(bottom)
        bl2.setContentsMargins(16, 6, 16, 6)
        self.bottom_status = QLabel("就绪 · 点'启动录音+转写'开始")
        self.bottom_status.setObjectName("bottomStatus")
        bl2.addWidget(self.bottom_status, 1)
        root.addWidget(bottom)
        card_init_error = getattr(self, "_card_init_error", "")
        if card_init_error:
            self.bottom_status.setText(
                f"卡片工作台初始化失败，已自动使用经典视图：{card_init_error}"
            )
        elif getattr(self, "card_board", None) is not None:
            QTimer.singleShot(10_000, self._refresh_card_index_async)

    def _build_card_workspace(self):
        """构建卡片工作台；任何迁移异常都只影响新工作台。"""
        self.card_board = None
        self.card_service = None
        self._card_init_error = ""
        try:
            from card_board import CardBoard, CardBoardCallbacks
            from card_service import CardCloudService

            self.card_service = CardCloudService(ROOT)
            try:
                self.card_service.store.purge_deleted()
            except Exception:
                pass
            callbacks = CardBoardCallbacks(
                generation_gate=self._card_generation_gate,
                compile_card=self.card_service.compile_card,
                generate_card=self.card_service.generate_card,
                revise_card=self.card_service.revise_card,
                chat_card=self.card_service.chat_card,
                open_todo_capture=self._open_todo_capture_dialog,
            )
            board = CardBoard(self.card_service.store, callbacks=callbacks)
            self.card_todo_widget = TodoWidget()
            self.card_done_widget = DoneWidget()
            self.card_todo_widget.set_done_widget(self.card_done_widget)
            self.card_done_widget.set_todo_widget(self.card_todo_widget)
            board.set_body_widget("todos", self.card_todo_widget)
            board.set_body_widget("done", self.card_done_widget)
            self.card_brief_text = self._make_card_brief_view(
                "运行一次“生成”后自动整理今日简报。"
            )
            self.card_yest_text = self._make_card_brief_view(
                "昨日复盘还没有生成。"
            )
            board.set_body_widget("today_brief", self.card_brief_text)
            board.set_body_widget("yesterday_brief", self.card_yest_text)
            board.set_generate_handler(
                "today_brief",
                lambda: self._start_card_brief_generation("today_brief"),
            )
            board.set_generate_handler(
                "yesterday_brief",
                lambda: self._start_card_brief_generation("yesterday_brief"),
            )
            board.set_generate_handler(
                "todos",
                self._start_card_todo_generation,
            )
            self.card_content_ideas_widget = (
                self._build_card_content_ideas_widget()
            )
            board.set_body_widget(
                "short_video", self.card_content_ideas_widget
            )
            board.documentImportRequested.connect(self._on_card_import_document)
            board.documentLibraryRequested.connect(
                self._on_card_document_library
            )
            board.cardUseful.connect(self._on_card_useful)
            board.cardRevisionRestored.connect(
                self._on_card_revision_restored
            )
            board.cardConvertToTodoRequested.connect(
                self._on_card_convert_to_todo
            )
            board.cardShortVideoRequested.connect(self._on_card_short_video)
            board.statusChanged.connect(self._on_card_status)
            board.errorRaised.connect(self._on_card_status)
            self.card_board = board
            return board
        except Exception as exc:
            self._card_init_error = str(exc)[:300]
            self.card_board = None
            self.card_service = None
            return None

    @staticmethod
    def _make_card_brief_view(placeholder: str) -> QPlainTextEdit:
        view = QPlainTextEdit()
        view.setObjectName("cardContent")
        view.setReadOnly(True)
        view.setFrameShape(QPlainTextEdit.NoFrame)
        view.setPlaceholderText(placeholder)
        view.setStyleSheet(
            "border:none; background:transparent; padding:4px 2px;"
        )
        return view

    def _build_card_content_ideas_widget(self):
        """复用经典视图的数据和创作链路，只为卡片工作台创建独立视图。"""
        widget = QWidget()
        widget.setStyleSheet("background:transparent;")
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        header = QHBoxLayout()
        self.card_content_title = QLabel("短视频选题 · 0")
        self.card_content_title.setStyleSheet(
            "color:#5a564e; font-size:12px; font-weight:700;"
        )
        header.addWidget(self.card_content_title, 1)
        self.card_btn_find_ideas = QPushButton("找选题")
        self.card_btn_find_ideas.setObjectName("ghostBtn")
        self.card_btn_find_ideas.setToolTip("从今天的本人语音里寻找短视频选题素材")
        self.card_btn_find_ideas.clicked.connect(self._on_find_content_ideas)
        header.addWidget(self.card_btn_find_ideas)
        layout.addLayout(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setStyleSheet("background:transparent; border:none;")
        container = QWidget()
        container.setStyleSheet("background:transparent;")
        self.card_content_layout = QVBoxLayout(container)
        self.card_content_layout.setContentsMargins(0, 4, 0, 4)
        self.card_content_layout.setSpacing(8)
        self.card_content_layout.addStretch(1)
        scroll.setWidget(container)
        layout.addWidget(scroll, 1)
        return widget

    def _on_card_status(self, message: str):
        if hasattr(self, "bottom_status") and message:
            self.bottom_status.setText(message)

    def _refresh_card_index_async(self):
        board = getattr(self, "card_board", None)
        service = getattr(self, "card_service", None)
        if board is None or service is None:
            return

        def done(counts):
            total = sum(int(value or 0) for value in counts.values())
            board._set_status(
                f"本地知识库索引已就绪 · 本次更新 {total} 个文件"
            )

        board.task_runner.submit(
            "card:index",
            service.refresh_index,
            done,
            lambda message: board._report_error(
                f"本地知识库索引失败：{message}"
            ),
        )

    def _run_due_cards(self):
        board = getattr(self, "card_board", None)
        service = getattr(self, "card_service", None)
        if board is None or service is None:
            return
        try:
            cards = [
                card
                for card in service.store.list_cards(
                    include_hidden=True, include_deleted=False
                )
                if card.enabled
                and card.card_type == "custom"
                and card.trigger_mode == "daily"
                and card.output_type
                not in {"structured_todos", "structured_done"}
            ]
        except Exception as exc:
            board._report_error(f"定时卡片检查失败：{exc}")
            return
        for card in cards[:10]:
            card_id = card.card_id
            key = f"card:scheduled:{card_id}"
            if board.is_card_busy(card_id):
                continue
            board._set_card_busy(card_id, True)
            board.task_runner.submit(
                key,
                lambda value=card_id: service.generate_scheduled_card(value),
                lambda result, value=card_id: board.complete_generation(
                    value, result
                ),
                lambda message, value=card_id: board._finish_with_error(
                    value, f"自动更新失败：{message}"
                ),
            )

    def _on_card_import_document(self):
        board = getattr(self, "card_board", None)
        service = getattr(self, "card_service", None)
        if board is None or service is None:
            return
        from PySide6.QtWidgets import QFileDialog

        path_text, _ = QFileDialog.getOpenFileName(
            self,
            "导入本地资料",
            str(Path.home() / "Documents"),
            "文字资料 (*.txt *.md *.markdown)",
        )
        if not path_text:
            return
        path = Path(path_text)
        board._set_status(f"正在复制并建立本地索引：{path.name}")

        def done(document):
            name = str(getattr(document, "original_name", path.name))
            board._set_status(
                f"已导入 {name}。原文件移动或删除后，声年中的副本仍可使用。"
            )

        board.task_runner.submit(
            "card:import",
            lambda: service.import_document(path),
            done,
            lambda message: board._report_error(f"导入资料失败：{message}"),
        )

    def _on_card_document_library(self):
        board = getattr(self, "card_board", None)
        service = getattr(self, "card_service", None)
        if board is None or service is None:
            return
        try:
            documents = service.resolver.list_imports()
        except Exception as exc:
            board._report_error(f"读取资料库失败：{exc}")
            return
        if not documents:
            QMessageBox.information(
                self,
                "本地资料库",
                "还没有导入 TXT 或 Markdown 文件。\n\n"
                "声年不会扫描其他文件，只有你主动导入的资料才会进入索引。",
            )
            return
        from PySide6.QtWidgets import QInputDialog

        labels: list[str] = []
        lookup: dict[str, object] = {}
        for document in documents:
            size_kb = max(1, int(document.size) // 1024)
            label = (
                f"{document.original_name} · {size_kb} KB · "
                f"{document.imported_at[:10]}"
            )
            labels.append(label)
            lookup[label] = document
        selected, ok = QInputDialog.getItem(
            self,
            "本地资料库",
            "选择一份资料。继续后可从声年副本和本地索引中删除：",
            labels,
            0,
            False,
        )
        if not ok:
            return
        document = lookup.get(str(selected))
        if document is None:
            return
        if QMessageBox.question(
            self,
            "删除导入资料",
            f"确认从声年本地资料库和索引中删除？\n\n"
            f"{document.original_name}\n\n原位置的文件不会被删除。",
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            service.resolver.delete_import(document.document_id)
            board._set_status(
                f"已从声年本地资料库删除：{document.original_name}"
            )
        except Exception as exc:
            board._report_error(f"删除资料失败：{exc}")

    def _on_card_useful(self, card_id: str):
        """确认版本后，为自定义卡片生成可由 Obsidian 打开的镜像。"""
        service = getattr(self, "card_service", None)
        if service is None:
            return
        try:
            revision = service.local.confirm_revision(card_id)
            if card_id in {"today_brief", "yesterday_brief"}:
                self._sync_confirmed_default_card(card_id, revision.content)
        except Exception as exc:
            self._on_card_status(f"内容已确认，但 Markdown 镜像失败：{exc}")

    def _on_card_revision_restored(
        self, card_id: str, content: str
    ) -> None:
        """默认简报回退版本时，同步真实 Markdown 并刷新两套视图。"""

        if card_id in {"today_brief", "yesterday_brief"}:
            self._sync_confirmed_default_card(card_id, content)

    def _sync_confirmed_default_card(self, card_id: str, content: str):
        """把用户明确选中的默认卡片版本同步回既有 Markdown。"""
        if card_id == "today_brief":
            target = knowledge_dir() / "每日简报.md"
        else:
            yesterday = dt.date.today() - dt.timedelta(days=1)
            target = ROOT / "notes" / f"{yesterday.isoformat()}-review.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        clean_content = str(content or "").strip()
        if not clean_content:
            return
        previous = ""
        if target.exists():
            previous = target.read_text(encoding="utf-8")
        if previous.strip() == clean_content:
            return
        if previous.strip():
            backup_root = ROOT / "notes" / "卡片备份"
            backup_root.mkdir(parents=True, exist_ok=True)
            timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = backup_root / f"{target.stem}-{timestamp}{target.suffix}"
            shutil.copy2(target, backup)
        from cards.engine import CardEngine

        CardEngine._atomic_write(target, clean_content + "\n")
        if card_id == "today_brief":
            self._reload_brief()
        else:
            self._reload_yest()
        board = getattr(self, "card_board", None)
        if board is not None:
            board._set_status(
                f"已确认并同步到本地 Markdown：{target.name}"
            )

    def _on_card_convert_to_todo(self, _card_id: str, content: str):
        """由用户选择一条结果并明确确认后，才写入待办。"""
        from PySide6.QtWidgets import QInputDialog

        candidates: list[str] = []
        for raw in content.splitlines():
            text = re.sub(r"^\s*(?:[-*+]|\d+[.)、])\s*", "", raw).strip()
            if (
                text
                and not text.startswith("#")
                and text not in candidates
                and len(text) <= 300
            ):
                candidates.append(text)
            if len(candidates) >= 20:
                break
        if not candidates:
            QMessageBox.information(self, "转为待办", "当前结果里没有可选择的事项。")
            return
        selected, ok = QInputDialog.getItem(
            self,
            "转为待办",
            "请选择要写入本地待办的一条内容：",
            candidates,
            0,
            True,
        )
        selected = str(selected or "").strip()
        if not ok or not selected:
            return
        if QMessageBox.question(
            self,
            "确认写入待办",
            f"确认把这条内容写入本地待办？\n\n{selected}",
        ) != QMessageBox.StandardButton.Yes:
            return
        self.todo_input.setText(selected)
        self._add_todo_item()
        board = getattr(self, "card_board", None)
        if board is not None:
            board._set_status("已写入本地待办。")

    def _on_card_short_video(self, card_id: str):
        """短视频第二阶段：选中一个题目，再生成完整口播稿。"""
        board = getattr(self, "card_board", None)
        service = getattr(self, "card_service", None)
        if board is None or service is None:
            return
        if board.is_card_busy(card_id):
            board._set_status("这张卡片正在处理，请等待当前任务完成。")
            return
        revision = service.store.current_revision(card_id)
        if revision is None or not revision.content.strip():
            board._set_status("先从本地语料生成约三个选题。")
            board.request_generate(card_id)
            return
        candidates: list[str] = []
        for raw in revision.content.splitlines():
            if raw.lstrip().startswith("#"):
                continue
            text = re.sub(r"^\s*(?:[-*+]|\d+[.)、])\s*", "", raw).strip()
            text = text.strip("# ").strip()
            if (
                text
                and text not in candidates
                and 4 <= len(text) <= 500
                and not text.startswith("AI 生成")
            ):
                candidates.append(text)
            if len(candidates) >= 12:
                break
        if not candidates:
            QMessageBox.information(
                self, "生成口播稿", "当前还没有可选择的短视频选题。"
            )
            return
        from PySide6.QtWidgets import QInputDialog

        selected, ok = QInputDialog.getItem(
            self,
            "生成短视频口播稿",
            "选择一个题目。确认后会调用你配置的 AI API：",
            candidates,
            0,
            False,
        )
        selected = str(selected or "").strip()
        if not ok or not selected:
            return
        board._set_card_busy(card_id, True)
        board.task_runner.submit(
            f"card:video:{card_id}",
            lambda: service.generate_short_video_script(card_id, selected),
            lambda result: board.complete_generation(card_id, result),
            lambda message: board._finish_with_error(
                card_id, f"口播稿生成失败：{message}"
            ),
        )

    def _legacy_card_source(self, card_id: str) -> Path | None:
        if card_id == "today_brief":
            source = resolve_today_brief(ROOT, knowledge_dir())
            return source.output_path if source.has_output else None
        if card_id == "yesterday_brief":
            source = resolve_yesterday_brief(ROOT)
            # 昨日卡片只能展示复盘链路的最终产物。原始日记即使存在，
            # 也不能拿来当作“昨日简报”兜底，否则会把未经整理的内容直接暴露。
            return source.output_path if source.has_output else None
        return None

    def _start_card_brief_generation(self, card_id: str) -> bool:
        """从卡片触发经典简报处理链，成功后再刷新阅读视图。"""

        if card_id not in {"today_brief", "yesterday_brief"}:
            return False
        if not self._ensure_cloud_ai_enabled(interactive=True):
            return False
        provider = CONFIG["summary"].get("provider", "deepseek")
        key_env = {
            "deepseek": "DEEPSEEK_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "openai": "OPENAI_API_KEY",
        }.get(provider, "DEEPSEEK_API_KEY")
        api_key = self._provider_api_key(key_env)
        if not api_key:
            QMessageBox.warning(
                self,
                "缺少 API Key",
                f"当前 provider={provider}\n\n"
                f"未找到环境变量 {key_env}。\n\n"
                f"请按 README 配置环境变量 {key_env} 后重启声年。",
            )
            return False
        os.environ[key_env] = api_key
        label = "今日简报" if card_id == "today_brief" else "昨日简报"
        self.bottom_status.setText(f"正在生成{label}…")

        def run() -> None:
            try:
                if card_id == "today_brief":
                    command = _role_command("daily-summary", "--no-lark")
                else:
                    yesterday = dt.date.today() - dt.timedelta(days=1)
                    command = _role_command(
                        "daily-summary",
                        "--review",
                        "--date",
                        yesterday.isoformat(),
                    )
                proc = subprocess.run(
                    command,
                    cwd=str(ROOT),
                    capture_output=True,
                    timeout=180,
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                    creationflags=NO_WINDOW,
                )
                if proc.returncode == 0:
                    self.card_brief_finished.emit(
                        card_id, True, f"{label}已完成"
                    )
                else:
                    self.card_brief_finished.emit(
                        card_id,
                        False,
                        f"{label}生成失败：云端 AI 暂时不可用，请稍后重试",
                    )
            except subprocess.TimeoutExpired:
                self.card_brief_finished.emit(
                    card_id, False, f"{label}生成超时，请检查网络后重试"
                )
            except Exception as exc:
                self.card_brief_finished.emit(
                    card_id, False, f"{label}生成异常：{exc}"
                )

        threading.Thread(target=run, daemon=True).start()
        return True

    def _on_card_brief_finished(
        self, card_id: str, ok: bool, message: str
    ) -> None:
        board = getattr(self, "card_board", None)
        if ok:
            if card_id == "today_brief":
                self._reload_brief()
                self._reload_todo()
            else:
                self._reload_yest()
        if board is not None:
            board.finish_handled_generation(
                card_id, ok=ok, message=message
            )
        self.bottom_status.setText(message)

    def _start_card_todo_generation(self) -> bool:
        """从待办卡片复用阶段小结链路，提取并写入结构化待办。"""

        if self._mini_summary_running:
            self.bottom_status.setText("正在整理新的语音记录，请稍候。")
            return False
        self._card_todo_generation_pending = True
        self._do_mini_summary(interactive=True)
        if not self._mini_summary_running:
            self._card_todo_generation_pending = False
            return False
        self.bottom_status.setText("正在从新的语音记录中提取待办…")
        return True

    def _sync_legacy_card(self, card_id: str) -> bool:
        """仅在卡片仍跟随旧 Markdown 时同步，绝不覆盖用户的新版本。"""
        service = getattr(self, "card_service", None)
        if service is None:
            return False
        path = self._legacy_card_source(card_id)
        if path is None or not path.is_file():
            return False
        try:
            content = path.read_text(encoding="utf-8").strip()
            if not content:
                return False
            current = service.store.current_revision(card_id)
            if current is not None and current.kind != "imported":
                return False
            if current is not None and current.content.strip() == content:
                return False
            service.store.add_revision(
                card_id,
                content,
                kind="imported",
                source_hash=hashlib.sha256(
                    content.encode("utf-8")
                ).hexdigest(),
            )
            return True
        except Exception:
            return False

    def _sync_card_workspace_from_legacy(self):
        sync_results = [
            self._sync_legacy_card(card_id)
            for card_id in ("today_brief", "yesterday_brief")
        ]
        changed = any(sync_results)
        try:
            path = knowledge_dir() / "待办总览.md"
            card_todo = getattr(self, "card_todo_widget", None)
            card_done = getattr(self, "card_done_widget", None)
            if card_todo is not None:
                card_todo.load(path)
            if card_done is not None:
                card_done.load(path)
        except Exception:
            pass
        board = getattr(self, "card_board", None)
        if changed and board is not None:
            board.refresh()

    def _auto_backfill_yest_review(self):
        """启动 2 秒后检查：如果昨日有 mini 小结但没 review 文件，自动补做。"""
        try:
            yest = dt.date.today() - dt.timedelta(days=1)
            review_p = ROOT / "notes" / f"{yest.isoformat()}-review.md"
            if review_p.exists():
                return   # 已有就不重做
            # 看昨日有没有 mini 小结（表示昨天确实有录音）
            mini_dir = ROOT / "notes" / "mini"
            has_mini = any(mini_dir.glob(f"{yest.isoformat()}-*.json")) if mini_dir.exists() else False
            if not has_mini:
                return   # 昨天没数据就不做
            # 检查 API key
            provider = CONFIG["summary"].get("provider", "deepseek")
            key_env = {"deepseek": "DEEPSEEK_API_KEY",
                       "anthropic": "ANTHROPIC_API_KEY",
                       "openai": "OPENAI_API_KEY"}.get(provider, "DEEPSEEK_API_KEY")
            api_key = self._provider_api_key(key_env)
            if not api_key:
                return
            os.environ[key_env] = api_key

            self.bottom_status.setText(f"后台补做 {yest} 昨日复盘...")

            def run():
                try:
                    proc = subprocess.run(
                        _role_command("daily-summary", "--review", "--date", yest.isoformat()),
                        cwd=str(ROOT), capture_output=True, timeout=120,
                        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                        creationflags=NO_WINDOW,
                    )
                    if proc.returncode == 0:
                        self.background_status.emit("昨日复盘已补做 · 已自动刷新")
                        self.background_refresh.emit("yest")
                    else:
                        err = proc.stderr.decode("utf-8", "ignore")[:200]
                        self.background_status.emit(f"补做复盘失败：{err}")
                except Exception as e:
                    self.background_status.emit(f"补做复盘异常：{e}")

            threading.Thread(target=run, daemon=True).start()
        except Exception:
            pass

    def _reload_yest(self):
        """从唯一成品源同时刷新经典视图和卡片工作台。"""
        try:
            source = resolve_yesterday_brief(ROOT)
            yest = source.expected_day

            if source.has_output:
                rendered = render_yesterday_review(source.content)
                self._set_brief_views(
                    "yesterday_brief",
                    rendered,
                    f"昨日（{yest}）复盘没有可展示的内容。",
                )
                self._reload_open_questions(yest)
                return

            placeholder = f"昨日（{yest}）{pending_hint(source)}"
            self._set_brief_views("yesterday_brief", "", placeholder)
            self._reload_open_questions(yest)
        except Exception as ex:
            self._set_brief_views(
                "yesterday_brief", f"（读取昨日复盘失败：{ex}）", ""
            )
        finally:
            if self._sync_legacy_card("yesterday_brief"):
                board = getattr(self, "card_board", None)
                if board is not None:
                    board.refresh()

    # ===========================================
    # 内容选题面板 + 工坊
    # ===========================================
    def _reload_content_ideas(self):
        """刷新内容选题面板。全局 try 防崩(同待讨论)。"""
        try:
            self._reload_content_ideas_impl()
        except Exception as e:
            import traceback
            try:
                with open(ROOT / "runtime" / "content-ui.log", "a", encoding="utf-8") as f:
                    f.write(f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
                            f"reload err: {e}\n{traceback.format_exc()}\n")
            except Exception:
                pass

    def _reload_content_ideas_impl(self):
        targets = []
        if hasattr(self, "content_layout") and self.content_layout is not None:
            targets.append((self.content_layout, self.content_title))
        if (
            hasattr(self, "card_content_layout")
            and self.card_content_layout is not None
        ):
            targets.append((self.card_content_layout, self.card_content_title))
        for target_layout, _target_title in targets:
            while target_layout.count():
                item = target_layout.takeAt(0)
                if item is None:
                    break
                w = item.widget()
                if w is not None:
                    w.setParent(None)
                    w.deleteLater()

        try:
            import content_ideas as ci
            # 新版内容选题只展示短视频。历史公众号选题仍保留在本地池中，
            # 不删除、不迁移，避免用户已有资料丢失。
            ideas = ci.list_open(format_filter="shortvideo")
            idea_limit = ci.max_open_ideas()
        except Exception:
            ideas = []
            idea_limit = 20

        for target_layout, target_title in targets:
            self._populate_content_ideas_panel(
                target_layout, target_title, ideas, idea_limit
            )

    def _populate_content_ideas_panel(
        self, target_layout, target_title, ideas, idea_limit
    ):
        """在经典视图和卡片工作台渲染完全相同的逐条选题。"""
        is_card_panel = target_title is getattr(
            self, "card_content_title", None
        )
        title_prefix = "短视频选题" if is_card_panel else "✍ 内容选题"
        if not ideas:
            target_title.setText(f"{title_prefix} · 0")
            empty = QLabel(
                "暂无选题 — 每天复盘后 AI 自动从你的口播挖选题\n"
                "或点右上「找选题」手动触发"
            )
            empty.setWordWrap(True)
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet(
                "color:#8e8a82; font-size:12px; padding:24px 8px; line-height:1.7;")
            target_layout.addWidget(empty)
            target_layout.addStretch(1)
            return

        if len(ideas) >= idea_limit:
            target_title.setText(
                f"{title_prefix} · {len(ideas)} · 已达 {idea_limit} 条上限"
            )
        else:
            target_title.setText(
                f"{title_prefix} · {len(ideas)} / 上限 {idea_limit}"
            )
        for idea in ideas:
            if idea.get("origin") == "external":
                badge = "🔁 二创"
            else:
                badge = "📱 短视频"
            title = idea.get("title", "(无标题)")
            hook = idea.get("hook", "")
            has_draft = bool(idea.get("draft"))

            card = QFrame()
            card.setStyleSheet(
                "QFrame { background:rgba(28,27,24,0.05); border-radius:8px; }"
                "QFrame:hover { background:rgba(200,150,104,0.10); }"
            )
            cv = QVBoxLayout(card)
            cv.setContentsMargins(12, 10, 10, 10)
            cv.setSpacing(6)

            badge_lbl = QLabel(badge)
            badge_lbl.setStyleSheet(
                "color:#cc785c; font-size:11px; font-weight:700; background:transparent;")
            cv.addWidget(badge_lbl)

            title_lbl = QLabel(title)
            title_lbl.setWordWrap(True)
            title_lbl.setStyleSheet(
                "color:#1c1b18; font-size:13px; font-weight:600; "
                "background:transparent; line-height:1.5;")
            cv.addWidget(title_lbl)

            if hook:
                hook_short = hook if len(hook) <= 80 else hook[:78] + "…"
                hook_lbl = QLabel(f"💡 {hook_short}")
                hook_lbl.setWordWrap(True)
                hook_lbl.setStyleSheet(
                    "color:#5a564e; font-size:11px; "
                    "background:transparent; line-height:1.5;")
                cv.addWidget(hook_lbl)

            btn_row = QHBoxLayout()
            btn_row.addStretch(1)
            btn = QPushButton("继续创作 →" if has_draft else "创作 →")
            btn.setMinimumWidth(90)
            btn.setMaximumWidth(120)
            btn.setStyleSheet(
                "QPushButton { background:#1c1b18; color:#ffffff; border:none; "
                "border-radius:6px; font-size:13px; font-weight:700; padding:7px 14px; }"
                "QPushButton:hover { background:#cc785c; color:#ffffff; }"
                "QPushButton:pressed { background:#0f0e0c; }"
            )
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(
                lambda checked=False, ii=idea: self._open_content_studio(ii))
            btn_row.addWidget(btn)
            cv.addLayout(btn_row)
            target_layout.addWidget(card)
        target_layout.addStretch(1)

    def _on_find_content_ideas(self):
        """手动触发雷达,扫今天素材挖选题。后台跑,完成刷新面板。"""
        if getattr(self, "_radar_thread", None) and self._radar_thread.isRunning():
            self.bottom_status.setText("选题雷达正在跑,稍候...")
            return
        self.bottom_status.setText("🔍 正在从今天的语音中寻找短视频选题…")
        from PySide6.QtCore import QThread
        self._radar_thread = QThread(self)
        self._radar_worker = ContentRadarWorker()
        self._radar_worker.moveToThread(self._radar_thread)
        self._radar_thread.started.connect(self._radar_worker.run)
        self._radar_worker.done.connect(self._on_radar_done)
        self._radar_worker.done.connect(self._radar_thread.quit)
        self._radar_worker.failed.connect(self._on_radar_failed)
        self._radar_worker.failed.connect(self._radar_thread.quit)
        self._radar_thread.finished.connect(self._radar_worker.deleteLater)
        self._radar_thread.start()

    def _on_radar_done(self, result: dict):
        sv_added = result.get("sv_added", result.get("added", 0))
        sv = result.get("sv", {})
        err = sv.get("error")
        if err:
            self.bottom_status.setText(f"短视频选题生成出错:{str(err)[:50]}")
        elif sv_added == 0:
            if sv.get("skipped_reason") == "material_too_short":
                self.bottom_status.setText("今天素材太少,还没挖到选题")
            else:
                self.bottom_status.setText("扫完了 · 暂无新选题(已挖过或素材不够)")
        else:
            self.bottom_status.setText(f"✍ 短视频选题完成 · 新增 {sv_added} 条")
        QTimer.singleShot(50, self._reload_content_ideas)

    def _on_radar_failed(self, err: str):
        self.bottom_status.setText(f"选题雷达失败:{err.splitlines()[0][:80]}")

    def _open_content_studio(self, idea: dict):
        """打开内容工坊。关闭后刷新选题面板。"""
        try:
            dlg = ContentStudioDialog(self, idea=idea)
            dlg.exec()
        except NameError:
            QMessageBox.information(
                self, "内容工坊",
                f"选题:{idea.get('title','')}\n\n工坊正在开发中")
            return
        except Exception as e:
            import traceback
            try:
                with open(ROOT / "runtime" / "content-ui.log", "a", encoding="utf-8") as f:
                    f.write(f"studio err: {e}\n{traceback.format_exc()}\n")
            except Exception:
                pass
            QMessageBox.warning(self, "工坊异常", str(e))
            return
        QTimer.singleShot(50, self._reload_content_ideas)

    def _reload_open_questions(self, yest: dt.date = None):
        """从 open_questions pool 加载所有 status=open 的问题。
        yest 参数保留(向后兼容)但不再使用 — pool 是跨天累积的单一源。
        """
        if not feature_enabled("deep_discussion"):
            return
        # 全局 try 兜底 — 这个方法被 dialog 关闭后调,
        # 任何异常逃出都会让 Qt event loop 崩(Qt6Core.dll crash 0xc0000409)
        try:
            self._reload_open_questions_impl()
        except Exception as e:
            import traceback
            try:
                _diag = ROOT / "runtime" / "reload-openq.log"
                with open(_diag, "a", encoding="utf-8") as f:
                    f.write(f"\n[{dt.datetime.now().isoformat(timespec='seconds')}] "
                            f"REFRESH ERROR: {e}\n{traceback.format_exc()}\n")
            except Exception:
                pass

    def _reload_open_questions_impl(self):
        # 清空旧的 — 用 setParent(None) 即时切断,不依赖 deleteLater 时序
        if hasattr(self, "openq_layout") and self.openq_layout is not None:
            while self.openq_layout.count():
                item = self.openq_layout.takeAt(0)
                if item is None:
                    break
                w = item.widget()
                if w is not None:
                    w.setParent(None)   # 立即切断 parent,避免后续访问悬空
                    w.deleteLater()

        # 从 pool 拿所有 open 问题
        try:
            import open_questions as oq
            questions = oq.list_open()
        except Exception as e:
            questions = []
            log_path = ROOT / "runtime" / "reload-openq.log"
            try:
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
                            f"加载 pool 失败:{e}\n")
            except Exception:
                pass

        if not questions:
            self.openq_title.setText("⚑ 待讨论 · 0")
            self.openq_empty = QLabel(
                "暂无待讨论 — AI 会在每天复盘后\n"
                "自动把你「没想清楚的事」列在这里\n\n"
                "解决了或删掉的问题会从列表移除"
            )
            self.openq_empty.setWordWrap(True)
            self.openq_empty.setAlignment(Qt.AlignCenter)
            self.openq_empty.setStyleSheet(
                "color:#8e8a82; font-size:12px; padding:24px 8px; line-height:1.7;"
            )
            self.openq_layout.addWidget(self.openq_empty)
            self.openq_layout.addStretch(1)
            return

        self.openq_title.setText(f"⚑ 待讨论 · {len(questions)}")

        for q in questions:
            title = q.get("title", "(无标题)")
            why = q.get("why_matters", "")
            src_date = q.get("source_date", "")
            has_draft = bool(q.get("draft"))

            card = QFrame()
            card.setStyleSheet(
                "QFrame { background:rgba(28,27,24,0.05); border-radius:8px; }"
                "QFrame:hover { background:rgba(200,150,104,0.10); }"
            )
            cv = QVBoxLayout(card)
            cv.setContentsMargins(12, 10, 10, 10)
            cv.setSpacing(6)

            # 标题(可换行)
            title_lbl = QLabel(title)
            title_lbl.setWordWrap(True)
            title_lbl.setStyleSheet(
                "color:#1c1b18; font-size:13px; font-weight:600; "
                "background:transparent; line-height:1.5;"
            )
            cv.addWidget(title_lbl)

            # 元信息行:来源日期 + (草稿标记)
            meta_bits = []
            if src_date:
                meta_bits.append(f"{src_date} 提出")
            if has_draft:
                meta_bits.append("⚡ 有暂存草稿")
            if meta_bits:
                meta_lbl = QLabel(" · ".join(meta_bits))
                meta_lbl.setStyleSheet(
                    "color:#8e8a82; font-size:10px; background:transparent;")
                cv.addWidget(meta_lbl)

            # 为什么重要(短摘 60 字)
            if why:
                why_short = why if len(why) <= 70 else why[:68] + "…"
                why_lbl = QLabel(f"💡 {why_short}")
                why_lbl.setWordWrap(True)
                why_lbl.setStyleSheet(
                    "color:#5a564e; font-size:11px; "
                    "background:transparent; line-height:1.5;"
                )
                cv.addWidget(why_lbl)

            # 「讨论」按钮(右对齐)
            # 不用 primaryBtn — 橙底白字叠在背景图上看不清
            # 改用 实心深色底(近黑)+ 白字,足够厚重
            btn_row = QHBoxLayout()
            btn_row.addStretch(1)
            btn_label = "继续讨论 →" if has_draft else "讨论 →"
            btn = QPushButton(btn_label)
            btn.setMinimumWidth(90)
            btn.setMaximumWidth(110)
            btn.setStyleSheet(
                "QPushButton {"
                "  background:#1c1b18; color:#ffffff;"
                "  border:none; border-radius:6px;"
                "  font-size:13px; font-weight:700;"
                "  padding:7px 14px;"
                "}"
                "QPushButton:hover { background:#cc785c; color:#ffffff; }"
                "QPushButton:pressed { background:#0f0e0c; }"
            )
            btn.setToolTip("跟 DeepSeek 多轮讨论这个问题,弹独立窗口")
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(
                lambda checked=False, qq=q: self._open_discuss_dialog(qq)
            )
            btn_row.addWidget(btn)
            cv.addLayout(btn_row)

            self.openq_layout.addWidget(card)

        self.openq_layout.addStretch(1)

    def _open_discuss_dialog(self, question: dict):
        """打开讨论闭环对话框。关闭后自动刷新待讨论列表
        (问题可能被 resolved / dismissed / 存了草稿)。"""
        if not feature_enabled("deep_discussion"):
            return
        try:
            dlg = DiscussDialog(self, question=question)
            dlg.exec()
        except NameError:
            QMessageBox.information(
                self, "待讨论",
                f"问题:{question.get('title','')}\n\n讨论功能未加载"
            )
            return
        except Exception as e:
            # 兜底 — 不让 dialog 异常炸 launcher
            import traceback
            try:
                _diag = ROOT / "runtime" / "reload-openq.log"
                with open(_diag, "a", encoding="utf-8") as f:
                    f.write(f"\n[{dt.datetime.now().isoformat(timespec='seconds')}] "
                            f"DIALOG EXEC ERROR: {e}\n{traceback.format_exc()}\n")
            except Exception:
                pass
            QMessageBox.warning(self, "讨论窗口异常",
                f"{e}\n\n详见 runtime/reload-openq.log")
            return
        # 讨论完成后,延迟 50ms 再刷新 —
        # 让 Qt 把 dialog 关闭的事件队列清完再操作 openq_layout,
        # 避免 deleteLater 时序冲突 → Qt6Core.dll crash
        QTimer.singleShot(50, self._reload_open_questions)

    def _open_yest_md(self):
        """打开与两套视图完全一致的昨日复盘成品。"""
        source = resolve_yesterday_brief(ROOT)
        if source.has_output:
            import os
            open_path(str(source.output_path))
        else:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(
                self,
                "提示",
                f"昨日（{source.expected_day}）{pending_hint(source)}",
            )

    def _set_brief_views(
        self, card_id: str, text: str, placeholder: str
    ) -> None:
        classic = (
            self.brief_text
            if card_id == "today_brief"
            else self.yest_text
        )
        card = getattr(
            self,
            "card_brief_text"
            if card_id == "today_brief"
            else "card_yest_text",
            None,
        )
        for view in (classic, card):
            if view is None:
                continue
            view.setPlaceholderText(placeholder)
            view.setPlainText(text)

    def _reload_brief(self):
        """从唯一成品源同时刷新经典视图和卡片工作台。"""
        try:
            source = resolve_today_brief(ROOT, knowledge_dir())
            if not source.has_output:
                self._set_brief_views(
                    "today_brief",
                    "",
                    f"今日（{source.expected_day}）{pending_hint(source)}",
                )
                return
            result = render_today_brief(source.content)
            if result:
                if source.is_stale:
                    reason = pending_hint(source)
                    result = (
                        f"【尚未更新到 {source.expected_day}】{reason}\n\n"
                        f"{result}"
                    )
                self._set_brief_views("today_brief", result, "")
            else:
                self._set_brief_views(
                    "today_brief",
                    "",
                    "今日简报暂时没有可展示的内容，点击“生成”重新整理。",
                )
        except Exception as ex:
            self._set_brief_views(
                "today_brief", f"（读取简报失败：{ex}）", ""
            )
        finally:
            if self._sync_legacy_card("today_brief"):
                board = getattr(self, "card_board", None)
                if board is not None:
                    board.refresh()

    def _reload_todo(self):
        """从待办总览.md 同时加载待办和已办组件。"""
        try:
            p = knowledge_dir() / "待办总览.md"
            self.todo_widget.load(p)
            self.done_widget.load(p)
            card_todo = getattr(self, "card_todo_widget", None)
            card_done = getattr(self, "card_done_widget", None)
            if card_todo is not None:
                card_todo.load(p)
            if card_done is not None:
                card_done.load(p)
        except Exception:
            pass

    def _on_brief_focus_out(self, event):
        """简报文本框失去焦点时，把内容保存回 每日简报.md。"""
        from PySide6.QtWidgets import QPlainTextEdit as _PE
        _PE.focusOutEvent(self.brief_text, event)
        try:
            p = knowledge_dir() / "每日简报.md"
            content = self.brief_text.toPlainText()
            if content.strip():
                p.write_text(content, encoding="utf-8")
        except Exception:
            pass

    def _on_todo_text_changed(self, text: str):
        """检测输入了 @ → 弹现有待办列表。"""
        prev = getattr(self, "_last_input_len", 0)
        self._last_input_len = len(text)
        if len(text) <= prev:
            return
        if not text.endswith("@"):
            return
        if self.todo_picker.isVisible() or self.todo_calendar.isVisible():
            return
        # 从 TodoWidget 拿当前待办列表
        pending = getattr(self.todo_widget, "_pending", []) or []
        if not pending:
            self.bottom_status.setText("当前没有待办，无法用 @ 加 deadline")
            # 把刚才那个 @ 删掉
            self.todo_input.setText(text[:-1])
            return
        # 填充列表
        self.todo_picker.clear()
        for item in pending:
            dl = item.get("deadline", "")
            label = item["text"]
            if dl:
                label = f"{label}  · 已有 deadline {dl}"
            li = QListWidgetItem(label)
            li.setData(Qt.UserRole, item["raw"])   # 把原始 MD 行存起来
            self.todo_picker.addItem(li)
        # 弹出位置：输入框下方
        pos = self.todo_input.mapToGlobal(self.todo_input.rect().bottomLeft())
        self.todo_picker.move(pos)
        self.todo_picker.show()
        self.todo_picker.setFocus()
        self.todo_picker.setCurrentRow(0)

    def _on_todo_picker_picked(self, item: QListWidgetItem):
        """选中一条现有待办 → 关闭列表，弹日历。"""
        self._editing_todo_raw = item.data(Qt.UserRole)
        self.todo_picker.hide()
        # 输入框里把 @ 清掉（防止干扰下一步操作）
        text = self.todo_input.text()
        if text.endswith("@"):
            self.todo_input.setText(text[:-1])
        # 弹日历
        pos = self.todo_input.mapToGlobal(self.todo_input.rect().bottomLeft())
        self.todo_calendar.move(pos)
        self.todo_calendar.setSelectedDate(dt.date.today())
        self.todo_calendar.show()
        self.todo_calendar.setFocus()

    def _on_todo_calendar_picked(self, qdate):
        """日历选完日期 → 给选中的那条待办在 MD 文件里加/改 deadline。"""
        if not self._editing_todo_raw:
            self.todo_calendar.hide()
            return
        iso = qdate.toString("yyyy-MM-dd")
        target_raw = self._editing_todo_raw
        self._editing_todo_raw = None
        self.todo_calendar.hide()

        # 写回 MD
        try:
            p = knowledge_dir() / "待办总览.md"
            if not p.exists():
                return
            import re as _re
            lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
            new_lines = []
            updated = False
            for ln in lines:
                if ln.rstrip("\n") == target_raw and not updated:
                    # 如果已有 deadline:xxxx-xx-xx，替换；没有则在末尾追加
                    if _re.search(r"deadline[：:]\s*\d{4}-\d{2}-\d{2}", ln):
                        ln = _re.sub(
                            r"deadline[：:]\s*\d{4}-\d{2}-\d{2}",
                            f"deadline:{iso}", ln
                        )
                    else:
                        # 在行末（去掉换行）之前插入 (deadline:xxx)
                        stripped = ln.rstrip("\n")
                        ln = f"{stripped} (deadline:{iso})\n"
                    updated = True
                new_lines.append(ln)
            if updated:
                p.write_text("".join(new_lines), encoding="utf-8")
                self.bottom_status.setText(f"已为待办设置 deadline · {iso}")
                self._reload_todo()
            else:
                self.bottom_status.setText("未能匹配到目标待办（可能已被改动）")
        except Exception as ex:
            self.bottom_status.setText(f"设置 deadline 失败：{ex}")

    def _parse_deadline(self, raw: str) -> tuple[str, str]:
        """从待办输入里解析 @deadline 部分。返回 (剩余文本, deadline_iso)。"""
        import re
        today = dt.date.today()
        # 匹配各种 @xxx 格式
        m = re.search(r"@(\S+)", raw)
        if not m:
            return raw.strip(), ""
        token = m.group(1)
        clean = (raw[:m.start()] + raw[m.end():]).strip()
        dl = ""
        if token == "今天":
            dl = today.isoformat()
        elif token == "明天":
            dl = (today + dt.timedelta(days=1)).isoformat()
        elif token == "后天":
            dl = (today + dt.timedelta(days=2)).isoformat()
        elif token.endswith("天后"):
            try:
                n = int(token[:-2])
                dl = (today + dt.timedelta(days=n)).isoformat()
            except Exception:
                pass
        elif re.match(r"^\d{4}-\d{2}-\d{2}$", token):
            dl = token
        elif re.match(r"^\d{2}-\d{2}$", token):
            # MM-DD 默认今年；如果已经过去，落到明年
            try:
                m_, d_ = map(int, token.split("-"))
                cand = dt.date(today.year, m_, d_)
                if cand < today:
                    cand = dt.date(today.year + 1, m_, d_)
                dl = cand.isoformat()
            except Exception:
                pass
        return clean, dl

    def _add_todo_item(self):
        """在待办总览.md 里追加一条新待办，然后刷新列表。"""
        text = self.todo_input.text().strip()
        if not text:
            return
        try:
            p = knowledge_dir() / "待办总览.md"
            today = dt.date.today().isoformat()
            # 解析 @deadline
            clean_text, deadline = self._parse_deadline(text)
            if deadline:
                new_line = f"- [ ] {clean_text} (deadline:{deadline}) (来源：{today})\n"
            else:
                new_line = f"- [ ] {clean_text} (来源：{today})\n"
            if p.exists():
                content = p.read_text(encoding="utf-8")
                # 插到「待完成」section 末尾
                import re
                lines = content.splitlines(keepends=True)
                # 找到待完成 section 下一个 ## 之前插入
                insert_at = len(lines)
                in_pending = False
                for i, ln in enumerate(lines):
                    s = ln.strip()
                    if re.search(r"^##.*待完成", s) or re.search(r"^##.*待办", s):
                        in_pending = True
                    elif s.startswith("##") and in_pending:
                        insert_at = i
                        break
                lines.insert(insert_at, new_line)
                p.write_text("".join(lines), encoding="utf-8")
            else:
                p.write_text(
                    f"# 待办总览\n> 最后更新：{today}\n## 待完成\n{new_line}## 本周已完成\n- 无\n",
                    encoding="utf-8")
            self.todo_input.clear()
            self._reload_todo()
        except Exception as ex:
            self.bottom_status.setText(f"新增待办失败：{ex}")

    def _open_todo_capture_dialog(self):
        """打开本地对话式待办入口；写入前必须由用户确认。"""
        planned_ends = []
        for item in getattr(self.todo_widget, "_pending", []) or []:
            planned = str(item.get("planned", "") or "")
            if re.fullmatch(r"\d{2}:\d{2}-\d{2}:\d{2}", planned):
                planned_ends.append(planned[-5:])
        dialog = TodoCaptureDialog(
            self,
            occupied_until=max(planned_ends) if planned_ends else "",
        )
        dialog.tasksConfirmed.connect(self._save_today_conversation_todos)
        dialog.exec()

    def _save_today_conversation_todos(self, tasks: list):
        """把确认后的对话任务写入待办总览，并默认标记为今天。"""
        normalized = []
        seen = set()
        for task in tasks:
            if isinstance(task, dict):
                payload = dict(task)
                text = str(payload.get("title") or "").strip()
            else:
                text = str(task or "").strip()
                payload = {"title": text}
            if text and text not in seen:
                payload["title"] = text
                normalized.append(payload)
                seen.add(text)
        if not normalized:
            return
        try:
            p = knowledge_dir() / "待办总览.md"
            today = dt.date.today().isoformat()
            new_lines = []
            for task in normalized:
                metadata = []
                priority = str(task.get("priority") or "").strip()
                planned = str(task.get("planned") or "").strip()
                duration = int(task.get("duration") or 0)
                if priority:
                    metadata.append(f"(priority:{priority})")
                if planned:
                    metadata.append(f"(planned:{planned})")
                if duration:
                    metadata.append(f"(duration:{duration}m)")
                meta_text = " ".join(metadata)
                task_deadline = (
                    (dt.date.today() + dt.timedelta(days=1)).isoformat()
                    if planned.startswith("明天 ") else today
                )
                new_lines.append(
                    f"- [ ] {task['title']} (deadline:{task_deadline}) {meta_text} "
                    f"(来源：今日计划 {today})\n"
                )
            if p.exists():
                lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
                import re
                pending_header = None
                next_section = len(lines)
                for i, line in enumerate(lines):
                    heading = line.strip()
                    if pending_header is None and (
                        re.search(r"^##.*待完成", heading)
                        or re.search(r"^##.*待办", heading)
                    ):
                        pending_header = i
                    elif pending_header is not None and heading.startswith("##"):
                        next_section = i
                        break
                if pending_header is None:
                    if lines and lines[-1].strip():
                        lines.append("\n")
                    lines.append("## 待完成\n")
                    lines.extend(new_lines)
                else:
                    lines[next_section:next_section] = new_lines
                p.write_text("".join(lines), encoding="utf-8")
            else:
                p.write_text(
                    f"# 待办总览\n> 最后更新：{today}\n## 待完成\n"
                    + "".join(new_lines)
                    + "## 本周已完成\n",
                    encoding="utf-8",
                )
            self._reload_todo()
            self.bottom_status.setText(
                f"已按优先级和时间保存 {len(normalized)} 件今日待办；完成后直接勾选即可。"
            )
        except Exception as ex:
            self.bottom_status.setText(f"保存今日待办失败：{ex}")

    def _open_brief_md(self):
        """用系统默认程序打开每日简报文件。"""
        p = knowledge_dir() / "每日简报.md"
        if not p.exists():
            p = note_path(dt.date.today())
        if p.exists():
            open_path(str(p))
        else:
            open_path(str(ROOT / "notes"))
            QMessageBox.information(self, "还没有今日总结", "已打开笔记文件夹。运行一次“立即总结今天”后，这里会生成 Markdown 文件。")

    def _open_todo_md(self):
        """用系统默认程序打开待办总览文件。"""
        p = knowledge_dir() / "待办总览.md"
        if not p.exists():
            today = dt.date.today().isoformat()
            p.write_text(f"# 待办总览\n> 最后更新：{today}\n## 待完成\n## 本周已完成\n", encoding="utf-8")
        open_path(str(p))

    def _open_wiki(self):
        """用系统默认程序打开第二大脑文件夹。"""
        open_path(str(knowledge_dir()))

    def _build_topbar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("topbar")
        bar.setFixedHeight(56)
        hl = QHBoxLayout(bar)
        hl.setContentsMargins(20, 10, 20, 10)
        hl.setSpacing(12)

        # 品牌
        brand_box = QVBoxLayout()
        brand_box.setSpacing(0)
        title = QLabel("声年")
        title.setObjectName("brandTitle")
        sub = QLabel("你的 AI 语音知识库 · 说出来，自动整理")
        sub.setObjectName("brandSub")
        brand_box.addWidget(title)
        brand_box.addWidget(sub)
        hl.addLayout(brand_box)

        hl.addSpacing(20)

        # 信号灯
        self.signal_dot = SignalDot()
        hl.addWidget(self.signal_dot)
        self.signal_label = QLabel("未启动")
        self.signal_label.setObjectName("signalText")
        hl.addWidget(self.signal_label)

        hl.addStretch(1)

        # 卡片透明度滑杆（拖一下立刻看到背景透出来更多/更少）
        opacity_lbl = QLabel("背景")
        opacity_lbl.setStyleSheet("color:#8e8a82; font-size:12px;")
        hl.addWidget(opacity_lbl)
        self.opacity_slider = QSlider(Qt.Horizontal)
        self.opacity_slider.setRange(0, 100)   # 全开 0%-100% 卡片不透明度
        self.opacity_slider.setValue(94)
        self.opacity_slider.setFixedWidth(120)
        self.opacity_slider.setToolTip("拖动调整卡片透明度（越往左越透）")
        self.opacity_slider.valueChanged.connect(self._on_opacity_changed)
        hl.addWidget(self.opacity_slider)
        self.opacity_value_lbl = QLabel("94%")
        self.opacity_value_lbl.setStyleSheet(
            "color:#5a564e; font-size:12px; font-family:'Consolas'; min-width:36px;")
        hl.addWidget(self.opacity_value_lbl)

        hl.addSpacing(12)

        # 换背景按钮
        self.btn_change_bg = QPushButton("换背景")
        self.btn_change_bg.setObjectName("ghostBtn")
        self.btn_change_bg.setToolTip("选一张本地图片作为背景（自动模糊处理）")
        self.btn_change_bg.clicked.connect(self._on_change_background)
        hl.addWidget(self.btn_change_bg)

        # 字体大小：仅缩放文字，不改变卡片宽度、拖拽规则和用户内容。
        self.btn_font = QPushButton("字体")
        self.btn_font.setObjectName("ghostBtn")
        self.btn_font.setToolTip("调整全局文字大小；选择会自动记住")
        font_menu = QMenu(self.btn_font)
        font_labels = {0.9: "小", 1.0: "标准", 1.15: "大"}
        for scale in FONT_SCALES:
            action = font_menu.addAction(font_labels.get(scale, f"{scale:.0%}"))
            action.setCheckable(True)
            action.setChecked(scale == self._font_scale)
            action.triggered.connect(
                lambda _checked=False, selected=scale: self._set_font_scale(selected)
            )
            self._font_scale_actions[scale] = action
        self.btn_font.setMenu(font_menu)
        hl.addWidget(self.btn_font)

        # 麦克风设备按钮（自动切换 + 手动补救选设备）
        self.btn_mic = QPushButton("麦克风")
        self.btn_mic.setObjectName("ghostBtn")
        self.btn_mic.setToolTip("查看当前用的麦 + 手动切换设备(拔了 DJI 自动没切时,来这手动选)")
        self.btn_mic.clicked.connect(self._on_pick_mic)
        hl.addWidget(self.btn_mic)

        # 热词编辑按钮
        self.btn_hotwords = QPushButton("热词")
        self.btn_hotwords.setObjectName("ghostBtn")
        self.btn_hotwords.setToolTip("编辑 hotwords.txt — 加常说的人名/术语，提升识别准确率")
        self.btn_hotwords.clicked.connect(self._on_edit_hotwords)
        hl.addWidget(self.btn_hotwords)

        # 贴链接按钮（抖音/B站/视频号/公众号 自动抓取入库）
        self.btn_paste_url = QPushButton("+ 贴链接")
        self.btn_paste_url.setObjectName("ghostBtn")
        self.btn_paste_url.setToolTip(
            "粘贴一个抖音/B 站/视频号/公众号链接，自动抓取内容入库\n"
            "作为「外部·xxx」归入「转述/学习输入」，参与日报"
        )
        self.btn_paste_url.clicked.connect(self._on_paste_url)
        hl.addWidget(self.btn_paste_url)

        self.btn_files = QPushButton("我的文件")
        self.btn_files.setObjectName("ghostBtn")
        self.btn_files.setToolTip("查看录音、转写、总结和内容文件；可选使用 Obsidian 管理笔记")
        self.btn_files.clicked.connect(self._on_files)
        hl.addWidget(self.btn_files)

        self.btn_account = QPushButton("API 配置")
        self.btn_account.setObjectName("ghostBtn")
        self.btn_account.setToolTip("查看自有 API Key 的配置状态")
        self.btn_account.clicked.connect(self._on_account)
        hl.addWidget(self.btn_account)

        self.btn_update = QPushButton("检查更新")
        self.btn_update.setObjectName("ghostBtn")
        self.btn_update.setToolTip("立即检查新版；软件运行期间也会每 4 小时自动检查")
        self.btn_update.clicked.connect(lambda: self._check_update_async(manual=True))
        self.btn_update.setVisible(
            is_commercial_mode()
            and bool(str(CONFIG.get("account", {}).get("update_manifest_url", "")).strip())
        )
        hl.addWidget(self.btn_update)

        self.btn_about = QPushButton("关于")
        self.btn_about.setObjectName("ghostBtn")
        self.btn_about.setToolTip("版本、第三方组件、开源许可证与对应源码")
        self.btn_about.clicked.connect(self._on_about)
        hl.addWidget(self.btn_about)

        hl.addSpacing(16)

        # 时钟
        self.clock_label = QLabel("")
        self.clock_label.setObjectName("clock")
        hl.addWidget(self.clock_label)

        return bar

    def _on_edit_hotwords(self):
        """打开热词编辑弹窗。保存后重启 transcriber 生效。"""
        dlg = HotwordsDialog(self)
        dlg.exec()

    def _on_pick_mic(self):
        """打开麦克风设备选择弹窗(手动切换 / 恢复自动)。"""
        try:
            dlg = MicDeviceDialog(self)
            dlg.exec()
        except Exception as e:
            QMessageBox.warning(self, "麦克风", f"打开设备列表失败:{e}")

    def _on_account(self):
        """显示用户自有 API 的配置状态。"""
        if sys.platform == "darwin":
            from api_settings import show_api_dialog
            return show_api_dialog(self, ROOT)
        from ai_gateway import provider_api_key

        configured = bool(provider_api_key("DEEPSEEK_API_KEY").strip())
        message = (
            "声年开源版不提供账号、收费、套餐或 Token 网关。\n\n"
            "AI 文字从本机直接发送到你配置的 DeepSeek API；"
            "录音和本地转写不会上传。\n\n"
            + ("已检测到 DEEPSEEK_API_KEY。" if configured else
               "尚未检测到 DEEPSEEK_API_KEY。请按 README 配置后重启声年。")
        )
        QMessageBox.information(self, "自有 API 配置", message)
        return QDialog.DialogCode.Accepted

    def _card_generation_gate(self, _card_id: str) -> bool:
        """在 GUI 线程检查用户自己的 API Key。"""
        if not self._ensure_cloud_ai_enabled(interactive=True):
            return False
        from ai_gateway import provider_api_key

        if provider_api_key("DEEPSEEK_API_KEY").strip():
            return True
        message = "未找到本机 DEEPSEEK_API_KEY，请先配置 DeepSeek API Key"
        self.bottom_status.setText(message)
        QMessageBox.warning(self, "API 未配置", message)
        return False

    def _ensure_cloud_ai_enabled(self, *, interactive: bool) -> bool:
        """开源版由用户自行配置 API；本地功能不需要产品账号授权。"""
        return True

    def _on_files(self):
        """显示本地存储说明，并提供一键打开入口。"""
        from onboarding import StorageGuideDialog

        StorageGuideDialog(ROOT, self).exec()

    def _legal_dir(self) -> Path:
        """返回源码模式或冻结安装包中的许可证材料目录。"""
        if getattr(sys, "frozen", False):
            candidates = [
                Path(sys.executable).resolve().parent / "legal",
                RESOURCE_ROOT / "legal",
            ]
        else:
            candidates = [RESOURCE_ROOT / "packaging" / "legal"]
        return next((path for path in candidates if path.exists()), candidates[0])

    def _on_about(self):
        box = QMessageBox(self)
        box.setWindowTitle("关于声年")
        box.setIcon(QMessageBox.Information)
        box.setText(f"声年\n你的 AI 语音知识库\n说出来，自动整理\n版本 {APP_VERSION}")
        box.setInformativeText(
            "本软件包含依据 LGPL、MIT、Apache-2.0、BSD 等许可证提供的"
            "第三方组件。声年自有业务代码保持闭源。"
        )
        license_button = box.addButton("开源许可证", QMessageBox.ActionRole)
        source_button = box.addButton("对应源码说明", QMessageBox.ActionRole)
        complaint_button = box.addButton("投诉 / 举报", QMessageBox.ActionRole)
        box.addButton("关闭", QMessageBox.RejectRole)
        box.exec()
        selected = box.clickedButton()
        target = None
        if selected is license_button:
            target = self._legal_dir() / "THIRD_PARTY_NOTICES.md"
        elif selected is source_button:
            target = self._legal_dir() / "SOURCE-OFFER.md"
        elif selected is complaint_button:
            webbrowser.open("https://github.com/kingkk469/shengnian/issues")
            return
        if target is not None:
            if target.exists():
                open_path(str(target))
            else:
                QMessageBox.warning(self, "许可证材料缺失", f"找不到文件：\n{target}")

    def _clear_ingest_pause(self):
        """清掉「贴链接自动暂停」的残留标记(只清 ingest-auto,不动用户手动暂停)。
        场景:贴链接中途程序崩了,标记没清 → 重启后录音一直显示暂停。"""
        try:
            fp = pause_flag()
            if fp.exists():
                try:
                    mark = fp.read_text(encoding="utf-8").strip()
                except Exception:
                    mark = ""
                if mark == "ingest-auto":
                    fp.unlink()
        except Exception:
            pass

    def _on_ingest_bg_done(self, result: dict):
        """贴链接窗口被提前关闭后,后台抓取完成的回调(receiver=主窗口,线程安全)。"""
        self._clear_ingest_pause()
        try:
            src_cn = {
                "douyin": "抖音", "bilibili": "B 站", "wechat": "公众号",
                "wechat_channels": "视频号",
            }.get(
                result.get("source", ""), "")
            self.bottom_status.setText(
                f"后台抓取完成 · {src_cn} · {str(result.get('title', ''))[:24]} · 已入输入档案")
        except Exception:
            pass

    def _on_ingest_bg_failed(self, err: str):
        self._clear_ingest_pause()
        try:
            self.bottom_status.setText(f"后台抓取失败:{err.splitlines()[0][:60]}")
        except Exception:
            pass

    def _on_paste_url(self):
        """打开「贴链接抓内容」弹窗，自动识别抖音/B 站/视频号/公众号。
        非模态:抓取期间主窗口照常可用,不锁界面。"""
        old = getattr(self, "_ingest_dlg", None)
        if old is not None and old.isVisible():
            old.raise_()
            old.activateWindow()
            return
        # 默认值：剪贴板里如果有 http 链接，直接预填
        try:
            cb = QApplication.clipboard()
            clip = (cb.text() or "").strip()
        except Exception:
            clip = ""
        if not (clip.startswith("http://") or clip.startswith("https://")):
            clip = ""
        dlg = IngestUrlDialog(self, default_url=clip)
        self._ingest_dlg = dlg   # 持引用防 GC(非模态 show 不阻塞)
        dlg.show()

    def _on_change_background(self):
        """选一张本地图片做背景。先问要不要做处理（模糊+白雾），不处理就保留原图。"""
        from PySide6.QtWidgets import QFileDialog
        from PySide6.QtGui import QPalette, QPixmap, QBrush

        src_path, _ = QFileDialog.getOpenFileName(
            self, "选择背景图",
            str(Path.home()),
            "图片 (*.png *.jpg *.jpeg *.webp *.bmp)"
        )
        if not src_path:
            return

        # 问用户要不要处理
        from PySide6.QtWidgets import QMessageBox
        box = QMessageBox(self)
        box.setWindowTitle("背景处理")
        box.setText("要对这张图做处理吗？")
        box.setInformativeText(
            "「保留原图」 — 直接用，背景能完整看清，但卡片上文字可能受影响\n"
            "「柔化处理」 — 模糊+脱色+叠白雾，最适合做 UI 底图"
        )
        btn_raw = box.addButton("保留原图", QMessageBox.AcceptRole)
        btn_processed = box.addButton("柔化处理", QMessageBox.AcceptRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked not in (btn_raw, btn_processed):
            return
        keep_raw = (clicked == btn_raw)

        try:
            from PIL import Image, ImageEnhance, ImageFilter
            img = Image.open(src_path).convert("RGB")
            img = img.resize((1920, 1160), Image.LANCZOS)
            if not keep_raw:
                # 柔化处理
                img = img.filter(ImageFilter.GaussianBlur(radius=6))
                img = ImageEnhance.Brightness(img).enhance(1.00)
                img = ImageEnhance.Color(img).enhance(0.75)
                overlay = Image.new("RGB", img.size, (250, 247, 240))
                img = Image.blend(img, overlay, 0.40)
            dst = ROOT / "assets" / "backgrounds" / "custom-bg.png"
            dst.parent.mkdir(parents=True, exist_ok=True)
            img.save(dst, optimize=True)
        except Exception as ex:
            QMessageBox.warning(self, "处理失败", f"图片处理出错：{ex}")
            return

        # 即时刷新窗口背景（直接更新 _bg_pixmap + 重绘）
        self._bg_pixmap = QPixmap(str(dst))
        self.update()
        mode = "原图" if keep_raw else "柔化处理"
        self.bottom_status.setText(f"背景已换 · {mode} · {Path(src_path).name}")

    def _on_opacity_changed(self, value: int):
        """滑杆拖动时实时改卡片透明度。value 是 50-100 的整数。"""
        self._apply_visual_styles(value)
        self.opacity_value_lbl.setText(f"{value}%")

    def _apply_visual_styles(self, opacity_value: int | None = None) -> None:
        """统一应用字体档位与背景透明度，避免两项设置互相覆盖。"""

        value = (
            int(opacity_value)
            if opacity_value is not None
            else int(getattr(self, "opacity_slider", None).value())
            if getattr(self, "opacity_slider", None) is not None
            else 94
        )
        scaled_qss = scale_stylesheet_font_sizes(QSS, self._font_scale)
        app = QApplication.instance()
        if app is not None:
            base_font = app.font()
            base_font.setPointSizeF(11.0 * self._font_scale)
            app.setFont(base_font)
        a = value / 100.0
        # 卡片层（statusCard + infoPanel）
        card_qss = (
            f"QFrame#statusCard, QFrame#infoPanel {{ "
            f"background-color: rgba(255, 255, 255, {a:.2f}); "
            f"border: 1px solid rgba(28, 27, 24, 0.08); "
            f"border-radius: 10px; }}"
        )
        # 顶栏跟着同步（稍微更不透明一些，差 0.04）
        top_a = min(1.0, a + 0.04)
        top_qss = (
            f"QFrame#topbar {{ "
            f"background-color: rgba(250, 249, 245, {top_a:.2f}); "
            f"border-bottom: 1px solid rgba(28, 27, 24, 0.08); }}"
        )
        # 把这段加到 QApplication 全局样式表末尾（追加优先级最高）
        if app is not None:
            app.setStyleSheet(scaled_qss + "\n" + card_qss + "\n" + top_qss)
        board = getattr(self, "card_board", None)
        if board is not None and hasattr(board, "set_font_scale"):
            board.set_font_scale(self._font_scale)

    def _set_font_scale(self, scale: float) -> None:
        """保存并即时应用小／标准／大三档全局字体。"""

        self._font_scale = save_font_scale(ROOT, scale)
        for value, action in self._font_scale_actions.items():
            action.setChecked(value == self._font_scale)
        self._apply_visual_styles()
        labels = {0.9: "小", 1.0: "标准", 1.15: "大"}
        self.bottom_status.setText(
            f"字体已调整为“{labels.get(self._font_scale, '标准')}”，下次启动继续使用"
        )

    def _build_status_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("statusCard")
        hl = QHBoxLayout(card)
        hl.setContentsMargins(20, 14, 20, 14)
        hl.setSpacing(36)

        def cell(label_text: str) -> tuple[QVBoxLayout, QLabel]:
            box = QVBoxLayout()
            box.setSpacing(2)
            l = QLabel(label_text)
            l.setStyleSheet("color:#8e8a82; font-size:12px; letter-spacing:2px;")
            v = QLabel("—")
            v.setProperty("class", "statusValue")
            v.setStyleSheet("color:#1c1b18; font-size:15px; font-weight:700;")
            box.addWidget(l)
            box.addWidget(v)
            return box, v

        rec_box, self.rec_value = cell("录音器")
        tr_box, self.tr_value = cell("转写器")
        pause_box, self.pause_value = cell("状态")
        dev_box, self.dev_value = cell("当前设备")

        hl.addLayout(rec_box)
        hl.addLayout(tr_box)
        hl.addLayout(pause_box)
        hl.addLayout(dev_box)
        hl.addStretch(1)

        return card

    def _build_action_row(self) -> QHBoxLayout:
        hl = QHBoxLayout()
        hl.setSpacing(8)

        self.btn_start = QPushButton("启动录音 + 转写")
        self.btn_start.setObjectName("primaryBtn")
        self.btn_start.clicked.connect(self._start_all)
        self.btn_start.setMinimumHeight(36)

        self.btn_stop = QPushButton("全部停止")
        self.btn_stop.setObjectName("dangerBtn")
        self.btn_stop.clicked.connect(self._stop_all)
        self.btn_stop.setMinimumHeight(36)

        self.btn_pause = QPushButton("暂停 / 恢复")
        self.btn_pause.clicked.connect(self._toggle_pause)
        self.btn_pause.setMinimumHeight(36)

        self.btn_import_audio = QPushButton("导入录音")
        self.btn_import_audio.setToolTip(
            "导入手机、录音笔或会议录音，在本机使用 FunASR 转写；录音不会上传"
        )
        self.btn_import_audio.clicked.connect(self._on_import_audio)
        self.btn_import_audio.setMinimumHeight(36)

        hl.addWidget(self.btn_start)
        hl.addWidget(self.btn_stop)
        hl.addWidget(self.btn_pause)
        hl.addWidget(self.btn_import_audio)
        hl.addStretch(1)

        return hl

    def _on_import_audio(self):
        """选择手机/录音笔文件；分析和转换都放到后台线程。"""
        if self._audio_import_running:
            self.bottom_status.setText("已有录音正在导入，请稍候")
            return
        from PySide6.QtWidgets import QFileDialog
        from audio_import import SUPPORTED_AUDIO_SUFFIXES

        patterns = " ".join(f"*{suffix}" for suffix in sorted(SUPPORTED_AUDIO_SUFFIXES))
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "导入录音 · 全程本地处理",
            str(Path.home()),
            f"录音文件 ({patterns});;所有文件 (*)",
        )
        if paths:
            self._begin_audio_probe([Path(path) for path in paths])

    def _begin_audio_probe(self, paths: list[Path]):
        if self._audio_import_running or not paths:
            return
        self._audio_import_running = True
        self.btn_import_audio.setEnabled(False)
        self.bottom_status.setText(
            f"正在本机读取 {len(paths)} 段录音的信息…不会上传"
        )

        def work():
            try:
                from audio_import import probe_recordings

                self.audio_probe_finished.emit(probe_recordings(paths))
            except Exception as exc:
                self.audio_import_failed.emit(str(exc))

        threading.Thread(
            target=work,
            daemon=True,
            name="audio-import-probe",
        ).start()

    def _on_audio_probe_finished(self, guesses):
        """可信时间直接使用；低可信时间在 GUI 线程内逐段确认。"""
        selections = []
        for guess in list(guesses or []):
            selected_time = None
            if guess.needs_confirmation:
                dialog = RecordingTimeDialog(guess, self)
                if dialog.exec() != QDialog.DialogCode.Accepted:
                    continue
                selected_time = dialog.selected_datetime()
            selections.append((guess, selected_time))
        if not selections:
            self._finish_audio_import_ui("没有导入录音")
            return
        self.bottom_status.setText(
            f"正在本机准备 {len(selections)} 段录音并加入转写队列…"
        )

        def work():
            results = []
            failures = []
            from audio_import import import_recording

            for guess, selected_time in selections:
                try:
                    results.append(
                        import_recording(
                            guess,
                            ROOT,
                            recorded_at=selected_time,
                        )
                    )
                except Exception as exc:
                    failures.append(f"{guess.path.name}：{exc}")
            self.audio_import_finished.emit(
                {"results": results, "failures": failures}
            )

        threading.Thread(
            target=work,
            daemon=True,
            name="audio-import-convert",
        ).start()

    def _on_audio_import_finished(self, payload):
        if isinstance(payload, dict):
            results = list(payload.get("results") or [])
            failures = list(payload.get("failures") or [])
        else:
            results = list(payload or [])
            failures = []
        new_count = sum(not result.duplicate for result in results)
        duplicate_count = len(results) - new_count

        self.transcriber.adopt_running()
        if new_count and not self.transcriber.is_running():
            if not self.transcriber.start():
                self._finish_audio_import_ui(
                    "录音已经保存在本机，但转写器启动失败"
                )
                QMessageBox.warning(
                    self,
                    "录音已保存，转写尚未开始",
                    f"{self.transcriber.last_error}\n\n"
                    "请稍后点击“启动录音 + 转写”，已导入的录音会自动补转写。",
                )
                return

        parts = []
        if new_count:
            parts.append(f"{new_count} 段已进入本地转写队列")
        if duplicate_count:
            parts.append(f"{duplicate_count} 段重复文件已跳过")
        if failures:
            parts.append(f"{len(failures)} 段未能读取")
        message = "；".join(parts) or "录音导入完成"
        self._finish_audio_import_ui(message)
        detail = (
            f"{message}。\n\n"
            "录音原文件和转写结果只保存在这台电脑。长录音的本地识别需要一些时间，"
            "你可以继续使用其他功能。"
        )
        if failures:
            detail += "\n\n未导入：\n" + "\n".join(failures[:5])
            QMessageBox.warning(self, "部分录音未导入", detail)
        else:
            QMessageBox.information(self, "导入录音完成", detail)

    def _on_audio_import_failed(self, detail: str):
        self._finish_audio_import_ui("导入录音失败")
        QMessageBox.warning(
            self,
            "导入录音失败",
            f"{detail[:1000]}\n\n原文件没有被修改。",
        )

    def _finish_audio_import_ui(self, message: str):
        self._audio_import_running = False
        if hasattr(self, "btn_import_audio"):
            self.btn_import_audio.setEnabled(True)
        self.bottom_status.setText(message)

    def dragEnterEvent(self, event):
        """允许把本地录音直接拖进主窗口，不抢占 Skill 的拖拽入口。"""
        try:
            from audio_import import is_supported_audio

            urls = event.mimeData().urls()
            paths = [Path(url.toLocalFile()) for url in urls if url.isLocalFile()]
            if paths and all(path.is_file() and is_supported_audio(path) for path in paths):
                event.acceptProposedAction()
                return
        except Exception:
            pass
        super().dragEnterEvent(event)

    def dropEvent(self, event):
        try:
            from audio_import import is_supported_audio

            paths = [
                Path(url.toLocalFile())
                for url in event.mimeData().urls()
                if url.isLocalFile()
            ]
            if paths and all(path.is_file() and is_supported_audio(path) for path in paths):
                event.acceptProposedAction()
                self._begin_audio_probe(paths)
                return
        except Exception as exc:
            self._on_audio_import_failed(str(exc))
            return
        super().dropEvent(event)

    def _build_today_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("statusCard")
        hl = QHBoxLayout(card)
        hl.setContentsMargins(20, 14, 20, 14)
        hl.setSpacing(20)

        today_box = QHBoxLayout()
        today_box.setSpacing(12)
        today_lbl = QLabel("今 日")
        today_lbl.setStyleSheet("color:#8e8a82; font-size:12px; letter-spacing:4px; font-weight:600;")
        self.today_num = QLabel("0")
        self.today_num.setObjectName("todayNum")
        seg_lbl = QLabel("段")
        seg_lbl.setStyleSheet("color:#3a3833; font-size:13px;")
        self.today_chars = QLabel("0")
        self.today_chars.setObjectName("todayNum")
        chars_lbl = QLabel("字")
        chars_lbl.setStyleSheet("color:#3a3833; font-size:13px;")
        today_box.addWidget(today_lbl)
        today_box.addWidget(self.today_num)
        today_box.addWidget(seg_lbl)
        today_box.addSpacing(8)
        today_box.addWidget(self.today_chars)
        today_box.addWidget(chars_lbl)
        hl.addLayout(today_box)

        # 小结状态标签
        mini_box = QVBoxLayout()
        mini_box.setSpacing(2)
        mini_lbl = QLabel("阶 段 小 结")
        mini_lbl.setStyleSheet("color:#8e8a82; font-size:12px; letter-spacing:4px; font-weight:600;")
        self.mini_status_label = QLabel("–")
        self.mini_status_label.setStyleSheet("color:#cc785c; font-size:13px; font-weight:600;")
        mini_box.addWidget(mini_lbl)
        mini_box.addWidget(self.mini_status_label)
        hl.addLayout(mini_box)

        hl.addStretch(1)

        self.btn_history = QPushButton("历史")
        self.btn_history.setToolTip("查看历史日期的转写和总结")
        self.btn_history.clicked.connect(self._open_history)
        self.btn_speakers = QPushButton("声纹")
        self.btn_speakers.setToolTip("管理声纹库")
        self.btn_speakers.clicked.connect(self._open_speakers)
        self.btn_open_md = QPushButton("今日笔记")
        self.btn_open_md.setToolTip("用默认编辑器打开今日总结 Markdown")
        self.btn_open_md.clicked.connect(self._open_today_md)
        self.btn_mini = QPushButton("小结")
        self.btn_mini.setToolTip("对上次小结之后的新段做一次阶段小结（3小时自动触发，也可手动）")
        self.btn_mini.clicked.connect(self._do_mini_summary)
        self.btn_moments = QPushButton("朋友圈")
        self.btn_moments.setToolTip("从指定日期的语音日记生成朋友圈候选稿；先预览，不自动发布")
        self.btn_moments.clicked.connect(self._on_generate_moments_preview)
        self.btn_summary = QPushButton("总结今日")
        self.btn_summary.setObjectName("primaryBtn")
        self.btn_summary.setToolTip("把今天所有片段一次性总结，写入 Obsidian + 飞书")
        self.btn_summary.clicked.connect(self._do_summary_now)

        hl.addWidget(self.btn_history)
        hl.addWidget(self.btn_speakers)
        hl.addWidget(self.btn_open_md)
        hl.addWidget(self.btn_mini)
        if MOMENTS_SCRIPT.exists():
            hl.addWidget(self.btn_moments)
        hl.addWidget(self.btn_summary)

        return card

    def _moments_preview_path(self, day_text: str) -> Path:
        if MOMENTS_OUTPUT_LAYOUT == "dated-dir":
            return (
                MOMENTS_WORKFLOW_DIR
                / "运行输出"
                / day_text
                / "朋友圈素材筛选.md"
            )
        return MOMENTS_WORKFLOW_DIR / "运行输出" / f"{day_text}-朋友圈素材筛选.md"

    def _moments_diary_path(self, day_text: str) -> Path:
        obs = CONFIG.get("obsidian", {})
        vault = configured_obsidian_vault()
        folder = obs.get("folder", "语音日记")
        if vault:
            return vault / folder / f"{day_text}.md"
        return ROOT / "notes" / f"{day_text}.md"

    def _resolve_moment_image(self, day_text: str, candidate: dict) -> tuple[Path | None, Path]:
        """按候选 ID 解析最终品牌图；返回现有图片和应生成的目标路径。"""
        candidate_id = str(candidate.get("candidate_id") or "").strip()
        expected = (
            MOMENTS_WORKFLOW_DIR
            / "运行输出"
            / day_text
            / "配图"
            / f"{candidate_id}-final-branded.png"
        )
        task = candidate.get("image_task") or {}
        declared = [
            candidate.get("image_path"),
            task.get("final_branded_path"),
            task.get("downloads_copy_path"),
        ]
        paths: list[Path] = []
        for value in declared:
            if value:
                paths.append(Path(str(value)).expanduser())
        if candidate_id:
            paths.extend([expected, Path.home() / "Downloads" / expected.name])
        for path in paths:
            if path.is_file():
                return path, expected
        return None, expected

    def _moments_log(self, message: str):
        try:
            p = ROOT / "runtime" / "moments-ui.log"
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as f:
                f.write(f"[{dt.datetime.now().isoformat(timespec='seconds')}] {message}\n")
        except Exception:
            pass

    def _obsidian_diary_path(self, day: dt.date) -> Path | None:
        obs = CONFIG.get("obsidian", {})
        vault = configured_obsidian_vault()
        folder = obs.get("folder", "语音日记")
        if not vault:
            return note_path(day)
        return vault / folder / f"{day.isoformat()}.md"

    def _load_moments_status(self) -> dict:
        try:
            if MOMENTS_STATUS_PATH.exists():
                return json.loads(MOMENTS_STATUS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {"items": {}}

    def _save_moments_status(self, data: dict):
        try:
            MOMENTS_STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
            MOMENTS_STATUS_PATH.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as ex:
            self.bottom_status.setText(f"朋友圈状态保存失败：{ex}")

    def _moment_status_key(self, day_text: str, title: str) -> str:
        return f"{day_text}::{title}"

    def _get_moment_status(self, day_text: str, title: str) -> str:
        data = self._load_moments_status()
        item = data.get("items", {}).get(self._moment_status_key(day_text, title), {})
        return item.get("status", "待处理")

    def _set_moment_status(self, day_text: str, title: str, status: str):
        data = self._load_moments_status()
        items = data.setdefault("items", {})
        key = self._moment_status_key(day_text, title)
        item = items.setdefault(key, {})
        item["date"] = day_text
        item["title"] = title
        item["status"] = status
        item["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
        self._save_moments_status(data)

    def _split_publish_row(self, row: str) -> list[str]:
        return [cell.strip() for cell in row.strip().strip("|").split("|")]

    def _format_publish_row(self, cells: list[str]) -> str:
        padded = (cells + [""] * 11)[:11]
        return "| " + " | ".join(padded) + " |"

    def _publish_title_key(self, value: str) -> str:
        return re.sub(r"\s+", "", value or "").lower()

    def _sync_moment_publish_record(self, day_text: str, candidate: dict, status: str):
        """同步朋友圈候选状态到 Obsidian 发布记录表。"""
        if status not in {"待发", "已发", "不发"}:
            return
        title = (candidate.get("title") or "").strip()
        if not title:
            return

        row_type = (candidate.get("type") or "朋友圈").strip()
        publish_time = dt.datetime.now().strftime("%Y-%m-%d %H:%M") if status == "已发" else ""
        new_cells = [
            day_text,
            f"[[语音日记/{day_text}]]",
            row_type,
            title.replace("|", "/"),
            status,
            publish_time,
            "",
            "",
            "",
            "",
            "",
        ]

        try:
            path = MOMENTS_PUBLISH_RECORD_PATH
            if path.exists():
                lines = path.read_text(encoding="utf-8").splitlines()
            else:
                lines = [
                    "# 朋友圈发布记录",
                    "> 状态：候选 / 待发 / 已发 / 不发 / 已改",
                    "",
                    "| 日期 | 来源日记 | 类型 | 主题 | 状态 | 发布时间 | 点赞 | 评论 | 私聊人数 | 成交线索 | 复盘备注 |",
                    "|---|---|---|---|---|---|---:|---:|---:|---|---|",
                ]

            title_key = self._publish_title_key(title)
            matched_idx: int | None = None
            for idx, line in enumerate(lines):
                stripped = line.strip()
                if not stripped.startswith("|") or stripped.startswith("|---") or "日期" in stripped:
                    continue
                cells = self._split_publish_row(stripped)
                if len(cells) < 5:
                    continue
                same_day = cells[0].strip() == day_text
                same_title = self._publish_title_key(cells[3] if len(cells) > 3 else "") == title_key
                if same_day and same_title:
                    matched_idx = idx
                    break

            if matched_idx is None:
                lines.append(self._format_publish_row(new_cells))
            else:
                cells = (self._split_publish_row(lines[matched_idx]) + [""] * 11)[:11]
                cells[4] = status
                if status == "已发" and not cells[5].strip():
                    cells[5] = publish_time
                lines[matched_idx] = self._format_publish_row(cells)

            path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
            self._moments_log(f"publish record synced: {day_text} {status} {title}")
        except Exception as ex:
            self._moments_log(f"publish record sync failed: {ex}")
            self.bottom_status.setText(f"朋友圈发布记录同步失败：{ex}")

    def _choose_moments_day(self) -> str | None:
        """用日历选择语音日记日期。"""
        dlg = QDialog(self)
        dlg.setWindowTitle("选择朋友圈日期")
        dlg.resize(620, 460)

        root = QHBoxLayout(dlg)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(14)

        left = QVBoxLayout()
        title = QLabel("选择语音日记日期")
        title.setStyleSheet("color:#1c1b18; font-size:16px; font-weight:800;")
        left.addWidget(title)

        calendar = QCalendarWidget()
        calendar.setGridVisible(True)
        calendar.setVerticalHeaderFormat(QCalendarWidget.NoVerticalHeader)
        calendar.setSelectedDate(QDate.currentDate().addDays(-1))
        calendar.setStyleSheet("""
            QCalendarWidget QWidget { alternate-background-color: #faf9f5; }
            QCalendarWidget QAbstractItemView:enabled {
                color: #1c1b18;
                background: #ffffff;
                selection-background-color: #cc785c;
                selection-color: #ffffff;
            }
            QCalendarWidget QToolButton {
                color: #1c1b18;
                background: transparent;
                font-size: 13px;
                font-weight: 600;
                padding: 4px;
            }
            QCalendarWidget QToolButton:hover { background: rgba(204,120,92,0.12); border-radius: 4px; }
        """)

        diary_days = set(list_history_days())
        fmt = QTextCharFormat()
        fmt.setBackground(QBrush(QColor(204, 120, 92, 42)))
        fmt.setForeground(QBrush(QColor(28, 27, 24)))
        fmt.setFontWeight(QFont.Weight.Bold)
        for day in diary_days:
            calendar.setDateTextFormat(QDate(day.year, day.month, day.day), fmt)
        left.addWidget(calendar, 1)
        root.addLayout(left, 1)

        right = QVBoxLayout()
        right.setSpacing(10)
        info = QLabel("")
        info.setWordWrap(True)
        info.setStyleSheet(
            "color:#5a564e; font-size:13px; line-height:1.6; "
            "background:#ffffff; border:1px solid rgba(28,27,24,0.08); "
            "border-radius:8px; padding:12px;"
        )
        right.addWidget(info)

        quick = QHBoxLayout()
        btn_today = QPushButton("今天")
        btn_yest = QPushButton("昨天")
        btn_before = QPushButton("前天")
        quick.addWidget(btn_today)
        quick.addWidget(btn_yest)
        quick.addWidget(btn_before)
        right.addLayout(quick)

        right.addStretch(1)
        btn_row = QHBoxLayout()
        btn_cancel = QPushButton("取消")
        btn_ok = QPushButton("选择")
        btn_ok.setObjectName("primaryBtn")
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(btn_ok)
        right.addLayout(btn_row)
        root.addLayout(right)

        selected = {"day": ""}

        def selected_date() -> dt.date:
            qd = calendar.selectedDate()
            return dt.date(qd.year(), qd.month(), qd.day())

        def update_info():
            day = selected_date()
            day_text = day.isoformat()
            diary_p = self._obsidian_diary_path(day)
            has_diary = (diary_p.exists() if diary_p else False) or day in diary_days
            has_transcript = transcript_path(day).exists()
            has_preview = self._moments_preview_path(day_text).exists()
            lines = [
                f"日期：{day_text}",
                f"语音日记：{'有' if has_diary else '未找到'}",
                f"原始转写：{'有' if has_transcript else '未找到'}",
                f"朋友圈候选：{'已有预览' if has_preview else '还没生成'}",
            ]
            if not has_diary and not has_transcript:
                lines.append("")
                lines.append("这一天没有检测到可用的语音资料。")
            elif not has_diary and has_transcript:
                lines.append("")
                lines.append("日总结尚未生成，声年会直接从当天原始转写中筛选朋友圈素材。")
            elif has_preview:
                lines.append("")
                lines.append("选择后可以直接打开已有结果，也可以重新生成。")
            else:
                lines.append("")
                lines.append("选择后会生成新的朋友圈候选。")
            info.setText("\n".join(lines))

        def set_day(delta: int):
            d = dt.date.today() + dt.timedelta(days=delta)
            calendar.setSelectedDate(QDate(d.year, d.month, d.day))
            update_info()

        btn_today.clicked.connect(lambda: set_day(0))
        btn_yest.clicked.connect(lambda: set_day(-1))
        btn_before.clicked.connect(lambda: set_day(-2))
        calendar.selectionChanged.connect(update_info)
        btn_cancel.clicked.connect(dlg.reject)

        def accept_day():
            selected["day"] = selected_date().isoformat()
            dlg.accept()

        btn_ok.clicked.connect(accept_day)
        update_info()

        if dlg.exec() != QDialog.Accepted:
            return None
        return selected["day"]

    def _on_generate_moments_preview(self):
        """生成指定日期的朋友圈候选稿，并在应用内预览。"""
        self._moments_log("clicked")
        if getattr(self, "_moments_running", False):
            self.bottom_status.setText("朋友圈候选正在生成中，稍候...")
            self._moments_log("ignored: already running")
            return
        if not MOMENTS_SCRIPT.exists():
            QMessageBox.warning(self, "朋友圈", f"找不到脚本：\n{MOMENTS_SCRIPT}")
            self._moments_log(f"missing script: {MOMENTS_SCRIPT}")
            return

        day_text = self._choose_moments_day()
        if not day_text:
            self._moments_log("cancelled date picker")
            return
        self._moments_log(f"selected date: {day_text}")

        preview_path = self._moments_preview_path(day_text)
        if preview_path.exists():
            box = QMessageBox(self)
            box.setWindowTitle("已有朋友圈预览")
            box.setText(f"{day_text} 已经生成过朋友圈候选。")
            box.setInformativeText("可以直接打开已有结果；只有想刷新内容时再重新生成。")
            btn_open = box.addButton("打开已有结果", QMessageBox.AcceptRole)
            btn_regen = box.addButton("重新生成", QMessageBox.DestructiveRole)
            box.addButton("取消", QMessageBox.RejectRole)
            box.exec()
            clicked = box.clickedButton()
            if clicked == btn_open:
                self._moments_log(f"open existing: {preview_path}")
                self._show_moments_preview_dialog(day_text, preview_path)
                return
            if clicked != btn_regen:
                self._moments_log("cancelled existing preview dialog")
                return
            self._moments_log(f"regenerate requested: {day_text}")

        diary_file = self._moments_diary_path(day_text)
        if not diary_file.exists():
            try:
                day = dt.date.fromisoformat(day_text)
                transcript_file = transcript_path(day)
            except ValueError:
                transcript_file = None

            if transcript_file and transcript_file.exists():
                # 朋友圈生成本身已支持原始转写。不再把每日总结当作前置，
                # 避免总结服务暂时不可用时，用户等数分钟后连朋友圈也无法生成。
                self._moments_log(f"missing diary, generate directly from transcript: {transcript_file}")
                self._moments_running = True
                self._generate_moments_for_day(day_text)
                return

            msg = (
                f"{day_text} 还没有生成语音日记 Markdown。\n\n"
                f"朋友圈脚本需要先读取：\n{diary_file}\n\n"
                "这一天也没有找到本地转写 jsonl，可能当天还没有可用语音日记。"
            )
            self._moments_log(f"missing diary and transcript: {diary_file}")
            self.bottom_status.setText(f"朋友圈候选未生成：{day_text} 没有可用转写")
            QMessageBox.information(self, "朋友圈", msg)
            return

        self._moments_running = True
        self._generate_moments_for_day(day_text)

    def _generate_missing_diary_then_moments(self, day_text: str, diary_file: Path):
        """有转写但缺日记时，先补当天总结和知识库 Markdown，再继续朋友圈。"""
        self._moments_running = True
        self.btn_moments.setEnabled(False)
        self.btn_moments.setText("补日记中…")
        self.bottom_status.setText(f"正在补生成 {day_text} 语音日记 · 完成后自动继续朋友圈")

        def run():
            try:
                self._moments_log(f"daily summary prerequisite start: {day_text}")
                proc = subprocess.run(
                    _role_command("daily-summary", "--date", day_text, "--no-lark"),
                    cwd=str(ROOT),
                    capture_output=True,
                    timeout=900,
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                    creationflags=NO_WINDOW,
                )
                out = proc.stdout.decode("utf-8", "ignore")
                err = proc.stderr.decode("utf-8", "ignore")
                if proc.returncode != 0:
                    detail = (err or out or "当天总结生成失败").strip()
                    self._moments_log(
                        f"daily summary prerequisite failed code={proc.returncode}: {detail[:300]}"
                    )
                    try:
                        fallback_transcript = transcript_path(
                            dt.date.fromisoformat(day_text)
                        )
                    except ValueError:
                        fallback_transcript = None
                    if fallback_transcript and fallback_transcript.exists():
                        self._moments_log(
                            f"daily summary unavailable, continue from transcript: {fallback_transcript}"
                        )
                        self.moments_diary_ready.emit(day_text)
                        return
                    self.moments_failed.emit(f"补生成 {day_text} 语音日记失败：\n{detail}")
                    return
                if not diary_file.exists():
                    self._moments_log(f"daily summary finished but diary missing: {diary_file}")
                    self.moments_failed.emit(
                        f"当天总结已运行，但没有写出朋友圈需要的日记文件：\n{diary_file}"
                    )
                    return
                self._moments_log(f"daily summary prerequisite ok: {diary_file}")
                self.moments_diary_ready.emit(day_text)
            except Exception as exc:
                self._moments_log(f"daily summary prerequisite exception: {exc}")
                self.moments_failed.emit(f"补生成 {day_text} 语音日记失败：{exc}")

        threading.Thread(target=run, daemon=True).start()

    def _generate_moments_for_day(self, day_text: str):
        """前置日记已就绪，开始生成朋友圈候选。"""
        self.btn_moments.setEnabled(False)
        self.btn_moments.setText("生成中…")
        self.bottom_status.setText(f"朋友圈候选生成中 · {day_text}")

        def run():
            try:
                self._moments_log(f"subprocess start: {day_text}")
                proc = subprocess.Popen(
                    _role_command("moments", "--date", day_text, "--dry-run"),
                    cwd=str(ROOT),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    env={
                        **os.environ,
                        "PYTHONIOENCODING": "utf-8",
                        "PYTHONUNBUFFERED": "1",
                    },
                    creationflags=NO_WINDOW,
                )
                output_lines: list[str] = []
                if proc.stdout is not None:
                    for raw_line in iter(proc.stdout.readline, b""):
                        line = _decode_moments_output(raw_line).strip()
                        if not line:
                            continue
                        output_lines.append(line)
                        match = re.match(r"^\[progress\]\s+(\d+)/(\d+)\s*(.*)$", line)
                        if match:
                            done, total, detail = match.groups()
                            status = f"朋友圈候选生成中 · {day_text} · {done}/{total}"
                            if detail:
                                status += f" · {detail}"
                            self.moments_progress.emit(status)
                            self._moments_log(f"progress: {done}/{total} {detail}".rstrip())
                returncode = proc.wait()
                out = "\n".join(output_lines)
                if returncode != 0:
                    msg = (out or "生成失败").strip()
                    self._moments_log(f"subprocess failed code={returncode}: {msg[:300]}")
                    self.moments_failed.emit(msg)
                    return
                preview_path = self._moments_preview_path(day_text)
                self._moments_log(f"subprocess ok: {preview_path}")
                self.moments_done.emit(day_text, str(preview_path))
            except Exception as exc:
                self._moments_log(f"subprocess exception: {exc}")
                self.moments_failed.emit(str(exc))

        threading.Thread(target=run, daemon=True).start()

    def _on_moments_done(self, day_text: str, preview_path: Path):
        self._moments_running = False
        self.btn_moments.setEnabled(True)
        self.btn_moments.setText("朋友圈")

        if not preview_path.exists():
            self.bottom_status.setText(f"朋友圈候选生成完成，但找不到预览文件 · {day_text}")
            QMessageBox.warning(self, "朋友圈", f"找不到预览文件：\n{preview_path}")
            return

        self.bottom_status.setText(f"朋友圈候选已生成 · {day_text}")
        self._show_moments_preview_dialog(day_text, preview_path)

    def _on_moments_failed(self, err: str):
        self._moments_running = False
        self.btn_moments.setEnabled(True)
        self.btn_moments.setText("朋友圈")
        friendly = _friendly_moments_error(err)
        self._moments_log(f"friendly error shown: {friendly.splitlines()[0]}")
        self.bottom_status.setText(friendly.splitlines()[0])
        QMessageBox.warning(self, "朋友圈生成失败", friendly)

    def _parse_moments_candidates(self, markdown: str) -> list[dict]:
        """把朋友圈 Markdown 预览拆成候选卡片，便于 UI 展示和复制正文。"""
        import re

        candidates: list[dict] = []
        section = ""
        matches = list(re.finditer(r"^###\s+(.+?)\s*$", markdown, flags=re.M))
        for i, m in enumerate(matches):
            title_line = m.group(1).strip()
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
            block = markdown[start:end].strip()
            before = markdown[:m.start()]
            last_h2 = re.findall(r"^##\s+(.+?)\s*$", before, flags=re.M)
            if last_h2:
                section = last_h2[-1].strip()

            legacy_match = re.match(
                r"^(?:候选|待补)\s*\d*\s*[：:]\s*(.+?)\s*$",
                title_line,
            )
            canonical_match = re.match(r"^.+?\s*[：:]\s*(.+?)\s*$", title_line)
            canonical_section = (
                "85 分候选池" in section or "75—84 分待补区" in section
            )
            if not legacy_match and not (canonical_match and canonical_section):
                continue
            matched_title = legacy_match or canonical_match
            title = matched_title.group(1).strip() if matched_title else title_line
            candidate_id_match = re.match(
                r"^(\d{4}-\d{2}-\d{2}-C\d+)\s*[：:]",
                title_line,
            )
            candidate_id = candidate_id_match.group(1) if candidate_id_match else ""

            def field(name: str) -> str:
                fm = re.search(rf"^{re.escape(name)}[：:]\s*(.+?)\s*$", block, flags=re.M)
                return fm.group(1).strip() if fm else ""

            copy_text = ""
            cm = re.search(r"朋友圈草稿（复制版）：\s*```text\s*(.*?)\s*```", block, flags=re.S)
            if cm:
                copy_text = cm.group(1).strip()
            elif title_line.startswith("待补"):
                # 待补候选通常只有“补完后可写方向”的 text 代码块，也允许一键复制。
                code_blocks = re.findall(r"```text\s*(.*?)\s*```", block, flags=re.S)
                if code_blocks:
                    copy_text = code_blocks[-1].strip()

            reading_text = ""
            rm = re.search(
                r"朋友圈草稿（阅读版）：\s*(.*?)\s*朋友圈草稿（复制版）：",
                block,
                flags=re.S,
            )
            if rm:
                reading_text = rm.group(1).strip()
            elif copy_text:
                reading_text = copy_text

            score = field("质量评分")
            kind = field("类型")
            image_status = ""
            image_path = ""
            image_match = re.search(
                r"^配图任务[：:]\s*(.*?)\s*[｜|]\s*(.+?)\s*$",
                block,
                flags=re.M,
            )
            if image_match:
                image_status = image_match.group(1).strip()
                image_path = image_match.group(2).strip()
            candidates.append({
                "candidate_id": candidate_id,
                "title": title or title_line,
                "section": section,
                "type": kind,
                "score": score,
                "recommend": field("推荐"),
                "score_note": field("评分说明"),
                "copy_text": copy_text,
                "reading_text": reading_text,
                "detail": block,
                "image_task_status": image_status,
                "image_path": image_path,
            })
        return candidates

    def _show_moments_preview_dialog(self, day_text: str, preview_path: Path):
        markdown = preview_path.read_text(encoding="utf-8")
        candidates = self._parse_moments_candidates(markdown)
        pool_path = preview_path.with_name("朋友圈候选池.json")
        try:
            pool = json.loads(pool_path.read_text(encoding="utf-8"))
            by_id = {
                str(item.get("candidate_id") or ""): item
                for item in pool.get("candidates", [])
            }
            for candidate in candidates:
                source = by_id.get(candidate.get("candidate_id", ""), {})
                if source:
                    candidate["image_task"] = source.get("image_task") or {}
        except Exception as ex:
            self._moments_log(f"candidate image metadata unavailable: {ex}")

        dlg = QDialog(self)
        dlg.setWindowTitle(f"朋友圈候选 · {day_text}")
        dlg.resize(1120, 760)

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        hdr = QHBoxLayout()
        title = QLabel(f"朋友圈候选 · {day_text}")
        title.setStyleSheet("color:#1c1b18; font-size:16px; font-weight:800;")
        hdr.addWidget(title, 1)
        count_lbl = QLabel(f"{len(candidates)} 条候选")
        count_lbl.setStyleSheet("color:#8e8a82; font-size:13px;")
        hdr.addWidget(count_lbl)
        layout.addLayout(hdr)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setStyleSheet("""
            QSplitter::handle:horizontal {
                background: rgba(28,27,24,0.10);
                width: 8px;
                margin: 0 2px;
                border-radius: 4px;
            }
            QSplitter::handle:horizontal:hover {
                background: rgba(204,120,92,0.45);
            }
        """)

        left_panel = QFrame()
        left_panel.setObjectName("infoPanel")
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(12, 10, 12, 10)
        left_layout.setSpacing(8)

        left_title = QLabel("候 选 列 表")
        left_title.setStyleSheet("color:#1c1b18; font-size:13px; letter-spacing:3px; font-weight:800;")
        left_layout.addWidget(left_title)

        candidate_list = QListWidget()
        candidate_list.setStyleSheet(
            "QListWidget { background:#ffffff; border:1px solid rgba(28,27,24,0.08); "
            "border-radius:8px; padding:4px; }"
            "QListWidget::item { padding:10px 10px; border-radius:6px; }"
            "QListWidget::item:selected { background:rgba(204,120,92,0.24); color:#1c1b18; }"
        )
        left_layout.addWidget(candidate_list, 1)

        detail_panel = QFrame()
        detail_panel.setObjectName("infoPanel")
        detail_layout = QVBoxLayout(detail_panel)
        detail_layout.setContentsMargins(12, 10, 12, 10)
        detail_layout.setSpacing(8)

        meta_lbl = QLabel("请选择一条候选")
        meta_lbl.setWordWrap(True)
        meta_lbl.setStyleSheet("color:#5a564e; font-size:13px; font-weight:600;")
        detail_layout.addWidget(meta_lbl)

        detail_text = QPlainTextEdit()
        detail_text.setReadOnly(True)
        detail_text.setFrameShape(QPlainTextEdit.NoFrame)
        detail_text.setStyleSheet(
            "QPlainTextEdit { background:#ffffff; border:1px solid rgba(28,27,24,0.10); "
            "border-radius:8px; padding:12px; font-size:14px; line-height:1.7; }"
        )
        content_splitter = QSplitter(Qt.Horizontal)
        content_splitter.addWidget(detail_text)

        image_panel = QFrame()
        image_panel.setObjectName("infoPanel")
        image_layout = QVBoxLayout(image_panel)
        image_layout.setContentsMargins(8, 8, 8, 8)
        image_layout.setSpacing(8)
        image_title_lbl = QLabel("对 应 配 图")
        image_title_lbl.setStyleSheet(
            "color:#1c1b18; font-size:12px; letter-spacing:2px; font-weight:800;"
        )
        image_layout.addWidget(image_title_lbl)
        image_preview = QLabel("配图尚未生成")
        image_preview.setAlignment(Qt.AlignCenter)
        image_preview.setFixedSize(240, 320)
        image_preview.setStyleSheet(
            "QLabel { background:#f7f1e8; border:1px solid rgba(52,74,94,0.18); "
            "border-radius:10px; color:#7e9db2; padding:8px; }"
        )
        image_layout.addWidget(image_preview, 0, Qt.AlignHCenter)
        image_info = QLabel("选择候选后自动匹配图片")
        image_info.setWordWrap(True)
        image_info.setStyleSheet("color:#8e8a82; font-size:11px;")
        image_layout.addWidget(image_info)
        image_layout.addStretch(1)
        content_splitter.addWidget(image_panel)
        content_splitter.setSizes([620, 260])
        content_splitter.setCollapsible(1, False)
        detail_layout.addWidget(content_splitter, 1)

        copy_row = QHBoxLayout()
        copy_status = QLabel("")
        copy_status.setStyleSheet("color:#8ca36a; font-size:12px;")
        copy_row.addWidget(copy_status, 1)

        mark_wait_btn = QPushButton("标记待发")
        mark_sent_btn = QPushButton("标记已发")
        mark_skip_btn = QPushButton("不发")
        mark_skip_btn.setObjectName("dangerBtn")
        copy_body_btn = QPushButton("复制朋友圈正文")
        copy_body_btn.setObjectName("primaryBtn")
        copy_read_btn = QPushButton("复制阅读版")
        copy_read_btn.setToolTip("复制带完整段落的阅读版；没有阅读版时复制朋友圈正文")
        copy_row.addWidget(mark_wait_btn)
        copy_row.addWidget(mark_sent_btn)
        copy_row.addWidget(mark_skip_btn)
        copy_row.addWidget(copy_read_btn)
        copy_row.addWidget(copy_body_btn)
        detail_layout.addLayout(copy_row)

        image_row = QHBoxLayout()
        image_status_lbl = QLabel("文案：DeepSeek Flash｜图片：等待读取生成信息")
        image_status_lbl.setWordWrap(True)
        image_status_lbl.setStyleSheet("color:#7e9db2; font-size:11px;")
        image_row.addWidget(image_status_lbl, 1)
        open_image_btn = QPushButton("打开对应图片")
        open_image_btn.setToolTip("用系统图片查看器打开当前候选绑定的最终品牌图")
        image_row.addWidget(open_image_btn)
        detail_layout.addLayout(image_row)

        splitter.addWidget(left_panel)
        splitter.addWidget(detail_panel)
        splitter.setSizes([340, 760])
        layout.addWidget(splitter, 1)

        current = {"idx": 0}

        def item_label(c: dict) -> str:
            direct_section = (
                "可直接发" in c["section"] or "85 分候选池" in c["section"]
            )
            tag = "可直接发" if direct_section else "待补细节"
            status = self._get_moment_status(day_text, c["title"])
            meta = " · ".join(x for x in [c.get("type"), c.get("score")] if x)
            return f"[{status}] {tag}\n{c['title']}\n{meta}".strip()

        def refresh_current_item():
            row_idx = current["idx"]
            if 0 <= row_idx < candidate_list.count() and row_idx < len(candidates):
                candidate_list.item(row_idx).setText(item_label(candidates[row_idx]))

        def apply_status(status: str):
            if not candidates:
                return
            c = candidates[current["idx"]]
            self._set_moment_status(day_text, c["title"], status)
            self._sync_moment_publish_record(day_text, c, status)
            refresh_current_item()
            select_candidate(current["idx"])
            copy_status.setText(f"已标记：{status}")
            self.bottom_status.setText(f"朋友圈候选已标记 {status} · {c['title'][:24]}")

        def select_candidate(idx: int):
            if not candidates:
                meta_lbl.setText("没有解析到候选，可打开 Markdown 查看原文。")
                detail_text.setPlainText(markdown)
                copy_body_btn.setEnabled(False)
                copy_read_btn.setEnabled(False)
                open_image_btn.setEnabled(False)
                return
            current["idx"] = idx
            c = candidates[idx]
            status = self._get_moment_status(day_text, c["title"])
            meta_parts = [
                f"状态：{status}",
                c.get("section", ""),
                c.get("type", ""),
                c.get("score", ""),
                c.get("recommend", ""),
            ]
            meta_lbl.setText(" · ".join(x for x in meta_parts if x))
            detail_text.setPlainText(f"# {c['title']}\n\n{c['detail']}".strip())
            copy_body_btn.setEnabled(bool(c.get("copy_text")))
            copy_read_btn.setEnabled(bool(c.get("reading_text") or c.get("copy_text")))
            image_task = c.get("image_task") or {}
            owner = str(image_task.get("generation_owner") or "声年配图流程")
            pipeline = str(image_task.get("generation_pipeline") or "按候选 ID 匹配最终图片")
            template_name = str(image_task.get("template_name") or image_task.get("template_id") or "").strip()
            template_text = f"｜模板：{template_name}" if template_name else ""
            image_status_lbl.setText(
                f"文案：DeepSeek Flash｜图片：{owner}{template_text}\n{pipeline}"
            )
            image_path, expected_path = self._resolve_moment_image(day_text, c)
            if image_path:
                pixmap = QPixmap(str(image_path))
                if pixmap.isNull():
                    image_preview.setPixmap(QPixmap())
                    image_preview.setText("图片文件无法预览")
                else:
                    image_preview.setText("")
                    image_preview.setPixmap(
                        pixmap.scaled(
                            image_preview.size(),
                            Qt.KeepAspectRatio,
                            Qt.SmoothTransformation,
                        )
                    )
                image_info.setText(f"已匹配：{image_path.name}")
                open_image_btn.setText("打开对应图片")
                open_image_btn.setEnabled(True)
            else:
                image_preview.setPixmap(QPixmap())
                image_preview.setText("配图尚未生成")
                image_info.setText(f"生成后将自动匹配：\n{expected_path}")
                open_image_btn.setText("配图尚未生成")
                open_image_btn.setEnabled(False)
            copy_status.setText("")

        def copy_text(which: str):
            if not candidates:
                return
            c = candidates[current["idx"]]
            text = c.get("copy_text") if which == "body" else (c.get("reading_text") or c.get("copy_text"))
            if not text:
                copy_status.setText("这条没有可复制正文")
                return
            visible_label = "【AI 辅助生成，使用或发布前请核实】"
            if not text.lstrip().startswith(visible_label):
                text = f"{visible_label}\n\n{text}"
            QApplication.clipboard().setText(text)
            if self._get_moment_status(day_text, c["title"]) == "待处理":
                self._set_moment_status(day_text, c["title"], "已复制")
                refresh_current_item()
                select_candidate(current["idx"])
            label = "朋友圈正文" if which == "body" else "阅读版"
            copy_status.setText(f"已复制{label}")
            self.bottom_status.setText(f"朋友圈候选已复制 · {c['title'][:24]}")

        def open_current_image():
            if not candidates:
                return
            c = candidates[current["idx"]]
            image_path, expected_path = self._resolve_moment_image(day_text, c)
            if not image_path:
                QMessageBox.information(
                    dlg,
                    "配图尚未生成",
                    f"当前候选还没有最终品牌图。\n\n生成后应保存到：\n{expected_path}",
                )
                return
            open_path(str(image_path))
            self.bottom_status.setText(f"已打开对应配图 · {c['title'][:24]}")

        copy_body_btn.clicked.connect(lambda: copy_text("body"))
        copy_read_btn.clicked.connect(lambda: copy_text("read"))
        open_image_btn.clicked.connect(open_current_image)
        mark_wait_btn.clicked.connect(lambda: apply_status("待发"))
        mark_sent_btn.clicked.connect(lambda: apply_status("已发"))
        mark_skip_btn.clicked.connect(lambda: apply_status("不发"))

        if candidates:
            for idx, c in enumerate(candidates):
                item = QListWidgetItem(item_label(c))
                item.setData(Qt.UserRole, idx)
                candidate_list.addItem(item)
            candidate_list.currentItemChanged.connect(
                lambda item, _prev: select_candidate(item.data(Qt.UserRole)) if item else None
            )
            candidate_list.setCurrentRow(0)
        else:
            select_candidate(0)

        row = QHBoxLayout()
        note = QLabel("复制按钮会优先复制 `朋友圈草稿（复制版）` 里的纯文本，保留段落空行。")
        note.setStyleSheet("color:#8e8a82; font-size:12px;")
        row.addWidget(note, 1)
        row.addStretch(1)

        open_btn = QPushButton("打开 Markdown")
        open_btn.setToolTip("用默认编辑器打开这次生成的预览文件")
        open_btn.clicked.connect(lambda: open_path(str(preview_path)))
        row.addWidget(open_btn)

        close_btn = QPushButton("关闭")
        close_btn.setObjectName("primaryBtn")
        close_btn.clicked.connect(dlg.accept)
        row.addWidget(close_btn)

        layout.addLayout(row)
        dlg.exec()

    # ---------- 动作 ----------
    def _adopt_existing(self):
        """launcher 启动后 0.5s 自动接管已在运行的 recorder/transcriber（开机自启场景）。
        如果接管到了 recorder,说明之前有人开过录音,启用守护重启。"""
        self.recorder.adopt_running()
        self.transcriber.adopt_running()
        # 如果接管到 recorder,说明用户之前开过录音 → 启用守护
        if self.recorder.is_running():
            self._recorder_was_started = True

    def _start_all(self):
        if sys.platform == "darwin" and getattr(sys, "frozen", False):
            from PySide6.QtCore import QMicrophonePermission
            app = QApplication.instance()
            permission = QMicrophonePermission()
            status = app.checkPermission(permission)
            if status == Qt.PermissionStatus.Undetermined:
                if not getattr(self, "_microphone_request_pending", False):
                    self._microphone_request_pending = True
                    def permission_result(result):
                        self._microphone_request_pending = False
                        if result.status() == Qt.PermissionStatus.Granted:
                            self._start_all()
                        else:
                            self.bottom_status.setText(microphone_permission_hint())
                    app.requestPermission(permission, self, permission_result)
                return
            if status != Qt.PermissionStatus.Granted:
                QMessageBox.information(self, "麦克风权限", microphone_permission_hint())
                return
        # 标记:用户主动开了录音 — 后续守护检查到 recorder 死了会自动重启
        self._recorder_was_started = True
        # 用户主动点「启动录音」= 想录 → 顺手清掉可能残留的自动暂停标记
        self._clear_ingest_pause()

        # 先接管已在系统中跑着的进程（launcher 重启后复用，不杀不重启）
        self.recorder.adopt_running()
        self.transcriber.adopt_running()

        rec_running = self.recorder.is_running()
        tr_running = self.transcriber.is_running()

        if rec_running and tr_running:
            self.bottom_status.setText("✅ 录音 + 转写均已在运行")
            return

        # 有没在跑的，启动缺失的
        errors = []
        if not tr_running and not self.transcriber.start():
            errors.append(self.transcriber.last_error)
        if not rec_running and not self.recorder.start():
            errors.append(self.recorder.last_error)

        if errors:
            detail = "\n".join(error for error in errors if error)
            self.bottom_status.setText("⚠ 启动失败 · 请查看提示")
            QMessageBox.warning(
                self,
                "录音与转写启动失败",
                f"后台服务没有完整启动。\n\n{detail}",
            )
            return

        if rec_running and not tr_running:
            self.bottom_status.setText("转写器已启动 · 录音器已在运行")
        elif tr_running and not rec_running:
            self.bottom_status.setText("录音器已启动 · 转写器已在运行")
        else:
            self.bottom_status.setText("✅ 已启动 · 模型首次加载约 20-30 秒...")

    def _stop_all(self):
        # 用户主动停 — 清掉守护标记,守护不再自动拉起
        self._recorder_was_started = False
        self.recorder.stop()
        self.transcriber.stop()
        self.bottom_status.setText("已停止 · WAV 和转写已保留")

    def _toggle_pause(self):
        flag = pause_flag()
        if flag.exists():
            flag.unlink()
            self.bottom_status.setText("已恢复录音")
        else:
            flag.write_text("paused", encoding="utf-8")
            self.bottom_status.setText("已暂停 · 麦克风输入被丢弃 · 再点恢复")

    def _do_summary_now(self):
        if not self._ensure_cloud_ai_enabled(interactive=True):
            return
        provider = CONFIG["summary"].get("provider", "deepseek")
        key_env = {"deepseek": "DEEPSEEK_API_KEY",
                   "anthropic": "ANTHROPIC_API_KEY",
                   "openai": "OPENAI_API_KEY"}.get(provider, "DEEPSEEK_API_KEY")
        # 优先读系统注册表（兼容 setx 设置后没重启启动器的情况）
        api_key = self._provider_api_key(key_env)
        if not api_key:
            self.bottom_status.setText(f"❌ 缺少 {key_env}，请重启启动器后重试")
            QMessageBox.warning(
                self, "缺少 API Key",
                f"当前 provider={provider}\n\n"
                f"未找到环境变量 {key_env}。\n\n"
                f"请按 README 配置环境变量 {key_env} 后重启声年。",
            )
            return
        # 把 key 注入当前进程环境，让子进程能继承
        os.environ[key_env] = api_key
        self.bottom_status.setText(f"正在让 {provider} 总结今天...")
        self.btn_summary.setEnabled(False)

        def run():
            try:
                proc = subprocess.run(
                    _role_command("daily-summary", "--no-lark"),
                    cwd=str(ROOT), capture_output=True, timeout=180,
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                    creationflags=NO_WINDOW,
                )
                ok = proc.returncode == 0
                msg = (
                    "总结完成 · 点“打开今日笔记”查看"
                    if ok else
                    "总结失败：云端 AI 暂时不可用，请检查账号、网络或稍后重试"
                )
                self.summary_finished.emit(ok, msg)
            except subprocess.TimeoutExpired:
                self.summary_finished.emit(False, "总结超时(180s)，请检查网络后重试")
            except Exception as e:
                self.summary_finished.emit(False, f"总结异常:{e}")

        threading.Thread(target=run, daemon=True).start()

    def _on_summary_finished(self, ok: bool, message: str):
        """在 Qt 主线程恢复今日总结界面。"""
        self.bottom_status.setText(message)
        self.btn_summary.setEnabled(True)
        if ok:
            self._open_today_md()
            self._reload_brief()
            self._reload_todo()

    def _check_mini_schedule(self):
        """按钟点触发阶段小结:到 9/12/15/18/21 点就跑,每个钟点每天只跑一次。"""
        now = dt.datetime.now()
        if now.hour not in self._mini_hours:
            return
        today = now.date().isoformat()
        key = f"{today}-{now.hour}"
        if key in self._mini_fired:
            return
        self._mini_fired.add(key)
        # 跨天重置:只保留今天的记录
        self._mini_fired = {k for k in self._mini_fired if k.startswith(today)}
        self._do_mini_summary(interactive=False)

    def _check_mini_startup(self):
        """启动时对齐:把今天已过的钟点都标记为已触发(避免 timer 重复补跑),
        并补跑一次,让简报立刻更新到当天,不用干等下一个钟点。"""
        now = dt.datetime.now()
        today = now.date().isoformat()
        passed = [h for h in self._mini_hours if h <= now.hour]
        if not passed:
            return  # 今天还没到第一个钟点,交给 timer
        for h in passed:
            self._mini_fired.add(f"{today}-{h}")
        if not self._mini_summary_running:
            self._do_mini_summary(interactive=False)

    def _do_mini_summary(self, _checked: bool = False, *, interactive: bool = True):
        """手动触发或定时触发：对今天新段做一次阶段小结。"""
        if self._mini_summary_running:
            self.bottom_status.setText("小结正在进行中，请稍候...")
            return
        if not self._ensure_cloud_ai_enabled(interactive=interactive):
            return
        provider = CONFIG["summary"].get("provider", "deepseek")
        key_env = {"deepseek": "DEEPSEEK_API_KEY",
                   "anthropic": "ANTHROPIC_API_KEY",
                   "openai": "OPENAI_API_KEY"}.get(provider, "DEEPSEEK_API_KEY")
        api_key = self._provider_api_key(key_env)
        if not api_key:
            self.bottom_status.setText(f"❌ 缺少 {key_env}，请重启启动器后重试")
            return
        os.environ[key_env] = api_key

        self._mini_summary_running = True
        self.btn_mini.setEnabled(False)
        self.bottom_status.setText("正在生成阶段小结...")

        def run():
            try:
                proc = subprocess.run(
                    _role_command("daily-summary", "--mini"),
                    cwd=str(ROOT), capture_output=True, timeout=120,
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                    creationflags=NO_WINDOW,
                )
                ok = proc.returncode == 0
                now_str = dt.datetime.now().strftime("%H:%M")
                if ok:
                    msg = f"阶段小结完成 · {now_str}"
                else:
                    msg = "小结失败：云端 AI 暂时不可用，请检查账号、网络或稍后重试"
                self.mini_summary_finished.emit(ok, msg, now_str)
            except subprocess.TimeoutExpired:
                self.mini_summary_finished.emit(
                    False, "小结超时(120s)，请检查网络后重试", ""
                )
            except Exception as e:
                self.mini_summary_finished.emit(False, f"小结异常:{e}", "")

        threading.Thread(target=run, daemon=True).start()

    def _on_mini_summary_finished(self, ok: bool, message: str, time_str: str):
        """在 Qt 主线程恢复阶段小结界面，避免后台线程的计时器回调丢失。"""
        self._mini_summary_running = False
        self.btn_mini.setEnabled(True)
        self.bottom_status.setText(message)
        if ok and time_str:
            self._on_mini_done(time_str)
        if getattr(self, "_card_todo_generation_pending", False):
            self._card_todo_generation_pending = False
            board = getattr(self, "card_board", None)
            if board is not None:
                if ok:
                    self._reload_todo()
                    board.finish_handled_generation(
                        "todos",
                        ok=True,
                        message="待办已从新的语音记录中整理完成。",
                    )
                else:
                    board.finish_handled_generation(
                        "todos",
                        ok=False,
                        message=message or "待办整理失败，请稍后重试。",
                    )

    def _on_mini_done(self, time_str: str):
        """小结完成后更新 UI 状态标签 + 刷新简报和待办（wiki 已被更新）。"""
        self._last_mini_at = time.time()
        self.mini_status_label.setText(f"最近: {time_str}")
        # 小结链路也会更新 wiki（待办/简报），必须刷新 launcher
        self._reload_brief()
        self._reload_todo()

    def _open_today_md(self):
        p = note_path()
        if not p.exists():
            QMessageBox.information(self, "提示", f"今天还没有总结。\n路径:{p}")
            return
        open_path(str(p))

    def _open_history(self):
        HistoryWindow(self).show()

    def _open_speakers(self):
        SpeakersWindow(self).show()

    # ---------- 周期 ----------
    def _tick(self):
        self.clock_label.setText(dt.datetime.now().strftime("%Y-%m-%d  %H:%M:%S"))
        rec_running = self.recorder.is_running()
        tr_running = self.transcriber.is_running()
        self.tr_value.setText("运行中" if tr_running else "停止")
        self.pause_value.setText("已暂停" if pause_flag().exists() else "正常")

        self._update_signal(rec_running)

        # ── 跨天检测：launcher 一直开着也能在 0 点后自动补做昨日复盘 ──
        today = dt.date.today()
        last_day = getattr(self, "_last_known_date", None)
        if last_day is None:
            self._last_known_date = today
        elif last_day != today:
            # 跨天了！触发昨日复盘补做（异步，不阻塞 UI）
            self._last_known_date = today
            self.bottom_status.setText(f"已跨入新一天 · 检查 {last_day} 复盘...")
            QTimer.singleShot(3000, self._auto_backfill_yest_review)

        # 每 2 秒刷今日统计
        if int(time.time()) % 2 == 0:
            self._refresh_today()
        # 每 10 秒刷小结状态
        if int(time.time()) % 10 == 0:
            self._refresh_mini_status()
        # 每 30 秒刷简报和 TODO（总结完会更新文件）
        if int(time.time()) % 30 == 0:
            self._reload_brief()
            self._reload_todo()
        # 每 60 秒刷昨日复盘（晚上 23:30 之后会有新 review）
        if int(time.time()) % 60 == 0:
            self._reload_yest()

        # ── 守护:transcriber 挂了自动重启(每 30 秒检查) ──
        # 避免之前那种"transcriber 5:42 静默死亡 ~ 7:38 才重启"导致 1.5 小时 wav 没转写的事
        if int(time.time()) % 30 == 0:
            self._guard_transcriber()
            self._guard_recorder()

    def _guard_transcriber(self):
        """每 30 秒检查 transcriber 是否还活着。
        如果 recorder 在跑(说明在录音),但 transcriber 不在,立刻拉起。"""
        try:
            if not self.recorder.is_running():
                # recorder 没开,user 主动停的,不要硬拉起 transcriber
                return
            if self.transcriber.is_running():
                return
            # transcriber 死了 / 没起 → 重启
            # 先 adopt 一下(可能有孤儿进程在跑,先认领)
            self.transcriber.adopt_running()
            if self.transcriber.is_running():
                return
            # 真的没在跑,拉起
            if not self.transcriber.start():
                self.bottom_status.setText(f"⚠ {self.transcriber.last_error}")
                return
            try:
                _log = ROOT / "runtime" / "guard.log"
                _log.parent.mkdir(parents=True, exist_ok=True)
                with open(_log, "a", encoding="utf-8") as f:
                    f.write(f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
                            f"⚠ transcriber 死了,自动重启\n")
            except Exception:
                pass
            self.bottom_status.setText("⚠ transcriber 之前挂了,自动重启中...")
        except Exception:
            pass

    def _guard_recorder(self):
        """每 30 秒检查 recorder。规则跟 transcriber 一样但更保守:
        只在 recorder 之前在跑(adopt 过)且现在挂了的情况下重启。"""
        try:
            # 用一个 attribute 标记"用户启动过 recorder"
            # 这样用户主动停了不会被自动拉起
            if not getattr(self, "_recorder_was_started", False):
                return
            if self.recorder.is_running():
                return
            # adopt 看看有没有孤儿进程
            self.recorder.adopt_running()
            if self.recorder.is_running():
                return
            if not self.recorder.start():
                self.bottom_status.setText(f"⚠ {self.recorder.last_error}")
                return
            try:
                _log = ROOT / "runtime" / "guard.log"
                with open(_log, "a", encoding="utf-8") as f:
                    f.write(f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
                            f"⚠ recorder 死了,自动重启\n")
            except Exception:
                pass
            self.bottom_status.setText("⚠ recorder 之前挂了,自动重启中...")
        except Exception:
            pass

    def _refresh_mini_status(self):
        """从 notes/mini/ 目录读今天已有小结，更新 UI 标签。"""
        try:
            from daily_summary import list_mini_summaries
            today = dt.date.today()
            minis = list_mini_summaries(today)
            if minis:
                last = minis[-1]
                t = last.get("created_at", "")
                if t:
                    t = t[11:16]  # HH:MM
                n = len(minis)
                self.mini_status_label.setText(f"{n} 次 · 最近 {t}")
            else:
                self.mini_status_label.setText("今日暂无小结")
        except Exception:
            pass

    @staticmethod
    def _read_reg_env(name: str) -> str:
        r"""从注册表 HKCU\Environment 读用户级环境变量（setx 写入但当前进程未继承时用）。"""
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                val, _ = winreg.QueryValueEx(key, name)
                return str(val)
        except Exception:
            return ""

    def _provider_api_key(self, name: str) -> str:
        """Commercial builds use the account gateway; developer builds use env keys."""
        from ai_gateway import provider_api_key

        return provider_api_key(name) or self._read_reg_env(name)

    def _update_signal(self, rec_running: bool):
        self.signal_label.setToolTip("")
        if not rec_running:
            self.signal_dot.set_state("off")
            self.signal_label.setText("未启动")
            self.rec_value.setText("停止")
            self.dev_value.setText("—")
            self._dismiss_alert()
            self._alert_armed_at = None
            return
        status = read_recorder_status()
        if status is None:
            self.signal_dot.set_state("warn")
            self.signal_label.setText("录音器启动中...")
            self.rec_value.setText("启动中")
            self.dev_value.setText("—")
            return

        state = str(status.get("state") or "recording")
        age = time.time() - status.get("ts", 0)
        if state in {"starting", "opening_device"} and age <= 8:
            self.signal_dot.set_state("warn")
            self.signal_label.setText(
                "正在连接麦克风..." if state == "opening_device" else "正在检查麦克风..."
            )
            self.rec_value.setText("启动中")
            self.dev_value.setText(status.get("device_name") or "—")
            return
        if state == "waiting_device" and age <= 8:
            self.signal_dot.set_state("bad")
            self.signal_label.setText("未找到可用麦克风 · 检查系统权限")
            self.rec_value.setText("等待设备")
            self.dev_value.setText("—")
            return
        if state == "error" and age <= 8:
            raw_reason = str(status.get("error") or "录音设备打开失败")
            reason = str(status.get("user_error") or raw_reason)
            reason = reason[:44] + ("…" if len(reason) > 44 else "")
            self.signal_dot.set_state("bad")
            self.signal_label.setText(f"录音失败 · {reason}")
            self.signal_label.setToolTip(
                f"{raw_reason}\n\n"
                "程序正在自动尝试其他采样率和音频接口。"
                "如仍失败，请点击“麦克风”切换设备。" + microphone_permission_hint()
            )
            self.rec_value.setText("自动重试中")
            self.dev_value.setText(status.get("device_name") or "—")
            return

        # 更新当前设备名（截短显示，最多 18 个字符）
        raw_dev = status.get("device_name", "")
        if raw_dev:
            # 从括号里提取品牌名，如 "麦克风 (Redmi 音响)" → "Redmi 音响"
            import re as _re
            m = _re.search(r'\((.+?)\)', raw_dev)
            short = m.group(1) if m else raw_dev
            short = short[:18] + ("…" if len(short) > 18 else "")
            # 主设备用金色，备用设备用橙色（直接读心跳里的 is_primary 字段）
            is_primary = status.get("is_primary", True)
            color = "#1c1b18" if is_primary else "#d9a865"
            self.dev_value.setText(short)
            self.dev_value.setStyleSheet(f"color:{color}; font-size:13px; font-weight:700;")
        else:
            self.dev_value.setText("—")

        if age > 5:
            self.signal_dot.set_state("bad")
            self.signal_label.setText(f"心跳超时 {age:.0f}s")
            self.rec_value.setText("无响应")
            return
        if not status.get("has_device", True):
            self.signal_dot.set_state("bad")
            self.signal_label.setText("未找到录音设备")
            self.rec_value.setText("等待设备")
            return
        if status.get("paused"):
            self.signal_dot.set_state("pause")
            self.signal_label.setText("已暂停")
            self.rec_value.setText(f"暂停 · {status.get('total_segments', 0)} 段")
            return
        if not status.get("input_active", True):
            self.signal_dot.set_state("bad")
            self.signal_label.setText("设备已打开，但没有收到音频数据")
            self.rec_value.setText("无音频输入")
            return
        signal_idle = float(status.get("signal_idle_sec", 0) or 0)
        if signal_idle >= 15:
            self.signal_dot.set_state("warn")
            self.signal_label.setText(f"麦克风无音量 {int(signal_idle)}s · 请检查静音")
            self.rec_value.setText("等待声音")
            return
        idle = status.get("idle_sec", 0)
        seg = status.get("total_segments", 0)
        if not status.get("voice_detected", True):
            self.signal_dot.set_state("good")
            self.signal_label.setText(f"麦克风已连接 · 音量 {status.get('level_rms', 0)}")
            self.rec_value.setText("等待说话")
            self._alert_armed_at = None
        elif idle < 30:
            self.signal_dot.set_state("good")
            self.signal_label.setText(f"正常收音 · {seg} 段")
            self.rec_value.setText(f"收音中 · {seg} 段")
            self._alert_armed_at = None
        elif idle < 600:
            self.signal_dot.set_state("warn")
            self.signal_label.setText(f"静音 {int(idle)}s · {seg} 段")
            self.rec_value.setText(f"静音 · {seg} 段")
        else:
            self.signal_dot.set_state("bad")
            self.signal_label.setText(f"静音 {int(idle / 60)} 分钟")
            self.rec_value.setText(f"长时静音 · {seg} 段")
            self._maybe_alert(idle)

    def _maybe_alert(self, idle_sec: float):
        if self._alert_armed_at is not None:
            return
        self._alert_armed_at = time.time()
        self.alert_label.setText(
            f"⚠ 已 {int(idle_sec / 60)} 分钟无声音 · 可能离 DJI 太远 / 没电 / 接收器掉线"
        )
        self.alert_frame.show()
        QApplication.beep()

    def _dismiss_alert(self):
        if self.alert_frame.isVisible():
            self.alert_frame.hide()

    def _refresh_today(self):
        records = list(read_jsonl(transcript_path()))
        valid = [r for r in records if r.get("text")]
        chars = sum(len(r["text"]) for r in valid)
        self.today_num.setText(str(len(valid)))
        self.today_chars.setText(str(chars))
        recent = valid[-50:]
        lines = []
        for r in recent:
            ts = (r.get("start") or "")[11:19]
            spk = r.get("speaker_name") or ""
            if spk and spk not in ("?", "未知"):
                lines.append(f"[{ts}]  {spk}  ·  {r['text']}")
            else:
                lines.append(f"[{ts}]  {r['text']}")
        text = "\n".join(lines)
        self.preview.setPlainText(text)
        sb = self.preview.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _backfill_wiki_async(self):
        """launcher 启动 5s 后调一次:扫最近 7 天,补做缺失的:
        ① 缺日报 → 跑 daily_summary 全流程(含 wiki + review)
        ② 有日报但缺 wiki entity/concept → 单独跑 wiki_appender
        ③ 有日报但缺 review → 跑 review

        在后台线程跑(LLM 调用慢,可能 1-2 分钟),不阻塞 UI。
        """
        import threading
        from common import note_path

        def _set_status(msg):
            self.background_status.emit(msg)

        def _check_api_key() -> tuple[bool, str]:
            provider = CONFIG["summary"].get("provider", "deepseek")
            key_env = {"deepseek": "DEEPSEEK_API_KEY",
                       "anthropic": "ANTHROPIC_API_KEY",
                       "openai": "OPENAI_API_KEY"}.get(provider, "DEEPSEEK_API_KEY")
            api_key = self._provider_api_key(key_env)
            if api_key:
                os.environ[key_env] = api_key
                return True, key_env
            return False, key_env

        def _worker():
            try:
                ok, key_env = _check_api_key()
                if not ok:
                    _set_status(f"⚠ 缺 {key_env},跳过后台补做")
                    return

                today = dt.date.today()
                LOOKBACK = 7

                # ① 找缺日报或仍处于 pending 的日子。pending 即使已有
                # 本地恢复笔记也必须重试，成功后 daily_summary 会自动清理标记。
                missing_daily = []
                for pending in sorted((ROOT / "notes").glob("*.pending.json")):
                    try:
                        pending_day = dt.date.fromisoformat(pending.name.split(".")[0])
                    except ValueError:
                        continue
                    if pending_day not in missing_daily:
                        missing_daily.append(pending_day)
                for i in range(1, LOOKBACK + 1):   # 从昨天往前
                    d = today - dt.timedelta(days=i)
                    tp = ROOT / "transcripts" / f"{d.isoformat()}.jsonl"
                    if not tp.exists():
                        continue
                    np = note_path(d)
                    if np and not np.exists() and d not in missing_daily:
                        missing_daily.append(d)

                if missing_daily:
                    _set_status(f"📝 后台补做 {len(missing_daily)} 天的日报...")
                    for d in missing_daily:
                        try:
                            _set_status(f"📝 补做 {d} 日报(含 wiki+review)...")
                            proc = subprocess.run(
                                _role_command("daily-summary", "--date", d.isoformat(), "--no-lark"),
                                cwd=str(ROOT), capture_output=True, timeout=300,
                                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                                creationflags=NO_WINDOW,
                            )
                            if proc.returncode != 0:
                                err = proc.stderr.decode("utf-8", "ignore")[-200:]
                                _set_status(f"⚠ {d} 日报补做失败:{err}")
                        except Exception as e:
                            _set_status(f"⚠ {d} 日报补做异常:{e}")

                # ② wiki appender 补做(daily summary 跑完会顺带跑 wiki,
                # 但若历史上某天日报跑过但 wiki 没跟上,这里单独补)
                import wiki_appender
                wiki_missing = wiki_appender.list_missing_days(LOOKBACK)
                if wiki_missing:
                    _set_status(f"📚 补做 {len(wiki_missing)} 天的 wiki 实体...")
                    def on_p(msg):
                        _set_status(f"📚 {msg}")
                    r = wiki_appender.backfill_missing_days(
                        lookback_days=LOOKBACK, on_progress=on_p
                    )
                    _set_status(
                        f"📚 wiki 补完 · {len(r['ran'])} 天 OK · 错误 {r['errors']}"
                    )

                # ③ review 补做(已有 _auto_backfill_yest_review 处理昨天,
                # 这里顺手把更早的也扫了)
                missing_review = []
                for i in range(1, LOOKBACK + 1):
                    d = today - dt.timedelta(days=i)
                    np = note_path(d)
                    if np and np.exists():
                        rp = ROOT / "notes" / f"{d.isoformat()}-review.md"
                        if not rp.exists():
                            missing_review.append(d)

                for d in missing_review:
                    try:
                        _set_status(f"🔄 补做 {d} 复盘...")
                        subprocess.run(
                            _role_command("daily-summary", "--review", "--date", d.isoformat()),
                            cwd=str(ROOT), capture_output=True, timeout=180,
                            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                            creationflags=NO_WINDOW,
                        )
                    except Exception as e:
                        _set_status(f"⚠ {d} 复盘补做异常:{e}")

                if missing_daily or wiki_missing or missing_review:
                    _set_status(
                        f"✓ 后台补做完成 · 日报 {len(missing_daily)} · "
                        f"wiki {len(wiki_missing)} · 复盘 {len(missing_review)} · "
                        f"已自动刷新"
                    )
                    # 刷新昨日面板 + 待讨论(可能有新问题入池)
                    self.background_refresh.emit("yest")
            except Exception as e:
                _set_status(f"后台补做异常:{e}")

        threading.Thread(target=_worker, daemon=True).start()

    def _on_background_refresh(self, target: str):
        """把后台任务请求的刷新统一放回 Qt 主线程执行。"""
        if target == "yest":
            self._reload_yest()

    def _on_background_status(self, message: str):
        """在 Qt 主线程显示后台任务状态。"""
        self.bottom_status.setText(message)

    # ===========================================
    # 会议候选检测 + 提醒条
    # ===========================================
    def _scan_meeting_candidates_async(self):
        """启动后异步扫最近 7 天的会议候选。
        关键:用 QThread + Signal 跨线程,不能在工作线程里调 QTimer.singleShot。"""
        _diag = ROOT / "runtime" / "meeting-hint.log"
        try:
            _diag.parent.mkdir(parents=True, exist_ok=True)
            with open(_diag, "a", encoding="utf-8") as f:
                f.write(f"[{dt.datetime.now().isoformat(timespec='seconds')}] scan 被调用\n")
        except Exception:
            pass

        # 用 QThread + Signal — 这是 Qt 跨线程的正确做法
        from PySide6.QtCore import QThread
        self._meeting_thread = QThread(self)
        self._meeting_worker = _MeetingScanWorker()
        self._meeting_worker.moveToThread(self._meeting_thread)
        self._meeting_thread.started.connect(self._meeting_worker.run)
        self._meeting_worker.found.connect(self._show_meeting_candidates)
        self._meeting_worker.finished.connect(self._meeting_thread.quit)
        self._meeting_thread.finished.connect(self._meeting_worker.deleteLater)
        self._meeting_thread.start()

    def _show_meeting_candidates(self, candidates: list[dict]):
        """显示会议候选提醒条。"""
        self._meeting_candidates = candidates
        self._meeting_cur_idx = 0
        self._render_current_meeting_hint()
        self.meeting_hint_frame.show()
        self.meeting_hint_frame.raise_()
        # 强制 hint frame 至少 80px,重排父布局
        self.meeting_hint_frame.setMinimumHeight(80)
        self.meeting_hint_frame.updateGeometry()
        if self.meeting_hint_frame.parentWidget():
            self.meeting_hint_frame.parentWidget().updateGeometry()
        # 诊断
        _diag = ROOT / "runtime" / "meeting-hint.log"
        try:
            with open(_diag, "a", encoding="utf-8") as f:
                g = self.meeting_hint_frame.geometry()
                vis = self.meeting_hint_frame.isVisible()
                hid = self.meeting_hint_frame.isHidden()
                par = self.meeting_hint_frame.parentWidget()
                par_name = par.objectName() if par else "(无)"
                f.write(
                    f"  [show] visible={vis} hidden={hid} "
                    f"geo=({g.x()},{g.y()} {g.width()}x{g.height()}) "
                    f"parent={par_name}\n"
                )
                # 看 frame 在 parent 里第几个 widget
                if par:
                    for i in range(par.layout().count() if par.layout() else 0):
                        item = par.layout().itemAt(i)
                        w = item.widget()
                        if w is self.meeting_hint_frame:
                            f.write(f"  在 root layout 第 {i} 位\n")
                            break
        except Exception as e:
            pass

    def _render_current_meeting_hint(self):
        """渲染当前光标位置的候选信息到提醒条。"""
        if not self._meeting_candidates:
            self.meeting_hint_frame.hide()
            return
        c = self._meeting_candidates[self._meeting_cur_idx]
        # speakers 字典 → 字符串
        spk_list = ", ".join(f"{k}:{v}" for k, v in c["speakers"].items())
        cur = self._meeting_cur_idx + 1
        total = len(self._meeting_candidates)
        start_short = c["start"][5:16].replace("T", " ")
        end_short = c["end"][11:16]
        snippet = c.get("snippet", "")[:60]
        self.meeting_hint_label.setText(
            f"📅 检测到可能是会议:{start_short}—{end_short} "
            f"({c['duration_min']:.0f} 分 · {c['segments']} 段 · "
            f"说话人 {spk_list}) · 第 {cur}/{total} 条\n"
            f"     片段:{snippet}…"
        )

    def _on_meeting_hint_preview(self):
        """点「预览内容」→ 打开预览窗口看完整转写,在里头做决定。"""
        if not self._meeting_candidates:
            return
        c = self._meeting_candidates[self._meeting_cur_idx]
        try:
            dlg = MeetingPreviewDialog(self, candidate=c)
            result = dlg.exec()
            # MeetingPreviewDialog 通过 done(code) 报告用户决定
            # code: 1 = 生成纪要, 2 = 不是会议(dismiss), 3 = 跳过(下一个), 0 = 关闭
            if result == 1:
                self._on_meeting_hint_make()
            elif result == 2:
                # 标 dismissed,移除当前,刷新
                import meeting_detector
                meeting_detector.mark_dismissed(c["day"], c["start"], c["end"])
                self._meeting_candidates.pop(self._meeting_cur_idx)
                if self._meeting_cur_idx >= len(self._meeting_candidates):
                    self._meeting_cur_idx = max(0, len(self._meeting_candidates) - 1)
                if self._meeting_candidates:
                    self._render_current_meeting_hint()
                else:
                    self.meeting_hint_frame.hide()
            elif result == 3:
                self._on_meeting_hint_skip()
        except NameError:
            QMessageBox.warning(self, "预览功能未加载", "MeetingPreviewDialog 类未定义")

    def _on_meeting_hint_make(self):
        """点「生成纪要」→ 打开历史窗口,跳到对应日期 + 自动多选那几段。"""
        if not self._meeting_candidates:
            return
        c = self._meeting_candidates[self._meeting_cur_idx]
        day = dt.date.fromisoformat(c["day"])
        # 打开历史窗口,跳到那天 + 预选区间
        try:
            dlg = HistoryWindow(self)
            dlg.show()
            # 先切到对应日期
            if hasattr(dlg, "_select_day"):
                dlg._select_day(day)
            # 预选指定时间范围的所有行(start ≤ row.start ≤ end)
            if hasattr(dlg, "_select_time_range"):
                dlg._select_time_range(c["start"], c["end"])
            else:
                # 兼容:没这个方法,只是给个提示
                QMessageBox.information(
                    self, "选段提示",
                    f"已打开 {day} 的历史窗口。\n\n"
                    f"请手动多选 {c['start'][11:16]}—{c['end'][11:16]} 的段落,\n"
                    f"然后右键 → 「生成会议纪要」"
                )
            # 把此候选标记为 exported(避免下次再提醒)
            import meeting_detector
            meeting_detector.mark_exported(c["day"], c["start"], c["end"])
        except Exception as e:
            QMessageBox.warning(self, "打开失败", str(e))
            return
        # 移除当前候选,看下一个
        self._meeting_candidates.pop(self._meeting_cur_idx)
        if self._meeting_cur_idx >= len(self._meeting_candidates):
            self._meeting_cur_idx = max(0, len(self._meeting_candidates) - 1)
        self._render_current_meeting_hint()

    def _on_meeting_hint_skip(self):
        """跳过 = 看下一个候选,但不标记 dismissed(下次还会出现)。"""
        if not self._meeting_candidates:
            return
        self._meeting_cur_idx = (self._meeting_cur_idx + 1) % len(self._meeting_candidates)
        self._render_current_meeting_hint()

    def _on_meeting_hint_dismiss_all(self):
        """全部忽略 = 都 mark_dismissed,下次启动也不会再提醒。"""
        r = QMessageBox.question(
            self, "确认",
            f"把这 {len(self._meeting_candidates)} 个候选都标为「不是会议」?\n"
            f"以后启动不再提醒这些区间。",
        )
        if r != QMessageBox.Yes:
            return
        try:
            import meeting_detector
            for c in self._meeting_candidates:
                meeting_detector.mark_dismissed(c["day"], c["start"], c["end"])
        except Exception:
            pass
        self._meeting_candidates = []
        self.meeting_hint_frame.hide()

    def _auto_summary_check(self):
        try:
            now = dt.datetime.now()
            hhmm = CONFIG["summary"].get("trigger_hm", "23:30")
            hh, mm = [int(x) for x in hhmm.split(":")]
            trigger_time = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if now >= trigger_time and not note_path().exists():
                provider = CONFIG["summary"].get("provider", "deepseek")
                key_env = {"deepseek": "DEEPSEEK_API_KEY",
                           "anthropic": "ANTHROPIC_API_KEY",
                           "openai": "OPENAI_API_KEY"}.get(provider)
                if key_env and self._provider_api_key(key_env):
                    self.bottom_status.setText(f"自动触发每日总结({hhmm} 兜底)...")
                    self._do_summary_now()
        except Exception as e:
            self.bottom_status.setText(f"自动总结检查异常:{e}")

    def paintEvent(self, event):
        """主窗口自定义背景：画 pixmap，保证 QSS 怎么刷新都不会丢失。"""
        if getattr(self, "_bg_pixmap", None) and not self._bg_pixmap.isNull():
            from PySide6.QtGui import QPainter
            p = QPainter(self)
            # 按窗口大小缩放（保持比例，覆盖整个窗口）
            scaled = self._bg_pixmap.scaled(
                self.size(),
                Qt.KeepAspectRatioByExpanding,
                Qt.SmoothTransformation,
            )
            # 居中绘制
            x = (self.width() - scaled.width()) // 2
            y = (self.height() - scaled.height()) // 2
            p.drawPixmap(x, y, scaled)
            p.end()
        super().paintEvent(event)

    def closeEvent(self, e):
        if self.recorder.is_running() or self.transcriber.is_running():
            r = QMessageBox.question(
                self, "确认", "录音 / 转写正在运行,确认关闭?")
            if r != QMessageBox.Yes:
                e.ignore()
                return
        self.recorder.stop()
        self.transcriber.stop()
        e.accept()


# ============================================================
# 历史 / 管理窗口
# ============================================================
class HistoryWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("历史 / 管理")
        self.resize(960, 600)
        self.current_day: dt.date | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 12)
        root.setSpacing(12)

        # 主体:左日期 + 右段表
        split = QSplitter(Qt.Horizontal)

        # 左侧
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(6)
        l_label = QLabel("日 期")
        l_label.setStyleSheet("color:#cc785c; font-size:12px; letter-spacing:4px; padding:4px;")
        ll.addWidget(l_label)
        self.day_list = QListWidget()
        self.day_list.setMaximumWidth(160)
        self.day_list.currentRowChanged.connect(self._on_day_change)
        ll.addWidget(self.day_list, 1)
        split.addWidget(left)

        # 右侧
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(8)

        head = QHBoxLayout()
        self.head_label = QLabel("选择左侧日期")
        self.head_label.setStyleSheet("color:#1c1b18; font-size:14px; font-weight:700;")
        head.addWidget(self.head_label, 1)
        btn_refresh = QPushButton("刷新")
        btn_refresh.clicked.connect(self._reload_current)
        btn_md = QPushButton("打开当日笔记")
        btn_md.clicked.connect(self._open_day_md)
        head.addWidget(btn_md)
        head.addWidget(btn_refresh)
        rl.addLayout(head)

        cols = ["#", "时间", "时长", "来源", "说话人", "文本"]
        self.table = QTableWidget(0, len(cols))
        self.table.setHorizontalHeaderLabels(cols)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._right_click)
        header = self.table.horizontalHeader()
        for i, w in enumerate([50, 80, 60, 50, 90, 999]):
            self.table.setColumnWidth(i, w)
        header.setSectionResizeMode(5, QHeaderView.Stretch)
        rl.addWidget(self.table, 1)

        bottom = QHBoxLayout()
        self.bottom_label = QLabel("提示 · Cmd/Shift 多选 · 删除选中" if sys.platform == "darwin" else "提示 · Ctrl/Shift 多选 · 删除选中")
        self.bottom_label.setStyleSheet("color:#3a3833; font-size:13px;")
        bottom.addWidget(self.bottom_label, 1)
        btn_edit_jsonl = QPushButton("编辑 jsonl")
        btn_edit_jsonl.clicked.connect(self._edit_jsonl)
        btn_del = QPushButton("删除选中")
        btn_del.setObjectName("dangerBtn")
        btn_del.clicked.connect(self._delete_selected)
        bottom.addWidget(btn_edit_jsonl)
        bottom.addWidget(btn_del)
        rl.addLayout(bottom)

        split.addWidget(right)
        split.setStretchFactor(1, 1)
        split.setSizes([160, 800])
        root.addWidget(split, 1)

        self._reload_days()

    def _reload_days(self):
        self.day_list.clear()
        for d in list_history_days():
            self.day_list.addItem(d.isoformat())
        if self.day_list.count() > 0:
            self.day_list.setCurrentRow(0)

    def _on_day_change(self, row: int):
        if row < 0:
            return
        try:
            day = dt.date.fromisoformat(self.day_list.item(row).text())
            self._load_day(day)
        except ValueError:
            return

    def _load_day(self, day: dt.date):
        self.current_day = day
        self.table.setRowCount(0)
        records = list(read_jsonl(transcript_path(day)))
        self._cur_records = records   # 缓存,用于「定位 wav」按行索引找
        for i, r in enumerate(records):
            ts = (r.get("start") or "")[11:19]
            dur = f"{r.get('duration_sec', 0):.1f}s"
            src = "导入" if r.get("source") == "tx_import" else "实时"
            spk = r.get("speaker_name") or "?"
            sim = r.get("speaker_sim", 0)
            if spk == "未知" and sim:
                spk = f"?({sim:.2f})"
            text = (r.get("text") or "").replace("\n", " ")
            if len(text) > 300:
                text = text[:300] + "…"
            self.table.insertRow(i)
            for col, val in enumerate([str(i), ts, dur, src, spk, text]):
                item = QTableWidgetItem(val)
                if col != 5:
                    item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(i, col, item)
        chars = sum(len(r.get("text") or "") for r in records)
        self.head_label.setText(f"{day.isoformat()}   ·  共 {len(records)} 段 / {chars} 字")
        self.bottom_label.setText("提示 · Cmd/Shift 多选 · 删除选中" if sys.platform == "darwin" else "提示 · Ctrl/Shift 多选 · 删除选中")

    def _reload_current(self):
        if self.current_day:
            self._load_day(self.current_day)

    # ─── 给外部(launcher 会议候选)用的辅助 ───
    def _select_day(self, day: dt.date) -> bool:
        """让 day_list 切到指定日期。找不到返回 False。"""
        target = day.isoformat()
        for i in range(self.day_list.count()):
            if self.day_list.item(i).text() == target:
                self.day_list.setCurrentRow(i)
                return True
        return False

    def _select_time_range(self, start_iso: str, end_iso: str) -> int:
        """在已加载的 table 里多选第 2 列(时间) ∈ [start, end] 的行。
        返回选中的行数。
        """
        try:
            s = dt.datetime.fromisoformat(start_iso)
            e = dt.datetime.fromisoformat(end_iso)
        except Exception:
            return 0
        if not self.current_day:
            return 0
        self.table.clearSelection()
        selected = 0
        first_row = None
        sel_model = self.table.selectionModel()
        from PySide6.QtCore import QItemSelection, QItemSelectionModel
        for row in range(self.table.rowCount()):
            ts_str = self.table.item(row, 1).text()   # HH:MM:SS
            try:
                hh, mm, ss = [int(x) for x in ts_str.split(":")]
                row_ts = dt.datetime.combine(
                    self.current_day, dt.time(hh, mm, ss)
                )
            except Exception:
                continue
            if s <= row_ts <= e:
                if first_row is None:
                    first_row = row
                idx_l = self.table.model().index(row, 0)
                idx_r = self.table.model().index(row, self.table.columnCount() - 1)
                sel_model.select(
                    QItemSelection(idx_l, idx_r),
                    QItemSelectionModel.Select | QItemSelectionModel.Rows
                )
                selected += 1
        if first_row is not None:
            self.table.scrollToItem(self.table.item(first_row, 0))
        self.bottom_label.setText(
            f"✓ 已自动选中 {selected} 段({start_iso[11:16]}—{end_iso[11:16]}) "
            f"· 右键 → 「生成会议纪要」"
        )
        return selected

    def _delete_selected(self):
        if not self.current_day:
            return
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        if not rows:
            QMessageBox.information(self, "提示", "请先选中至少一段")
            return
        preview = []
        for r in rows[:3]:
            ts = self.table.item(r, 1).text()
            txt = self.table.item(r, 5).text()[:30]
            preview.append(f"  #{r}  [{ts}]  {txt}")
        more = f"\n  …还有 {len(rows) - 3} 段" if len(rows) > 3 else ""
        msg = (f"将删除 {len(rows)} 段:\n\n" + "\n".join(preview) + more +
               "\n\n是否同时删除原始 WAV?")
        box = QMessageBox(self)
        box.setWindowTitle("确认删除")
        box.setText(msg)
        btn_delete_wav = box.addButton("是 · 连 WAV 一起删", QMessageBox.YesRole)
        btn_text_only = box.addButton("否 · 只删文本", QMessageBox.NoRole)
        btn_cancel = box.addButton("取消", QMessageBox.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is None or clicked is btn_cancel:
            return
        if clicked is not btn_delete_wav and clicked is not btn_text_only:
            return
        delete_wav = clicked is btn_delete_wav
        try:
            n_seg, n_wav = delete_segments(
                self.current_day,
                rows,
                delete_wav=delete_wav,
            )
        except OSError as exc:
            self.bottom_label.setText("删除失败 · 原记录未改动")
            QMessageBox.warning(
                self,
                "删除失败",
                f"无法删除选中的记录，请确认音频文件没有被其他程序占用后重试。\n\n{exc}",
            )
            return
        self.bottom_label.setText(f"已删 {n_seg} 段 · {n_wav} 个 WAV")
        self._load_day(self.current_day)

    def _edit_jsonl(self):
        if not self.current_day:
            return
        p = transcript_path(self.current_day)
        if p.exists():
            open_path(str(p))

    def _open_day_md(self):
        if not self.current_day:
            return
        p = note_path(self.current_day)
        if not p.exists():
            QMessageBox.information(self, "提示", f"该日没有总结 MD\n路径:{p}")
            return
        open_path(str(p))

    def _right_click(self, pos):
        row = self.table.rowAt(pos.y())
        if row < 0:
            return
        menu = QMenu(self)
        # 顶部：生成会议纪要（最常用，置顶）
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        n_sel = len(rows)
        act_meeting = menu.addAction(f"📝 生成会议纪要（{n_sel} 段）" if n_sel else "📝 生成会议纪要")
        act_meeting.setEnabled(n_sel > 0)
        act_meeting.triggered.connect(self._export_meeting)
        menu.addSeparator()
        # 听原声 / 定位 wav
        act_play = menu.addAction("▶ 播放原声 (这一段)")
        act_play.triggered.connect(lambda: self._play_wav_for_row(row))
        act_locate = menu.addAction("📁 在文件夹中显示 wav")
        act_locate.triggered.connect(lambda: self._locate_wav_for_row(row))
        menu.addSeparator()
        # 说话人标记
        speakers = load_speakers()
        for sp in speakers:
            act = menu.addAction(f"标记为:{sp['name']}")
            act.triggered.connect(lambda _=False, s=sp: self._mark_as(s["id"], s["name"]))
        if speakers:
            menu.addSeparator()
        act_new = menu.addAction("新建说话人...")
        act_new.triggered.connect(self._mark_as_new)
        act_unk = menu.addAction("标记为'未知'")
        act_unk.triggered.connect(lambda: self._mark_as(None, "未知"))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _wav_path_for_row(self, row: int) -> Path | None:
        """从缓存 records 拿 wav 字段 → 转成绝对路径。"""
        recs = getattr(self, "_cur_records", None)
        if not recs or row < 0 or row >= len(recs):
            return None
        wav_rel = recs[row].get("wav")
        if not wav_rel:
            return None
        p = ROOT / wav_rel
        return p if p.exists() else None

    def _play_wav_for_row(self, row: int):
        """用系统默认播放器(Windows Media Player / Groove) 播放这段 wav。"""
        p = self._wav_path_for_row(row)
        if p is None:
            QMessageBox.information(
                self, "找不到原声",
                "这一段没有对应 wav 文件(可能是导入/外部源,或文件被删了)"
            )
            return
        try:
            open_path(str(p))   # Windows 默认关联
        except Exception as e:
            QMessageBox.warning(self, "播放失败", str(e))

    def _locate_wav_for_row(self, row: int):
        """在资源管理器打开 wav 所在目录,并高亮选中这个文件。"""
        p = self._wav_path_for_row(row)
        if p is None:
            QMessageBox.information(
                self, "找不到原声",
                "这一段没有对应 wav 文件(可能是导入/外部源,或文件被删了)"
            )
            return
        try:
            # /select, 让 explorer 打开目录并高亮文件
            subprocess.Popen(["explorer", "/select,", str(p)])
        except Exception as e:
            QMessageBox.warning(self, "打开失败", str(e))

    def _export_meeting(self):
        """收集选中行的 segments，弹 MeetingExportDialog。"""
        if not self.current_day:
            return
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        if not rows:
            QMessageBox.information(self, "提示", "请先选中至少一段")
            return
        # 从 jsonl 读取完整 segment 数据
        from common import read_jsonl
        records = list(read_jsonl(transcript_path(self.current_day)))
        valid = [r for r in records if r.get("text")]
        try:
            segments = [valid[i] for i in rows if 0 <= i < len(valid)]
        except Exception as ex:
            QMessageBox.warning(self, "失败", f"读 segments 失败: {ex}")
            return
        if not segments:
            return
        dlg = MeetingExportDialog(segments, parent=self)
        dlg.exec()

    def _mark_as(self, sp_id: str | None, sp_name: str):
        if not self.current_day:
            return
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        if not rows:
            return
        import numpy as np
        from common import write_jsonl
        records = list(read_jsonl(transcript_path(self.current_day)))
        new_embs = [records[i].get("embedding") for i in rows
                    if 0 <= i < len(records) and records[i].get("embedding")]
        for i in rows:
            if 0 <= i < len(records):
                records[i]["speaker_id"] = sp_id
                records[i]["speaker_name"] = sp_name
                records[i]["speaker_sim"] = 1.0
        if sp_id and new_embs:
            speakers = load_speakers()
            for sp in speakers:
                if sp["id"] == sp_id:
                    old = np.array(sp["embedding"], dtype=np.float32)
                    n_old = sp.get("samples", 1)
                    summed = old * n_old
                    for e in new_embs:
                        summed = summed + np.array(e, dtype=np.float32)
                    avg = summed / (n_old + len(new_embs))
                    avg = avg / (np.linalg.norm(avg) + 1e-9)
                    sp["embedding"] = [round(float(x), 4) for x in avg.tolist()]
                    sp["samples"] = n_old + len(new_embs)
                    break
            save_speakers(speakers)
        write_jsonl(transcript_path(self.current_day), records)
        self._load_day(self.current_day)
        self.bottom_label.setText(f"已把 {len(rows)} 段标记为 '{sp_name}' · 已更新声纹库")

    def _mark_as_new(self):
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "新建说话人", "新说话人的名字:")
        if not ok or not name.strip():
            return
        name = name.strip()
        if not self.current_day:
            return
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        if not rows:
            return
        import numpy as np
        records = list(read_jsonl(transcript_path(self.current_day)))
        embs = [records[i].get("embedding") for i in rows
                if 0 <= i < len(records) and records[i].get("embedding")]
        if not embs:
            QMessageBox.warning(self, "失败", "选中的段没有 embedding\n(只有 ≥ 2 秒的段才有声纹)")
            return
        avg = np.mean([np.array(e, dtype=np.float32) for e in embs], axis=0)
        avg = avg / (np.linalg.norm(avg) + 1e-9)
        new_id = f"sp_{int(time.time())}"
        speakers = load_speakers()
        speakers.append({
            "id": new_id,
            "name": name,
            "embedding": [round(float(x), 4) for x in avg.tolist()],
            "samples": len(embs),
            "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        })
        save_speakers(speakers)
        self._mark_as(new_id, name)


# ============================================================
# 声纹库窗口
# ============================================================
class SpeakersWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("声纹库")
        self.resize(760, 560)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 12)
        root.setSpacing(10)

        head = QLabel("已注册的说话人")
        head.setStyleSheet("color:#1c1b18; font-size:14px; font-weight:700; letter-spacing:2px;")
        root.addWidget(head)

        cols = ["ID", "名字", "样本数", "创建时间"]
        self.table = QTableWidget(0, len(cols))
        self.table.setHorizontalHeaderLabels(cols)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        for i, w in enumerate([140, 200, 80, 240]):
            self.table.setColumnWidth(i, w)
        root.addWidget(self.table)

        op = QHBoxLayout()
        btn_ren = QPushButton("重命名")
        btn_ren.clicked.connect(self._rename)
        btn_me = QPushButton("设为 '我'")
        btn_me.clicked.connect(self._set_as_me)
        btn_del = QPushButton("删除")
        btn_del.setObjectName("dangerBtn")
        btn_del.clicked.connect(self._delete)
        op.addWidget(btn_ren)
        op.addWidget(btn_me)
        op.addStretch(1)
        op.addWidget(btn_del)
        root.addLayout(op)

        # 注册新人
        reg_label = QLabel("注 册 新 说 话 人")
        reg_label.setStyleSheet("color:#cc785c; font-size:12px; letter-spacing:6px; margin-top:8px;")
        root.addWidget(reg_label)

        reg = QFrame()
        reg.setObjectName("statusCard")
        rl = QVBoxLayout(reg)
        rl.setContentsMargins(16, 14, 16, 14)
        rl.setSpacing(8)

        tip = QLabel("从今日历史里选一段时长 ≥ 3 秒、明显是目标人声音的段")
        tip.setStyleSheet("color:#3a3833; font-size:13px;")
        rl.addWidget(tip)

        form = QFormLayout()
        form.setContentsMargins(0, 4, 0, 0)
        form.setSpacing(8)
        self.cand_combo = QComboBox()
        form.addRow("选择段:", self.cand_combo)
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("如:我、老婆、张三")
        form.addRow("起名:", self.name_input)
        rl.addLayout(form)

        btn_reg = QPushButton("注册")
        btn_reg.setObjectName("primaryBtn")
        btn_reg.clicked.connect(self._register)
        rl.addWidget(btn_reg)

        root.addWidget(reg)

        self.bottom = QLabel("提示 · 第一次用 · 先注册一个 '我'")
        self.bottom.setStyleSheet("color:#3a3833; font-size:13px; margin-top:4px;")
        root.addWidget(self.bottom)

        self._reload()
        self._reload_candidates()

    def _reload(self):
        self.table.setRowCount(0)
        for sp in load_speakers():
            r = self.table.rowCount()
            self.table.insertRow(r)
            for c, val in enumerate([sp["id"], sp["name"], str(sp.get("samples", 1)), sp.get("created_at", "")]):
                item = QTableWidgetItem(val)
                if c != 1:
                    item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(r, c, item)

    def _reload_candidates(self):
        records = list(read_jsonl(transcript_path()))
        self._candidates = []
        for r in records:
            if r.get("embedding") and (r.get("duration_sec") or 0) >= 3.0 and r.get("text"):
                ts = (r.get("start") or "")[11:19]
                label = f"[{ts}] {r['duration_sec']:.1f}s  {r['text'][:40]}"
                self._candidates.append((label, r))
        self.cand_combo.clear()
        self.cand_combo.addItems([c[0] for c in self._candidates])
        if not self._candidates:
            self.bottom.setText("今日还没有带声纹的段 · 需要 transcriber 录几段 ≥ 3 秒的话")

    def _register(self):
        idx = self.cand_combo.currentIndex()
        if idx < 0:
            QMessageBox.information(self, "提示", "请先选一段")
            return
        name = self.name_input.text().strip()
        if not name:
            QMessageBox.information(self, "提示", "请填名字")
            return
        _, rec = self._candidates[idx]
        emb = rec.get("embedding")
        if not emb:
            QMessageBox.critical(self, "失败", "该段没有 embedding")
            return
        speakers = load_speakers()
        new_id = f"sp_{int(time.time())}"
        speakers.append({
            "id": new_id, "name": name, "embedding": emb,
            "samples": 1,
            "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        })
        save_speakers(speakers)
        self.name_input.clear()
        self._reload()
        self.bottom.setText(f"已注册 · {name} ({new_id}) · 重启 transcriber 后生效")

    def _selected_id(self) -> str | None:
        r = self.table.currentRow()
        if r < 0:
            return None
        return self.table.item(r, 0).text()

    def _rename(self):
        sid = self._selected_id()
        if not sid:
            return
        from PySide6.QtWidgets import QInputDialog
        speakers = load_speakers()
        for sp in speakers:
            if sp["id"] == sid:
                new, ok = QInputDialog.getText(self, "重命名", f"把 '{sp['name']}' 改为:")
                if ok and new.strip():
                    sp["name"] = new.strip()
                    save_speakers(speakers)
                    self._reload()
                return

    def _delete(self):
        sid = self._selected_id()
        if not sid:
            return
        speakers = load_speakers()
        target = next((sp for sp in speakers if sp["id"] == sid), None)
        if not target:
            return
        if QMessageBox.question(self, "确认", f"删除声纹 '{target['name']}'?") != QMessageBox.Yes:
            return
        save_speakers([sp for sp in speakers if sp["id"] != sid])
        self._reload()

    def _set_as_me(self):
        sid = self._selected_id()
        if not sid:
            return
        speakers = load_speakers()
        for sp in speakers:
            if sp["id"] == sid:
                sp["name"] = "我"
        save_speakers(speakers)
        self._reload()


# ============================================================
# 热词编辑弹窗
# ============================================================
class MicDeviceDialog(QDialog):
    """麦克风设备选择:列出所有输入设备,手动切换或恢复自动。
    写 runtime/preferred_device.json,recorder 每 2 秒读一次,选了 2 秒内自动切过去。"""

    PREF = ROOT / "runtime" / "preferred_device.json"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("麦克风设备")
        self.resize(560, 460)
        v = QVBoxLayout(self)
        v.setContentsMargins(20, 18, 20, 18)
        v.setSpacing(12)

        # 当前状态
        cur_name, cur_mode = self._current_state()
        self.head = QLabel()
        self.head.setWordWrap(True)
        self.head.setStyleSheet("color:#1c1b18; font-size:14px;")
        self.head.setText(self._head_text(cur_name, cur_mode))
        v.addWidget(self.head)

        hint = QLabel("选一个设备点「切到这个」= 手动锁定;点「恢复自动」= 主麦(DJI)优先、插上自动用。\n"
                      "切换 2 秒内生效,不用重启。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#8e8a82; font-size:12px;")
        v.addWidget(hint)

        self.listw = QListWidget()
        self.listw.setStyleSheet(
            "QListWidget{background:#fff;border:1px solid rgba(28,27,24,0.12);border-radius:8px;"
            "font-size:13px;padding:4px;} QListWidget::item{padding:7px 8px;border-radius:5px;}"
            "QListWidget::item:selected{background:#f4ead8;color:#1c1b18;}")
        v.addWidget(self.listw, 1)
        self._reload_list(cur_name)

        row = QHBoxLayout()
        row.setSpacing(10)
        btn_refresh = QPushButton("刷新列表")
        btn_refresh.clicked.connect(lambda: self._reload_list(self._current_state()[0]))
        row.addWidget(btn_refresh)
        row.addStretch(1)
        btn_auto = QPushButton("恢复自动(主麦优先)")
        btn_auto.clicked.connect(self._set_auto)
        row.addWidget(btn_auto)
        btn_pick = QPushButton("切到这个")
        btn_pick.setObjectName("primaryBtn")
        btn_pick.clicked.connect(self._set_manual)
        row.addWidget(btn_pick)
        v.addLayout(row)

    def _input_devices(self) -> list[tuple[int, str]]:
        try:
            import sounddevice as sd
            out, seen = [], set()
            for i, d in enumerate(sd.query_devices()):
                if d.get("max_input_channels", 0) > 0:
                    nm = d.get("name") or ""
                    if nm and nm not in seen:  # 去重(同名多 host API)
                        seen.add(nm); out.append((i, nm))
            return out
        except Exception:
            return []

    def _kw(self):
        a = CONFIG.get("audio", {})
        return ([k.lower() for k in a.get("device_name_keywords", [])],
                [k.lower() for k in a.get("fallback_devices", [])])

    def _current_state(self):
        """返回 (当前实际在用的设备名, 模式auto/manual)。"""
        name = ""
        try:
            st = read_recorder_status()
            if st:
                name = st.get("device_name", "") or ""
        except Exception:
            pass
        mode = "auto"
        try:
            if self.PREF.exists():
                import json as _j
                mode = _j.loads(self.PREF.read_text(encoding="utf-8")).get("mode", "auto")
        except Exception:
            pass
        return name, mode

    def _head_text(self, name, mode):
        m = "自动(主麦优先)" if mode != "manual" else "手动锁定"
        return f"当前正在用:<b>{name or '(未知/录音未运行)'}</b><br>切换模式:{m}"

    def _reload_list(self, cur_name):
        self.listw.clear()
        kw, fb = self._kw()
        for idx, nm in self._input_devices():
            low = nm.lower()
            tag = ("[主麦]" if any(k in low for k in kw)
                   else ("[备用]" if any(k in low for k in fb) else ""))
            mark = "  ← 正在用" if nm == cur_name else ""
            it = QListWidgetItem(f"{tag} {nm}{mark}".strip())
            it.setData(Qt.UserRole, nm)
            self.listw.addItem(it)
            if nm == cur_name:
                self.listw.setCurrentItem(it)

    def _write_pref(self, data: dict):
        self.PREF.parent.mkdir(parents=True, exist_ok=True)
        import json as _j
        self.PREF.write_text(_j.dumps(data, ensure_ascii=False), encoding="utf-8")

    def _set_manual(self):
        it = self.listw.currentItem()
        if it is None:
            QMessageBox.information(self, "提示", "先在列表里点选一个设备")
            return
        nm = it.data(Qt.UserRole)
        self._write_pref({"mode": "manual", "name": nm})
        self.head.setText(self._head_text(nm, "manual"))
        QMessageBox.information(self, "已切换",
            f"已锁定:{nm}\n\nrecorder 2 秒内切过去。\n(想恢复自动,再来点「恢复自动」)")

    def _set_auto(self):
        self._write_pref({"mode": "auto"})
        self.head.setText(self._head_text(self._current_state()[0], "auto"))
        QMessageBox.information(self, "已恢复自动",
            "已恢复自动:主麦(DJI)优先,插上自动用,拔了自动回退备用。")


class HotwordsDialog(QDialog):
    """编辑 hotwords.txt — 一行一个词。保存后自动重启 transcriber 生效。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("热词编辑 · hotwords.txt")
        self.resize(560, 620)
        self._parent_launcher = parent

        v = QVBoxLayout(self)
        v.setContentsMargins(20, 18, 20, 18)
        v.setSpacing(12)

        # 顶部说明
        head = QLabel("热 词 表")
        head.setStyleSheet("color:#1c1b18; font-size:18px; font-weight:700;")
        v.addWidget(head)

        tip = QLabel(
            "一行一个词。# 开头的是注释会被忽略。\n"
            "适合写：人名（张老师、赵同事）、产品名、专有名词、易错词。\n"
            "保存后自动重启转写器，新转写立刻生效。"
        )
        tip.setStyleSheet("color:#5a564e; font-size:13px; line-height:1.6;")
        tip.setWordWrap(True)
        v.addWidget(tip)

        # 编辑区
        self.editor = QPlainTextEdit()
        self.editor.setFrameShape(QPlainTextEdit.NoFrame)
        self.editor.setStyleSheet(
            "background:#ffffff; color:#1c1b18; padding:10px; "
            "border:1px solid rgba(28,27,24,0.10); border-radius:8px; "
            "font-family:'Consolas','Cascadia Mono'; font-size:13px; line-height:1.6;"
        )
        self.path = ROOT / "hotwords.txt"
        if self.path.exists():
            self.editor.setPlainText(self.path.read_text(encoding="utf-8"))
        else:
            self.editor.setPlainText(
                "# 热词表 — 一行一个词,以 # 开头的是注释。\n"
                "# 例：\n"
                "# 张老师\n"
                "# 赵同事\n"
                "# 大疆\n\n"
            )
        v.addWidget(self.editor, 1)

        # 字数统计
        self.count_lbl = QLabel("")
        self.count_lbl.setStyleSheet("color:#8e8a82; font-size:12px;")
        self.editor.textChanged.connect(self._update_count)
        v.addWidget(self.count_lbl)
        self._update_count()

        # 按钮行
        h = QHBoxLayout()
        h.addStretch(1)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        h.addWidget(cancel_btn)
        save_btn = QPushButton("保存并重启转写器")
        save_btn.setObjectName("primaryBtn")
        save_btn.clicked.connect(self._save)
        h.addWidget(save_btn)
        v.addLayout(h)

    def _update_count(self):
        text = self.editor.toPlainText()
        words = [
            ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        self.count_lbl.setText(f"当前 {len(words)} 个热词")

    def _save(self):
        try:
            self.path.write_text(self.editor.toPlainText(), encoding="utf-8")
        except Exception as ex:
            QMessageBox.warning(self, "保存失败", str(ex))
            return
        # 重启 transcriber 让新热词生效
        if self._parent_launcher:
            try:
                self._parent_launcher.transcriber.stop()
                import time as _t
                _t.sleep(0.3)
                if not self._parent_launcher.transcriber.start():
                    raise RuntimeError(self._parent_launcher.transcriber.last_error)
                self._parent_launcher.bottom_status.setText(
                    f"热词已保存 · 转写器已重启 · 新热词立刻生效"
                )
            except Exception as ex:
                QMessageBox.information(
                    self, "保存成功",
                    f"热词已保存，但重启转写器时出错：{ex}\n你也可以手动停止再启动。"
                )
        self.accept()


# ============================================================
# 会议纪要导出对话框 + 异步 Worker
# ============================================================
class MeetingExportWorker(QObject):
    """后台线程跑 export_meeting，通过 Signal 推送进度。"""
    progress = Signal(str, str)   # (stage, msg)
    finished = Signal(str)        # 输出 md 路径
    failed = Signal(str)          # 错误信息

    def __init__(self, segments, title, category):
        super().__init__()
        self.segments = segments
        self.title = title
        self.category = category

    def run(self):
        try:
            from meeting_export import export_meeting
            out = export_meeting(
                self.segments,
                title=self.title,
                category=self.category,
                on_progress=lambda stage, msg: self.progress.emit(stage, msg),
            )
            self.finished.emit(str(out))
        except Exception as e:
            self.failed.emit(str(e))


class MeetingExportDialog(QDialog):
    """选完段后弹的确认 + 进度对话框。"""

    def __init__(self, segments: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("生成会议纪要")
        self.resize(540, 380)
        self.segments = segments
        self._worker_thread = None

        v = QVBoxLayout(self)
        v.setContentsMargins(20, 18, 20, 18)
        v.setSpacing(12)

        # 标题
        head = QLabel("生 成 会 议 纪 要")
        head.setStyleSheet("color:#1c1b18; font-size:18px; font-weight:700;")
        v.addWidget(head)

        # 选中段统计
        n = len(segments)
        starts = sorted([s.get("start", "") for s in segments if s.get("start")])
        first = starts[0] if starts else ""
        last = starts[-1] if starts else ""
        dur_sec = sum(s.get("duration_sec", 0) for s in segments)
        h = int(dur_sec // 3600); m = int((dur_sec % 3600) // 60)
        dur_str = f"{h}h{m}min" if h else f"{m}min"
        speakers = sorted({s.get("speaker_name") or "未知"
                          for s in segments if s.get("speaker_name")})
        spk_str = ", ".join(speakers) if speakers else "未识别"

        stats = QLabel(
            f"  · {n} 段 / 约 {dur_str}\n"
            f"  · 时间: {first[11:16] if first else '—'} ~ {last[11:16] if last else '—'}\n"
            f"  · 说话人: {spk_str}"
        )
        stats.setStyleSheet("color:#3a3833; font-size:13px; line-height:1.7;")
        v.addWidget(stats)

        # 分类选择（跟 vault 既有目录对齐）
        cat_lbl = QLabel("分类（决定 MD 落到 vault 哪个目录、frontmatter 怎么写）")
        cat_lbl.setStyleSheet("color:#5a564e; font-size:12px;")
        v.addWidget(cat_lbl)
        self.cat_combo = QComboBox()
        self.cat_combo.addItems([
            "周会记录", "直播复盘", "AI课项目", "客户拜访", "通用会议"
        ])
        self.cat_combo.setStyleSheet("font-size:14px; padding:6px 10px;")
        # 智能默认：根据 speakers 或时间猜默认分类
        if len(speakers) >= 3:
            self.cat_combo.setCurrentText("周会记录")
        elif "张老师" in speakers:
            self.cat_combo.setCurrentText("直播复盘")
        v.addWidget(self.cat_combo)

        # 会议标题输入
        lbl = QLabel("会议标题（可选，留空自动生成）")
        lbl.setStyleSheet("color:#5a564e; font-size:12px;")
        v.addWidget(lbl)
        self.title_input = QLineEdit()
        default_title = f"{first[:10]} {first[11:16] if len(first) >= 16 else ''}".strip()
        self.title_input.setPlaceholderText(default_title)
        self.title_input.setStyleSheet("font-size:14px; padding:6px 10px;")
        v.addWidget(self.title_input)

        # 流程提示
        flow = QLabel(
            "流程：\n"
            "  ① 提取选中段的本地转写文本\n"
            "  ② 将本次所需的选中转写文字发送给云端 AI（DeepSeek）处理\n"
            "  ③ 写到 vault 对应目录（仿照你的既有会议纪要格式）\n"
            "\n"
            "原始音频不会上传；只有你确认生成后，选中的转写文字才会通过"
            "加密连接发送。声年服务端临时处理后转交 DeepSeek，"
            "具体删除期限以隐私说明为准。"
        )
        flow.setStyleSheet("color:#5a564e; font-size:12px; line-height:1.7;")
        flow.setWordWrap(True)
        v.addWidget(flow, 1)

        # 进度
        self.progress_lbl = QLabel("")
        self.progress_lbl.setStyleSheet("color:#cc785c; font-size:13px; font-weight:600;")
        self.progress_lbl.setWordWrap(True)
        v.addWidget(self.progress_lbl)

        # 按钮行
        h_box = QHBoxLayout()
        h_box.addStretch(1)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.clicked.connect(self.reject)
        h_box.addWidget(self.btn_cancel)
        self.btn_start = QPushButton("开始生成")
        self.btn_start.setObjectName("primaryBtn")
        self.btn_start.clicked.connect(self._start)
        h_box.addWidget(self.btn_start)
        v.addLayout(h_box)

    def _start(self):
        from PySide6.QtCore import QThread
        title = self.title_input.text().strip() or None
        category = self.cat_combo.currentText()
        self.btn_start.setEnabled(False)
        self.btn_cancel.setText("关闭窗口")
        self.progress_lbl.setText("启动中...")

        self._worker_thread = QThread(self)
        self._worker = MeetingExportWorker(self.segments, title, category)
        self._worker.moveToThread(self._worker_thread)
        self._worker_thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._worker_thread.quit)
        self._worker.failed.connect(self._worker_thread.quit)
        self._worker_thread.start()

    def _on_progress(self, stage: str, msg: str):
        self.progress_lbl.setText(msg)

    def _on_done(self, path: str):
        self.progress_lbl.setText(f"✓ 完成：{Path(path).name}")
        # 自动打开 MD
        try:
            open_path(path)
        except Exception:
            pass
        QMessageBox.information(
            self, "完成",
            f"会议纪要已生成：\n{path}\n\n"
            f"分类：{self.cat_combo.currentText()}\n"
            f"已落到 vault 对应目录，并在 voice-journal/meetings/ 留一份本地备份。"
        )
        self.accept()

    def _on_failed(self, err: str):
        self.progress_lbl.setText(f"✗ 失败")
        QMessageBox.warning(self, "生成失败", err)
        self.btn_start.setEnabled(True)
        self.btn_cancel.setText("取消")

    def closeEvent(self, e):
        if self._worker_thread and self._worker_thread.isRunning():
            r = QMessageBox.question(
                self, "确认", "后台导出还在进行，确定关闭吗？\n（关闭后导出会继续到完成）"
            )
            if r != QMessageBox.Yes:
                e.ignore()
                return
        e.accept()


# ============================================================
# 贴链接抓取弹窗 + 异步 Worker
# ============================================================
class _MeetingScanWorker(QObject):
    """跨线程扫会议候选 — 通过 Signal 把结果送回主线程。"""
    found = Signal(list)        # 找到候选 → cands list
    finished = Signal()         # 结束(无论是否找到都触发,用于清理线程)

    def run(self):
        _diag = ROOT / "runtime" / "meeting-hint.log"
        try:
            import meeting_detector
            cands = meeting_detector.find_candidates(lookback_days=7)
            with open(_diag, "a", encoding="utf-8") as f:
                f.write(f"  [worker] 找到 {len(cands)} 个候选\n")
            if cands:
                self.found.emit(cands)
                with open(_diag, "a", encoding="utf-8") as f:
                    f.write(f"  [worker] emit found(cands)\n")
        except Exception as e:
            import traceback
            with open(_diag, "a", encoding="utf-8") as f:
                f.write(f"  [worker ERROR] {e}\n{traceback.format_exc()}\n")
        self.finished.emit()


class MeetingPreviewDialog(QDialog):
    """会议候选预览窗口 — 显示完整转写,让用户判断是不是会议。

    用 done(code) 报告:
      1 = 是会议,生成纪要
      2 = 不是会议,标 dismissed
      3 = 跳过,暂不决定
      0 = 关闭
    """
    def __init__(self, parent=None, candidate: dict = None):
        super().__init__(parent)
        self._cand = candidate or {}
        self.setWindowTitle(f"会议候选预览 · {self._cand.get('day', '')}")
        self.resize(900, 720)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(12)

        # ── 顶部:候选元信息 ──
        meta = QFrame()
        meta.setStyleSheet(
            "QFrame { background:#3a2a18; border-radius:8px; }"
        )
        ml = QVBoxLayout(meta)
        ml.setContentsMargins(16, 12, 16, 12)
        ml.setSpacing(4)
        start_s = self._cand.get("start", "")[5:16].replace("T", " ")
        end_s = self._cand.get("end", "")[11:16]
        speakers_str = ", ".join(
            f"{k}:{v}" for k, v in (self._cand.get("speakers") or {}).items()
        )
        title_lbl = QLabel(f"📅 {start_s} — {end_s}")
        title_lbl.setStyleSheet(
            "color:#faf5e8; font-size:15px; font-weight:800; background:transparent;"
        )
        ml.addWidget(title_lbl)
        info_lbl = QLabel(
            f"{self._cand.get('duration_min', 0):.0f} 分钟 · "
            f"{self._cand.get('segments', 0)} 段 · "
            f"{self._cand.get('total_chars', 0)} 字 · "
            f"说话人 {speakers_str} · "
            f"会议关键词命中 {self._cand.get('kw_hits', 0)} 个"
        )
        info_lbl.setStyleSheet(
            "color:#ddc8a8; font-size:12px; background:transparent;"
        )
        info_lbl.setWordWrap(True)
        ml.addWidget(info_lbl)
        outer.addWidget(meta)

        # ── 中部:完整转写 ──
        hint_lbl = QLabel(
            "👇 完整转写 · 看完判断这是不是一场会议(扫一眼说话人交替模式 + 内容主题)"
        )
        hint_lbl.setStyleSheet("color:#5a564e; font-size:12px;")
        outer.addWidget(hint_lbl)

        self.preview_view = QPlainTextEdit()
        self.preview_view.setReadOnly(True)
        self.preview_view.setFrameShape(QPlainTextEdit.NoFrame)
        self.preview_view.setStyleSheet(
            "background:#ffffff; color:#1c1b18; padding:14px;"
            "border:1px solid rgba(28,27,24,0.10); border-radius:8px;"
            "font-size:13px; line-height:1.7;"
        )
        outer.addWidget(self.preview_view, 1)

        # ── 底部:三出口按钮 ──
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)

        self.btn_yes = QPushButton("✓ 是会议 · 生成纪要")
        self.btn_yes.setStyleSheet(
            "QPushButton { background:#1c1b18; color:#ffffff; "
            "border:none; border-radius:6px; padding:10px 16px; "
            "font-size:14px; font-weight:700; }"
            "QPushButton:hover { background:#cc785c; }"
        )
        self.btn_yes.setCursor(Qt.PointingHandCursor)
        self.btn_yes.clicked.connect(lambda: self.done(1))
        btn_row.addWidget(self.btn_yes, 3)

        self.btn_no = QPushButton("✗ 不是会议 · 不再提醒")
        self.btn_no.setStyleSheet(
            "QPushButton { background:transparent; color:#8e8a82; "
            "border:1px solid rgba(28,27,24,0.18); border-radius:6px; "
            "padding:10px 16px; font-size:13px; }"
            "QPushButton:hover { color:#b94a4a; border-color:#b94a4a; }"
        )
        self.btn_no.clicked.connect(lambda: self.done(2))
        btn_row.addWidget(self.btn_no, 2)

        self.btn_skip = QPushButton("⏭ 暂时跳过")
        self.btn_skip.setStyleSheet(self.btn_no.styleSheet())
        self.btn_skip.clicked.connect(lambda: self.done(3))
        btn_row.addWidget(self.btn_skip, 1)

        outer.addLayout(btn_row)

        # 加载完整转写
        self._load_preview()

    def _load_preview(self):
        """从 jsonl 加载这段时间内所有 segments,渲染到 preview_view。"""
        from common import transcript_path
        try:
            day = dt.date.fromisoformat(self._cand.get("day", ""))
        except Exception:
            self.preview_view.setPlainText("(无法解析日期)")
            return
        try:
            s_ts = dt.datetime.fromisoformat(self._cand.get("start", ""))
            e_ts = dt.datetime.fromisoformat(self._cand.get("end", ""))
        except Exception:
            self.preview_view.setPlainText("(无法解析起止时间)")
            return

        tp = transcript_path(day)
        if not tp.exists():
            self.preview_view.setPlainText(f"(找不到 {tp.name})")
            return

        import json as _json
        lines = []
        cur_speaker = None
        for raw in tp.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            try:
                r = _json.loads(raw)
            except Exception:
                continue
            ts_str = r.get("start")
            if not ts_str:
                continue
            try:
                ts = dt.datetime.fromisoformat(ts_str)
            except Exception:
                continue
            if ts < s_ts or ts > e_ts:
                continue
            text = (r.get("text") or "").strip()
            if not text:
                continue
            sp = r.get("speaker_name") or "?"
            tt = ts.strftime("%H:%M:%S")
            # 同一说话人连续说,把名字省略
            if sp == cur_speaker:
                lines.append(f"          {text}")
            else:
                lines.append(f"\n[{tt}] {sp}:")
                lines.append(f"          {text}")
                cur_speaker = sp
        if not lines:
            self.preview_view.setPlainText("(这段时间没有转写片段)")
            return
        self.preview_view.setPlainText("\n".join(lines).strip())
        # 滚到顶
        self.preview_view.verticalScrollBar().setValue(0)


class ContentRadarWorker(QObject):
    """后台只从当天本人语音中提取短视频选题。"""
    done = Signal(dict)
    failed = Signal(str)

    def run(self):
        try:
            import content_radar
            import datetime as _dt
            sv = content_radar.extract_from_day(_dt.date.today(), force=True)
            sv = sv if isinstance(sv, dict) else {}
            self.done.emit({
                "sv_added": sv.get("added", 0),
                "sv": sv,
            })
        except Exception as e:
            import traceback
            self.failed.emit(f"{e}\n{traceback.format_exc()[-500:]}")


class IngestUrlWorker(QObject):
    """后台线程跑 ingest_url，进度通过 Signal 推回主线程。"""
    progress = Signal(str, str)   # (stage, msg)
    finished = Signal(dict)       # 返回 {source, title, count, url}
    failed = Signal(str)          # 错误信息

    def __init__(self, url: str):
        super().__init__()
        self.url = url

    def run(self):
        # 关键:抓取走独立子进程,不在 launcher 进程内 import ingest_url。
        # 否则抖音链路(asyncio/aiohttp/下载器)会污染主进程 Qt 字体渲染 → 实时滚动中文变 ◆。
        try:
            import subprocess, json as _json
            proc = subprocess.Popen(
                _role_command("ingest-url", "--ipc", self.url),
                cwd=str(ROOT),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                creationflags=NO_WINDOW,
            )
            result = None
            err_msg = None
            for line in proc.stdout:
                line = line.rstrip("\r\n")
                if line.startswith("@@PROGRESS@@"):
                    parts = line.split("\t")
                    if len(parts) >= 3:
                        self.progress.emit(parts[1], parts[2])
                elif line.startswith("@@RESULT@@"):
                    try:
                        result = _json.loads(line.split("\t", 1)[1])
                    except Exception:
                        pass
                elif line.startswith("@@ERROR@@"):
                    err_msg = line.split("\t", 1)[1].replace("\\n", "\n")
            proc.wait()
            if result is not None:
                self.finished.emit(result)
            elif err_msg:
                self.failed.emit(err_msg)
            else:
                self.failed.emit(f"抓取子进程未返回结果(退出码 {proc.returncode})")
        except Exception as ex:
            import traceback
            tb = traceback.format_exc()
            self.failed.emit(f"{ex}\n\n--- 详细 ---\n{tb[-1200:]}")


class IngestUrlDialog(QDialog):
    """贴一个 URL → 自动识别平台 → 抓取入库。

    支持：抖音 / B 站 / 视频号 / 公众号。
    抓回的内容保存到输入档案，并在 AI 分析成功时生成独立爆款分析。
    外部内容不混入麦克风语音的实时滚动和今日日报。
    """
    def __init__(self, parent=None, default_url: str = ""):
        super().__init__(parent)
        self.setWindowTitle("贴链接抓内容 · 外部输入")
        self.resize(560, 360)
        self._parent_launcher = parent
        self._worker = None
        self._thread = None

        from PySide6.QtCore import QThread

        v = QVBoxLayout(self)
        v.setContentsMargins(20, 18, 20, 18)
        v.setSpacing(12)

        head = QLabel("贴 链 接 抓 内 容")
        head.setStyleSheet("color:#1c1b18; font-size:18px; font-weight:700;")
        v.addWidget(head)

        tip = QLabel(
            "粘贴一个 链接 → 自动识别平台 → 内容入库。\n"
            "支持：抖音 / B 站 / 视频号 / 公众号。\n"
            "抓回来会保存完整转写，并生成独立的「爆款分析」文档。"
        )
        tip.setStyleSheet("color:#5a564e; font-size:13px; line-height:1.6;")
        tip.setWordWrap(True)
        v.addWidget(tip)

        # URL 输入框
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("粘贴抖音/B站/视频号/公众号链接")
        if default_url:
            self.url_input.setText(default_url)
            self.url_input.selectAll()
        self.url_input.setStyleSheet(
            "background:#ffffff; color:#1c1b18; padding:10px 12px;"
            "border:1px solid rgba(28,27,24,0.12); border-radius:8px;"
            "font-family:'Consolas','Cascadia Mono'; font-size:13px;"
        )
        v.addWidget(self.url_input)

        # 平台识别 label
        self.platform_lbl = QLabel("")
        self.platform_lbl.setStyleSheet("color:#8e8a82; font-size:12px;")
        self.url_input.textChanged.connect(self._update_platform)
        v.addWidget(self.platform_lbl)
        self._update_platform()

        v.addSpacing(4)

        # 进度区
        self.progress_lbl = QLabel("")
        self.progress_lbl.setStyleSheet(
            "color:#1c1b18; font-size:13px; padding:8px 12px;"
            "background:rgba(28,27,24,0.04); border-radius:6px;"
        )
        self.progress_lbl.setWordWrap(True)
        self.progress_lbl.setMinimumHeight(60)
        v.addWidget(self.progress_lbl, 1)

        # 按钮行
        h = QHBoxLayout()
        h.addStretch(1)
        self.cancel_btn = QPushButton("关闭")
        self.cancel_btn.clicked.connect(self.reject)
        h.addWidget(self.cancel_btn)
        self.start_btn = QPushButton("开始抓取")
        self.start_btn.setObjectName("primaryBtn")
        self.start_btn.clicked.connect(self._on_start)
        h.addWidget(self.start_btn)
        v.addLayout(h)

    def _update_platform(self):
        url = self.url_input.text().strip()
        if not url:
            self.platform_lbl.setText("（贴链接后会自动识别）")
            return
        try:
            from ingest_url import detect_source
            src = detect_source(url)
        except Exception:
            src = "unknown"
        mp = {
            "douyin": "✓ 抖音 — 下载音频做本地转写",
            "bilibili": "✓ B 站 — 优先抓字幕,无字幕回退到音频转写",
            "wechat_channels": "✓ 视频号 — 官方 API 解析后下载，本地转写",
            "wechat": "✓ 公众号 — 抓正文文字",
            "unknown": "⚠ 不支持的链接(只支持抖音/B 站/视频号/公众号)",
        }
        color = "#0e8a4f" if src != "unknown" else "#b94a4a"
        self.platform_lbl.setText(mp[src])
        self.platform_lbl.setStyleSheet(f"color:{color}; font-size:12px;")

    def _on_start(self):
        url = self.url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "提示", "请先粘贴一个链接")
            return
        try:
            from ingest_url import detect_source
            src = detect_source(url)
        except Exception:
            src = "unknown"
        if src == "unknown":
            QMessageBox.warning(self, "提示", "目前只支持:抖音 / B 站 / 视频号 / 公众号")
            return

        # 锁界面
        self.url_input.setEnabled(False)
        self.start_btn.setEnabled(False)
        self.start_btn.setText("抓取中...")
        self.progress_lbl.setText("启动后台抓取...")

        # 起后台线程
        from PySide6.QtCore import QThread
        self._thread = QThread(self)
        self._worker = IngestUrlWorker(url)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        # 清理
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        # 抓取期间暂停录音:防止你看/放这个视频时,外放声音被麦克风录进实时滚动。
        # 写 "ingest-auto" 标记区分「自动暂停」和用户手动暂停(空文件),
        # 这样就算程序崩了,下次启动也能识别并清掉残留(不会一直卡在暂停)。
        try:
            fp = pause_flag()
            if not fp.exists():
                fp.write_text("ingest-auto", encoding="utf-8")
        except Exception:
            pass
        self._thread.start()

    def _resume_recording(self):
        """抓取结束恢复录音。只清「ingest-auto」自动标记,不动用户手动按的暂停。"""
        try:
            fp = pause_flag()
            if fp.exists():
                try:
                    mark = fp.read_text(encoding="utf-8").strip()
                except Exception:
                    mark = ""
                if mark == "ingest-auto":
                    fp.unlink()
        except Exception:
            pass

    def _on_progress(self, stage: str, msg: str):
        prefix = {
            "info": "📋",
            "subtitle": "📝",
            "audio": "⬇",
            "api": "🔗",
            "download": "⬇",
            "merge": "🎞",
            "asr": "🎙",
            "fetch": "🌐",
            "parse": "✂",
            "done": "✓",
        }.get(stage, "•")
        self.progress_lbl.setText(f"{prefix} {msg}")
        if self._parent_launcher:
            try:
                self._parent_launcher.bottom_status.setText(f"贴链接 · {msg}")
            except Exception:
                pass

    def _on_finished(self, result: dict):
        self._resume_recording()
        src_cn = {
            "douyin": "抖音", "bilibili": "B 站", "wechat": "公众号",
            "wechat_channels": "视频号",
        }.get(
            result.get("source", ""), result.get("source", "")
        )
        n = result.get("count", 0)
        title = result.get("title", "")
        archive_saved = bool(result.get("wiki_anchor"))
        baokuan_path = str(result.get("baokuan_path") or "").strip()
        saved = []
        if archive_saved:
            saved.append("输入档案")
        if baokuan_path:
            saved.append("爆款分析")
        save_text = " + ".join(saved) if saved else "转写结果"
        self.progress_lbl.setText(
            f"✓ 完成 — {src_cn} · {title[:40]} · 转写 {n} 段\n"
            f"已保存：{save_text}。"
        )
        if self._parent_launcher:
            try:
                self._parent_launcher.bottom_status.setText(
                    f"外部内容已保存 · {src_cn} · {n} 段 · {save_text}"
                )
                # 让主面板刷一下,看到新条目
                if hasattr(self._parent_launcher, "_reload"):
                    self._parent_launcher._reload()
            except Exception:
                pass
        # 重置 UI 让用户可以连贴
        self.url_input.setEnabled(True)
        self.url_input.clear()
        self.start_btn.setEnabled(True)
        self.start_btn.setText("再来一个")
        self._update_platform()

    def _on_failed(self, err: str):
        self._resume_recording()
        self.progress_lbl.setText(f"✗ 失败:{err.splitlines()[0][:200]}")
        QMessageBox.warning(self, "抓取失败", err)
        self.url_input.setEnabled(True)
        self.start_btn.setEnabled(True)
        self.start_btn.setText("开始抓取")

    def _teardown_or_confirm(self) -> bool:
        """关窗前安全处理后台线程。返回 False=用户取消关闭。
        线程还在跑时:确认后把线程「转交」主窗口接管(断开本窗口回调+reparent),
        让它后台抓完照常入库 — 否则窗口销毁会带走运行中的线程,Qt 直接终止整个进程(闪退)。"""
        t, w = self._thread, self._worker
        if t is not None and t.isRunning():
            r = QMessageBox.question(
                self, "确认",
                "抓取还在进行,确定关闭吗?\n(后台会继续抓完并照常入库,完成后状态栏提示)"
            )
            if r != QMessageBox.Yes:
                return False
            if w is not None:
                for sig, slot in ((w.progress, self._on_progress),
                                  (w.finished, self._on_finished),
                                  (w.failed, self._on_failed)):
                    try:
                        sig.disconnect(slot)
                    except Exception:
                        pass
            mw = self._parent_launcher
            try:
                if mw is not None:
                    t.setParent(mw)   # 窗口销毁不再带走运行中的线程(根治闪退)
                    if w is not None:
                        w.finished.connect(mw._on_ingest_bg_done)
                        w.failed.connect(mw._on_ingest_bg_failed)
                t.finished.connect(t.deleteLater)
            except Exception:
                pass
            self._thread = None
            self._worker = None
        self._resume_recording()
        return True

    def reject(self):
        # 「关闭」按钮和 Esc 走 reject(不经过 closeEvent)— 之前闪退就是这里没守卫
        if self._teardown_or_confirm():
            super().reject()

    def closeEvent(self, e):
        if self._teardown_or_confirm():
            e.accept()
        else:
            e.ignore()


# ============================================================
# 讨论闭环 · 6 个思考框架 + DeepSeek 多轮对话 + 落地到第二大脑
# ============================================================

# (key, 中文显示名)
# 注意顺序:第 0 个是"自动路由"兜底用的默认值(socratic)
DISCUSS_FRAMEWORKS = [
    ("socratic",    "🤔 苏格拉底追问"),
    ("business",    "🩺 商业问诊 (dbs)"),
    ("action",      "🪞 执行力诊断 (阿德勒)"),
    ("deconstruct", "🔍 概念拆解 (维特根斯坦)"),
    ("slow",        "🐢 慢就是快"),
    ("benchmark",   "🎯 对标分析 (五重过滤)"),
]

# 路由关键词:命中第一个匹配的 framework
# 顺序很重要 — 越窄越具体的放前面;business 比 deconstruct 优先,
# 因为商业问题里也常有"是 A 还是 B"句式但本质是商业判断
FRAMEWORK_ROUTING = [
    # (framework_key, [关键词])
    ("action",      ["拖延", "为什么我不", "想做但没做", "卡住", "逃避",
                    "知道但做不到", "明知道", "总是不"]),
    ("benchmark",   ["对标", "模仿", "学谁", "学习谁", "抄谁", "谁在做",
                    "找参考"]),
    ("slow",        ["太快", "捷径", "更慢", "复利", "积累资产", "速成",
                    "走捷径", "省时间"]),
    ("business",    ["赚钱", "收入", "年入", "千万", "万元", "产品", "商业模式",
                    "卡点", "客户", "定价", "转化", "流量", "粉丝", "客单",
                    "项目", "业务", "市场", "运营"]),
    ("deconstruct", ["这个词", "什么意思", "怎么定义", "如何定义", "概念",
                    "本质是什么", "究竟是什么"]),
]


def load_framework_prompt(key: str) -> str:
    """读 prompts/discuss/<key>.md。读不到走 socratic 兜底。"""
    p = RESOURCE_ROOT / "prompts" / "discuss" / f"{key}.md"
    if not p.exists():
        p = RESOURCE_ROOT / "prompts" / "discuss" / "socratic.md"
    if p.exists():
        try:
            return p.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    # 极端兜底
    return (
        "你是用户的思考伙伴。每次回复短：复述用户刚说的 + 一个最关键的反问。"
        "不直呼用户名字，不客套，全程中文。"
    )


def pick_framework(question: dict) -> str:
    """根据 question 内容关键词匹配,返回 framework key。默认 socratic。"""
    text_blob = " ".join([
        str(question.get("title", "")),
        str(question.get("context", "")),
        str(question.get("why_matters", "")),
        " ".join(str(q) for q in (question.get("related_quotes") or [])),
    ])
    for key, keywords in FRAMEWORK_ROUTING:
        for kw in keywords:
            if kw in text_blob:
                return key
    return "socratic"


class DiscussWorker(QObject):
    """后台线程跑 DeepSeek chat 多轮对话,把回复 emit 回主线程。"""
    chunk = Signal(str)          # 流式增量(目前用整段回复一次性 emit)
    done = Signal(str)           # 完整回复
    failed = Signal(str)         # 错误信息

    def __init__(self, messages: list[dict]):
        super().__init__()
        self.messages = messages

    def run(self):
        try:
            import os as _os
            from ai_gateway import OpenAI
            api_key = _os.environ.get("DEEPSEEK_API_KEY")
            if not api_key:
                self.failed.emit("DEEPSEEK_API_KEY 未设置")
                return
            client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
            resp = client.chat.completions.create(
                model="deepseek-v4-flash",
                messages=self.messages,
                max_tokens=1500,
                temperature=0.5,
            )
            reply = (resp.choices[0].message.content or "").strip()
            self.done.emit(reply)
        except Exception as ex:
            import traceback
            self.failed.emit(f"{ex}\n\n{traceback.format_exc()[-800:]}")


class DiscussDialog(QDialog):
    """对一个「待讨论问题」做 DeepSeek 多轮对话,讨论完可保存到第二大脑。

    左侧:问题上下文(原话片段 / 为什么重要)
    右侧:多轮聊天(类似 ChatGPT)
    底部:[保存到第二大脑] [关闭]

    保存:把整段对话摘要 + 最终结论追加到「第二大脑/讨论档案.md」
    """
    def __init__(self, parent=None, question: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"讨论 · {question.get('title', '') if question else ''}")
        self.resize(960, 640)
        self._parent_launcher = parent
        self._question = question or {}
        self._qid = self._question.get("id", "")
        self._messages: list[dict] = []
        self._worker = None
        self._thread = None
        # 跟踪是否已经走过一个出口(resolved/dismissed),用于关闭时决定是否提醒暂存
        self._exit_taken = False

        from PySide6.QtCore import QThread

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── 左侧:问题上下文 ──
        ctx_panel = QFrame()
        ctx_panel.setStyleSheet("background:rgba(28,27,24,0.04);")
        ctx_panel.setFixedWidth(360)
        cv = QVBoxLayout(ctx_panel)
        cv.setContentsMargins(18, 18, 14, 18)
        cv.setSpacing(10)

        head = QLabel("待 讨 论")
        head.setStyleSheet("color:#3a3833; font-size:12px; letter-spacing:4px; font-weight:700;")
        cv.addWidget(head)

        title = QLabel(self._question.get("title", ""))
        title.setWordWrap(True)
        title.setStyleSheet("color:#1c1b18; font-size:16px; font-weight:700; line-height:1.4;")
        cv.addWidget(title)

        why = self._question.get("why_matters", "")
        if why:
            why_lbl = QLabel(f"💡 为什么重要\n\n{why}")
            why_lbl.setWordWrap(True)
            why_lbl.setStyleSheet("color:#3a3833; font-size:12px; line-height:1.6; padding:10px 0;")
            cv.addWidget(why_lbl)

        ctx = self._question.get("context", "")
        if ctx:
            ctx_lbl = QLabel(f"📝 昨天的上下文\n\n{ctx}")
            ctx_lbl.setWordWrap(True)
            ctx_lbl.setStyleSheet("color:#3a3833; font-size:12px; line-height:1.6; padding:6px 0;")
            cv.addWidget(ctx_lbl)

        quotes = self._question.get("related_quotes") or []
        if quotes:
            quotes_head = QLabel("🎙 相 关 原 话")
            quotes_head.setStyleSheet(
                "color:#3a3833; font-size:11px; letter-spacing:3px; "
                "font-weight:700; padding:8px 0 4px 0;"
            )
            cv.addWidget(quotes_head)
            for q in quotes[:5]:
                ql = QLabel(f"「{q}」")
                ql.setWordWrap(True)
                ql.setStyleSheet(
                    "color:#5a564e; font-size:12px; padding:6px 8px; "
                    "background:rgba(28,27,24,0.05); border-radius:4px; line-height:1.6;"
                )
                cv.addWidget(ql)

        cv.addStretch(1)
        outer.addWidget(ctx_panel)

        # ── 右侧:聊天 ──
        chat_panel = QFrame()
        cp = QVBoxLayout(chat_panel)
        cp.setContentsMargins(18, 14, 18, 14)
        cp.setSpacing(10)

        ch = QHBoxLayout()
        chat_title = QLabel("多 轮 讨 论 · DeepSeek")
        chat_title.setStyleSheet("color:#3a3833; font-size:12px; letter-spacing:3px; font-weight:700;")
        ch.addWidget(chat_title, 1)

        # framework 切换下拉框
        self.framework_combo = QComboBox()
        for key, label in DISCUSS_FRAMEWORKS:
            self.framework_combo.addItem(label, userData=key)
        self.framework_combo.setStyleSheet(
            "QComboBox { background:#ffffff; color:#1c1b18; padding:4px 10px;"
            "border:1px solid rgba(28,27,24,0.18); border-radius:6px; font-size:12px; }"
            "QComboBox:hover { border-color:#cc785c; }"
        )
        self.framework_combo.setToolTip("切换思考框架 — 切换后会重启对话")
        self.framework_combo.setMinimumWidth(160)
        self.framework_combo.currentIndexChanged.connect(self._on_framework_changed)
        ch.addWidget(self.framework_combo)
        cp.addLayout(ch)

        # 聊天记录区
        self.chat_view = QPlainTextEdit()
        self.chat_view.setReadOnly(True)
        self.chat_view.setFrameShape(QPlainTextEdit.NoFrame)
        self.chat_view.setStyleSheet(
            "background:#ffffff; color:#1c1b18; padding:12px;"
            "border:1px solid rgba(28,27,24,0.10); border-radius:8px;"
            "font-size:14px; line-height:1.7;"
        )
        cp.addWidget(self.chat_view, 1)

        # 输入区
        input_row = QHBoxLayout()
        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText("跟 DeepSeek 聊聊这个问题(回车发送)")
        self.input_edit.setStyleSheet(
            "background:#ffffff; color:#1c1b18; padding:10px 12px;"
            "border:1px solid rgba(28,27,24,0.12); border-radius:8px;"
            "font-size:14px;"
        )
        self.input_edit.returnPressed.connect(self._on_send)
        input_row.addWidget(self.input_edit, 1)
        self.btn_send = QPushButton("发送")
        self.btn_send.setObjectName("primaryBtn")
        self.btn_send.clicked.connect(self._on_send)
        input_row.addWidget(self.btn_send)
        cp.addLayout(input_row)

        # ── 三出口按钮:想清楚了 / 暂存继续 / 删掉 ──
        exit_row = QHBoxLayout()
        exit_row.setSpacing(10)

        self.btn_resolve = QPushButton("✓ 想清楚了 · 保存结论")
        self.btn_resolve.setObjectName("primaryBtn")
        self.btn_resolve.setToolTip(
            "把结论 + 完整对话写入 第二大脑/讨论档案.md\n"
            "这条问题从待讨论列表移除"
        )
        self.btn_resolve.setEnabled(False)   # 至少有一轮对话才能保存
        self.btn_resolve.clicked.connect(self._on_resolve)
        exit_row.addWidget(self.btn_resolve, 2)

        self.btn_park = QPushButton("⚡ 暂存 · 下次接着聊")
        self.btn_park.setStyleSheet(
            "QPushButton { background:#f4ead8; color:#5a564e; "
            "border:1px solid rgba(204,120,92,0.30); border-radius:6px; "
            "padding:8px 12px; font-size:13px; font-weight:600; }"
            "QPushButton:hover { background:#ecd9b8; color:#1c1b18; }"
            "QPushButton:disabled { background:#f4ead8; color:#b8b3a8; }"
        )
        self.btn_park.setToolTip(
            "保存当前对话为草稿,下次再点这个问题可继续\n"
            "问题继续留在待讨论列表"
        )
        self.btn_park.setEnabled(False)
        self.btn_park.clicked.connect(self._on_park)
        exit_row.addWidget(self.btn_park, 2)

        self.btn_dismiss = QPushButton("✗ 删掉")
        self.btn_dismiss.setStyleSheet(
            "QPushButton { background:transparent; color:#8e8a82; "
            "border:1px solid rgba(28,27,24,0.18); border-radius:6px; "
            "padding:8px 12px; font-size:13px; }"
            "QPushButton:hover { color:#b94a4a; border-color:#b94a4a; }"
        )
        self.btn_dismiss.setToolTip(
            "这个问题没意义/已不重要 → 从待讨论列表移除\n"
            "(会留在 archive 里,不会真删)"
        )
        self.btn_dismiss.clicked.connect(self._on_dismiss)
        exit_row.addWidget(self.btn_dismiss, 1)

        cp.addLayout(exit_row)

        outer.addWidget(chat_panel, 1)

        # 禁掉所有按钮的「回车默认触发」(同工坊:防回车误触发关窗按钮导致崩)
        for _b in self.findChildren(QPushButton):
            _b.setAutoDefault(False)
            _b.setDefault(False)

        # ── 恢复草稿 or 启动新对话 ──
        draft = self._question.get("draft")
        if draft and draft.get("messages"):
            # 有暂存草稿 → 直接恢复,不调 LLM
            self._restore_from_draft(draft)
        else:
            # 自动路由 framework + 初始化对话
            picked = pick_framework(self._question)
            idx = next((i for i, (k, _) in enumerate(DISCUSS_FRAMEWORKS) if k == picked), 0)
            if idx == 0:
                self._init_conversation()
            else:
                self.framework_combo.setCurrentIndex(idx)

    def _restore_from_draft(self, draft: dict):
        """从 question.draft 恢复对话(不调 LLM)。"""
        fw_key = draft.get("framework", "socratic")
        messages = draft.get("messages") or []
        # 把 combo 对到对应 framework — 用 blockSignals 避免触发 _on_framework_changed
        idx = next((i for i, (k, _) in enumerate(DISCUSS_FRAMEWORKS) if k == fw_key), 0)
        self.framework_combo.blockSignals(True)
        self.framework_combo.setCurrentIndex(idx)
        self.framework_combo.blockSignals(False)
        fw_label = self.framework_combo.currentText()
        self._messages = messages
        # 回放对话到 chat_view
        title = self._question.get("title", "")
        self.chat_view.appendPlainText(
            f"━ [{fw_label}] 关于:{title}\n"
            f"━ 已恢复 {draft.get('saved_at', '')} 的草稿对话\n"
        )
        for m in messages:
            if m["role"] == "system":
                continue
            speaker = "我" if m["role"] == "user" else "DeepSeek"
            self.chat_view.appendPlainText(f"\n{speaker}:{m['content']}\n")
        # 至少有一轮 → 允许保存
        if any(m["role"] == "assistant" for m in messages):
            self.btn_resolve.setEnabled(True)
            self.btn_park.setEnabled(True)
        # 滚到底
        sb = self.chat_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _on_framework_changed(self, idx: int):
        """用户手动切了框架 -> 清空对话,从头开始。"""
        self.chat_view.clear()
        self._messages = []
        self.btn_resolve.setEnabled(False)
        self.btn_park.setEnabled(False)
        self._init_conversation()

    def _init_conversation(self):
        """构造 system prompt = framework内核 + 本次具体问题上下文。"""
        title = self._question.get("title", "")
        ctx = self._question.get("context", "")
        why = self._question.get("why_matters", "")
        quotes = self._question.get("related_quotes") or []
        quotes_text = "\n".join(f"- 「{q}」" for q in quotes) if quotes else "(无)"

        # 读 framework prompt
        fw_key = self.framework_combo.currentData() if hasattr(self, "framework_combo") else "socratic"
        framework_text = load_framework_prompt(fw_key)
        fw_label = self.framework_combo.currentText() if hasattr(self, "framework_combo") else ""

        # framework + 本次问题上下文
        system = (
            framework_text + "\n\n"
            "─── 本次讨论的具体问题 ───\n\n"
            f"【问题】{title}\n\n"
            f"【为什么需要想清楚】{why}\n\n"
            f"【昨天/最近的上下文】{ctx}\n\n"
            f"【相关原话】\n{quotes_text}\n"
        )
        self._messages = [{"role": "system", "content": system}]

        # 顶部显示框架名 + 问题
        self.chat_view.appendPlainText(f"━ [{fw_label}] 关于:{title}\n")
        self._send_to_llm(initial=True)

    def _on_send(self):
        # 防重入:正在生成时忽略回车/连点,避免起多个 QThread 抢同一窗口导致崩溃
        t = getattr(self, "_thread", None)
        if t is not None and t.isRunning():
            return
        text = self.input_edit.text().strip()
        if not text:
            return
        self.input_edit.clear()
        self.chat_view.appendPlainText(f"\n我：{text}\n")
        self._messages.append({"role": "user", "content": text})
        self._send_to_llm()

    def _send_to_llm(self, initial: bool = False):
        if initial:
            # 第一次让模型开口
            self._messages.append({
                "role": "user",
                "content": "我想跟你把这个问题想清楚,先帮我复述一下你理解的问题核心,然后提一两个最关键的反问。"
            })

        self.input_edit.setEnabled(False)
        self.btn_send.setEnabled(False)
        self.btn_send.setText("思考中…")
        self.chat_view.appendPlainText("\nDeepSeek 思考中…")

        from PySide6.QtCore import QThread
        self._thread = QThread(self)
        self._worker = DiscussWorker(list(self._messages))
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.done.connect(self._on_llm_done)
        self._worker.failed.connect(self._on_llm_failed)
        self._worker.done.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.start()

    def _on_llm_done(self, reply: str):
        # 替换"思考中…"为真实回复
        text = self.chat_view.toPlainText()
        if text.endswith("DeepSeek 思考中…"):
            text = text[: -len("DeepSeek 思考中…")]
            self.chat_view.setPlainText(text + f"DeepSeek:{reply}\n")
        else:
            self.chat_view.appendPlainText(f"\nDeepSeek:{reply}\n")
        self._messages.append({"role": "assistant", "content": reply})
        # 至少有一轮 -> 允许"保存结论"和"暂存"
        if any(m["role"] == "assistant" for m in self._messages):
            self.btn_resolve.setEnabled(True)
            self.btn_park.setEnabled(True)
        self.input_edit.setEnabled(True)
        self.btn_send.setEnabled(True)
        self.btn_send.setText("发送")
        self.input_edit.setFocus()
        sb = self.chat_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _on_llm_failed(self, err: str):
        self.chat_view.appendPlainText(f"\n[error] {err.splitlines()[0]}")
        self.input_edit.setEnabled(True)
        self.btn_send.setEnabled(True)
        self.btn_send.setText("发送")
        QMessageBox.warning(self, "调用失败", err[:1200])

    # ===========================================
    # 三出口
    # ===========================================
    def _current_framework_key(self) -> str:
        return self.framework_combo.currentData() or "socratic"

    def _on_resolve(self):
        """[✓ 想清楚了 · 保存结论]
        弹一个对话框让用户写一句话结论(必填) → 写入第二大脑 + 标记 resolved。
        """
        # 让用户写一句话结论
        from PySide6.QtWidgets import QInputDialog
        conclusion, ok = QInputDialog.getMultiLineText(
            self, "保存结论 · 第二大脑/讨论档案.md",
            "用一句话写下你刚刚想清楚的结论(必填):\n\n"
            "好的结论形如「我决定 X,因为 Y」或「不做 X,因为 Z」",
            ""
        )
        if not ok or not conclusion.strip():
            return
        conclusion = conclusion.strip()

        # 写入第二大脑/讨论档案.md
        try:
            from daily_summary import _wiki_path
            p = _wiki_path("讨论档案.md")
        except Exception:
            p = None
        if p is None:
            QMessageBox.warning(self, "未配置 Obsidian",
                "config.toml 里没有 [obsidian].vault,无法保存到第二大脑")
            return

        title = self._question.get("title", "")
        fw_key = self._current_framework_key()
        fw_label = self.framework_combo.currentText()
        src_date = self._question.get("source_date", "")
        ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

        chat_lines = []
        for m in self._messages:
            if m["role"] == "system":
                continue
            speaker = "我" if m["role"] == "user" else "DeepSeek"
            content = m["content"].strip()
            if not content:
                continue
            chat_lines.append(f"**{speaker}**:\n\n{content}\n")

        section = (
            f"\n## ✓ {title}\n"
            f"> 来源:{src_date} 复盘 · 讨论于 {ts} · 框架:{fw_label}\n\n"
            f"### 🎯 结论\n\n{conclusion}\n\n"
            f"### 完整对话\n\n"
            + "\n".join(chat_lines)
            + "\n---\n"
        )
        if not p.exists():
            head = (
                "# 讨论档案\n\n"
                "> 从「待讨论」清单里展开的多轮讨论 + 最终结论。\n"
                "> 每节是一个想清楚的问题 · 结论是用户自己的判断。\n"
                "> 以后写文章/做决策时,这份档案是已验证认知库。\n\n"
                "---\n"
            )
            p.write_text(head + section, encoding="utf-8")
        else:
            p.write_text(p.read_text(encoding="utf-8") + section, encoding="utf-8")

        # 标 resolved
        try:
            import open_questions as oq
            oq.mark_resolved(self._qid, conclusion=conclusion,
                            framework=fw_key, messages=self._messages)
        except Exception as e:
            QMessageBox.warning(self, "标记失败",
                f"已写入讨论档案,但更新 open_questions 池失败:{e}")

        # 卡帕西 sync:把结论按 entities/concepts 同步到对应实体页(异步,不阻塞)
        wiki_msg = ""
        try:
            import wiki_appender
            r = wiki_appender.update_from_discuss(
                self._question, conclusion, fw_key, self._messages
            )
            if r["entities"] or r["concepts"]:
                wiki_msg = f"\n\n📚 同步到 {r['entities']} 个实体 + {r['concepts']} 个概念页"
        except Exception as e:
            wiki_msg = f"\n\n(实体/概念同步失败:{e})"

        self._detach_running_thread()
        self._exit_taken = True
        QMessageBox.information(
            self, "已保存",
            f"✓ 结论已写入 第二大脑/讨论档案.md\n"
            f"这个问题已从待讨论列表移除{wiki_msg}"
        )
        self.accept()

    def _on_park(self):
        """[⚡ 暂存] 保存当前对话为草稿,下次再点同一个问题可继续。"""
        if not self._qid:
            QMessageBox.warning(self, "无法暂存",
                "这个问题没有 id(可能是测试数据),无法暂存")
            return
        try:
            import open_questions as oq
            ok = oq.save_draft(self._qid, self._current_framework_key(), self._messages)
            if ok:
                self._detach_running_thread()
                self._exit_taken = True
                QMessageBox.information(self, "已暂存",
                    "⚡ 对话已保存为草稿\n\n下次再点这个问题会自动恢复")
                self.accept()
            else:
                QMessageBox.warning(self, "暂存失败",
                    "问题可能已被 resolved/dismissed,刷新列表试试")
        except Exception as e:
            QMessageBox.warning(self, "暂存失败", str(e))

    def _on_dismiss(self):
        """[✗ 删掉] 标记 dismissed。不写第二大脑(只是觉得没意义)。"""
        r = QMessageBox.question(
            self, "确认删掉",
            "这个问题没意义/已不重要?\n\n"
            "→ 从待讨论列表移除\n"
            "→ 不写入第二大脑\n"
            "→ 会留在 archive 里(不会真删)"
        )
        if r != QMessageBox.Yes:
            return
        try:
            import open_questions as oq
            oq.mark_dismissed(self._qid, reason="user_dismissed_from_dialog")
        except Exception as e:
            QMessageBox.warning(self, "标记失败", str(e))
            return
        self._detach_running_thread()   # 先安全断开后台线程,避免崩
        self._exit_taken = True
        self.accept()

    def _detach_running_thread(self):
        """关闭/出口前,若 DeepSeek 线程还在跑:断开它对本窗口 UI 的回调(否则线程结束时
        访问已销毁控件→崩),并 reparent 到主窗口让它后台跑完,不随 dialog 销毁而崩。"""
        t = getattr(self, "_thread", None)
        w = getattr(self, "_worker", None)
        if t is not None and t.isRunning():
            if w is not None:
                try: w.done.disconnect(self._on_llm_done)
                except Exception: pass
                try: w.failed.disconnect(self._on_llm_failed)
                except Exception: pass
            try:
                mw = self.parent()
                if mw is not None:
                    t.setParent(mw)
                t.finished.connect(t.deleteLater)
            except Exception:
                pass
        self._thread = None
        self._worker = None

    def closeEvent(self, e):
        # DeepSeek 还在思考:安全断开线程(线程后台跑完,不再访问已销毁控件),不阻塞关闭
        self._detach_running_thread()
        # 有未保存的对话,提醒选出口
        if (not self._exit_taken
                and any(m["role"] == "assistant" for m in self._messages)
                and self._qid):
            r = QMessageBox.question(
                self, "未保存",
                "你有未保存的对话。怎么处理?\n\n"
                "• 是 → 暂存草稿,下次接着聊\n"
                "• 否 → 丢弃这次对话(问题继续留在待讨论)\n"
                "• 取消 → 不关闭对话框",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                QMessageBox.Yes
            )
            if r == QMessageBox.Cancel:
                e.ignore()
                return
            if r == QMessageBox.Yes:
                try:
                    import open_questions as oq
                    oq.save_draft(self._qid, self._current_framework_key(),
                                  self._messages)
                except Exception:
                    pass
        e.accept()


# ============================================================
# 内容工坊 · 选题 → 短视频口播稿
# ============================================================
class ContentStudioWorker(QObject):
    """内容工坊 LLM 调用(多轮)。内容生产优先 Claude,fallback DeepSeek。"""
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, system: str, messages: list, max_tokens: int = 2500):
        super().__init__()
        self.system = system
        self.messages = messages
        self.max_tokens = max_tokens

    def run(self):
        if not feature_enabled("deep_discussion"):
            self.failed.emit("商业 V1 暂不提供深度讨论")
            return
        try:
            import content_radar
            reply = content_radar.studio_chat(self.system, self.messages,
                                              max_tokens=self.max_tokens, temperature=0.6)
            self.done.emit(reply)
        except Exception as e:
            import traceback
            raw = f"{e}\n{traceback.format_exc()[-600:]}"
            if "timed out" in raw.lower() or "APITimeoutError" in raw:
                self.failed.emit(
                    "接口请求超时。\n\n"
                    "这通常发生在公众号全文/四层自检这种长输出阶段：内容太长，中转接口返回超过等待时间。\n\n"
                    "我已经把内容工坊的默认等待时间提高到 600 秒。"
                    "如果仍然超时，可以再点一次生成，或先让 AI 写短一点的版本。"
                )
            else:
                self.failed.emit(raw)


class ContentStudioDialog(QDialog):
    """内容工坊 — 把选题协作成型。
    短视频两阶段:angle(角度卡片) → script(口播稿)。
    三出口:✓ 定稿(落地+mark_done) / ⚡ 暂存(save_draft) / ✗ 放弃(dismiss)。
    """
    def __init__(self, parent=None, idea: dict = None):
        super().__init__(parent)
        self._idea = idea or {}
        self._iid = self._idea.get("id", "")
        self._fmt = self._idea.get("format", "shortvideo")
        self._stage = self._first_stage()
        self._messages = []
        self._worker = None
        self._thread = None
        self._exit_taken = False

        self.setWindowTitle(f"内容工坊 · {self._idea.get('title','')[:30]}")
        self.resize(980, 680)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 左侧:选题上下文
        ctx = QFrame()
        ctx.setStyleSheet("background:rgba(28,27,24,0.04);")
        ctx.setFixedWidth(340)
        cv = QVBoxLayout(ctx)
        cv.setContentsMargins(18, 18, 14, 18)
        cv.setSpacing(10)
        if self._idea.get("origin") == "external":
            badge = "🔁 二创 · 短视频"
        else:
            badge = "📱 短视频" if self._fmt == "shortvideo" else "📄 公众号"
        bl = QLabel(badge)
        bl.setStyleSheet("color:#cc785c; font-size:12px; font-weight:700; letter-spacing:2px;")
        cv.addWidget(bl)
        tl = QLabel(self._idea.get("title", ""))
        tl.setWordWrap(True)
        tl.setStyleSheet("color:#1c1b18; font-size:16px; font-weight:700; line-height:1.4;")
        cv.addWidget(tl)
        hook = self._idea.get("hook", "")
        if hook:
            hl = QLabel(f"💡 抓手\n\n{hook}")
            hl.setWordWrap(True)
            hl.setStyleSheet("color:#3a3833; font-size:12px; line-height:1.6; padding:8px 0;")
            cv.addWidget(hl)
        quotes = self._idea.get("source_quotes") or []
        if quotes:
            qh = QLabel("🎙 原话依据")
            qh.setStyleSheet("color:#3a3833; font-size:11px; letter-spacing:3px; "
                             "font-weight:700; padding:8px 0 4px 0;")
            cv.addWidget(qh)
            for q in quotes[:4]:
                ql = QLabel(f"「{q}」")
                ql.setWordWrap(True)
                ql.setStyleSheet("color:#5a564e; font-size:12px; padding:6px 8px; "
                                 "background:rgba(28,27,24,0.05); border-radius:4px; line-height:1.6;")
                cv.addWidget(ql)
        cv.addStretch(1)
        outer.addWidget(ctx)

        # 右侧:对话
        chat = QFrame()
        cp = QVBoxLayout(chat)
        cp.setContentsMargins(18, 14, 18, 14)
        cp.setSpacing(10)

        hdr = QHBoxLayout()
        self.stage_lbl = QLabel("① 角度卡片")
        self.stage_lbl.setStyleSheet("color:#3a3833; font-size:12px; letter-spacing:2px; font-weight:700;")
        hdr.addWidget(self.stage_lbl, 1)
        self.btn_advance = QPushButton("→ 生成口播稿")
        self.btn_advance.setStyleSheet(
            "QPushButton { background:#f4ead8; color:#5a564e; border:1px solid rgba(204,120,92,0.3); "
            "border-radius:6px; padding:6px 12px; font-size:12px; font-weight:600; }"
            "QPushButton:hover { background:#ecd9b8; color:#1c1b18; }"
            "QPushButton:disabled { color:#b8b3a8; }")
        self.btn_advance.setToolTip("推进到下一阶段")
        self.btn_advance.setEnabled(False)
        self.btn_advance.clicked.connect(self._on_advance)
        hdr.addWidget(self.btn_advance)
        cp.addLayout(hdr)

        self.chat_view = QPlainTextEdit()
        self.chat_view.setReadOnly(True)
        self.chat_view.setFrameShape(QPlainTextEdit.NoFrame)
        self.chat_view.setStyleSheet("background:#ffffff; color:#1c1b18; padding:12px; "
            "border:1px solid rgba(28,27,24,0.10); border-radius:8px; font-size:14px; line-height:1.7;")
        cp.addWidget(self.chat_view, 1)

        # ▶ 开始生成(手动触发,默认隐藏):打开工坊不自动烧 token,用户点了才生成
        self.btn_kickoff = QPushButton("▶ 开始生成")
        self.btn_kickoff.setObjectName("primaryBtn")
        self.btn_kickoff.setVisible(False)
        self.btn_kickoff.clicked.connect(self._do_kickoff)
        cp.addWidget(self.btn_kickoff)

        input_row = QHBoxLayout()
        self.input_edit = QLineEdit()
        _ph = ("回答上面的红色问题 / 补充真实细节 / 让 AI 调整" if self._fmt == "article"
               else "让 AI 调整(立场再硬点 / 换个钩子 / 案例2换成我说的XX)")
        self.input_edit.setPlaceholderText(_ph)
        self.input_edit.setStyleSheet("background:#ffffff; color:#1c1b18; padding:10px 12px; "
            "border:1px solid rgba(28,27,24,0.12); border-radius:8px; font-size:14px;")
        self.input_edit.returnPressed.connect(self._on_send)
        input_row.addWidget(self.input_edit, 1)
        self.btn_send = QPushButton("发送")
        self.btn_send.setObjectName("primaryBtn")
        self.btn_send.clicked.connect(self._on_send)
        input_row.addWidget(self.btn_send)
        cp.addLayout(input_row)

        exit_row = QHBoxLayout()
        exit_row.setSpacing(10)
        self.btn_finalize = QPushButton("✓ 定稿 · 保存到内容产出")
        self.btn_finalize.setObjectName("primaryBtn")
        self.btn_finalize.setEnabled(False)
        self.btn_finalize.clicked.connect(self._on_finalize)
        exit_row.addWidget(self.btn_finalize, 3)
        self.btn_park = QPushButton("⚡ 暂存")
        self.btn_park.setStyleSheet("QPushButton { background:#f4ead8; color:#5a564e; "
            "border:1px solid rgba(204,120,92,0.3); border-radius:6px; padding:8px 12px; "
            "font-size:13px; font-weight:600; } QPushButton:hover { background:#ecd9b8; color:#1c1b18; }")
        self.btn_park.setEnabled(False)
        self.btn_park.clicked.connect(self._on_park)
        exit_row.addWidget(self.btn_park, 1)
        self.btn_drop = QPushButton("✗ 放弃")
        self.btn_drop.setStyleSheet("QPushButton { background:transparent; color:#8e8a82; "
            "border:1px solid rgba(28,27,24,0.18); border-radius:6px; padding:8px 12px; font-size:13px; }"
            "QPushButton:hover { color:#b94a4a; border-color:#b94a4a; }")
        self.btn_drop.clicked.connect(self._on_drop)
        exit_row.addWidget(self.btn_drop, 1)
        cp.addLayout(exit_row)

        outer.addWidget(chat, 1)

        # 禁掉所有按钮的「回车默认触发」:否则输入框按回车会同时触发默认按钮(放弃/定稿),
        # 窗口被关闭 + 刚起的后台线程被带走 → 崩。回车只应走输入框 returnPressed → 发送。
        for _b in self.findChildren(QPushButton):
            _b.setAutoDefault(False)
            _b.setDefault(False)

        draft = self._idea.get("draft")
        if draft and draft.get("messages"):
            self._restore_draft(draft)
        else:
            self._show_kickoff()

    def _load_prompt(self, name):
        import content_radar
        return content_radar._load_prompt(name)

    def _ctx_block(self):
        q = "\n".join(f"- 「{x}」" for x in (self._idea.get("source_quotes") or []))
        return (f"【选题】{self._idea.get('title','')}\n"
                f"【抓手】{self._idea.get('hook','')}\n"
                f"【原话依据】\n{q or '(无)'}\n")

    def _external_block(self):
        """二创选题:把对应的爆款分析文档全文注入上下文(截 4000 字)。"""
        if self._idea.get("origin") != "external":
            return ""
        p = self._idea.get("baokuan_path") or ""
        if not p:
            return ""
        try:
            from pathlib import Path as _P
            text = _P(p).read_text(encoding="utf-8")[:4000]
            return f"\n【爆款分析 · 二创参考(含数据/钩子/结构/仿写模板/评论区洞察)】\n{text}\n"
        except Exception:
            return ""

    def _framework_block(self):
        """读 vault 的「内容创作母框架.md」注入,让二创严格按 king 自己的方法论(改框架自动跟上)。"""
        try:
            from common import CONFIG as _C
            from pathlib import Path as _P
            vault = _C.get("obsidian", {}).get("vault", "")
            if vault:
                p = _P(vault) / "内容创作母框架.md"
                if p.exists():
                    return "\n【我的内容创作母框架(请严格遵守)】\n" + p.read_text(encoding="utf-8") + "\n"
        except Exception:
            pass
        return ""

    def _stage_flow(self) -> dict:
        """当前 format 的阶段流程:stage → {label, prompt, advance(下一步按钮文案/None), next, max}。
        二创:extract → rewrite。短视频:angle → script。公众号:frame → write → check。"""
        if self._idea.get("origin") == "external":
            return {
                "extract": {"label": "① 提炼观点 · 判断", "prompt": "recreate_extract",
                            "advance": "→ 套母框架改写", "next": "rewrite", "max": 3000},
                "rewrite": {"label": "② 套母框架 · 改写", "prompt": "recreate_rewrite",
                            "advance": None, "next": None, "max": 3000},
            }
        if self._fmt == "article":
            return {
                "frame": {"label": "① 框架 · 补细节", "prompt": "article_frame",
                          "advance": "→ 扩写全文", "next": "write", "max": 3000},
                "write": {"label": "② 全文初稿", "prompt": "article_write",
                          "advance": "→ 四层自检", "next": "check", "max": 8192},
                "check": {"label": "③ 四层自检 · 定稿", "prompt": "article_check",
                          "advance": None, "next": None, "max": 8192},
            }
        return {
            "organize": {"label": "① 梳理观点 · 判断", "prompt": "sv_organize",
                         "advance": "→ 套母框架写稿", "next": "rewrite", "max": 2500},
            "rewrite": {"label": "② 套母框架 · 口播稿", "prompt": "recreate_rewrite",
                        "advance": None, "next": None, "max": 2500},
        }

    def _first_stage(self) -> str:
        if self._idea.get("origin") == "external":
            return "extract"
        return "frame" if self._idea.get("format") == "article" else "organize"

    def _show_kickoff(self):
        """打开工坊先不调 AI — 显示选题 + 提示,等用户点「▶ 开始生成」才真正生成。"""
        flow = self._stage_flow()
        meta = flow[self._first_stage()]
        self.stage_lbl.setText(meta["label"])
        if self._idea.get("origin") == "external":
            tip = "AI 会先提炼这篇爆款的观点、评好坏、按五层论判断,再请你拍板保留哪些、立场是什么。"
        else:
            tip = ("AI 会基于左边的选题"
                   + ("搭长文框架、并列出需要你补充的真实细节。"
                      if self._fmt == "article" else "梳理观点、按五层论判断,再请你拍板立场。"))
        self.chat_view.setPlainText(
            f"━ 选题:{self._idea.get('title','')}\n\n点下方「▶ 开始生成」,{tip}\n")
        self.btn_kickoff.setVisible(True)
        self.btn_kickoff.setEnabled(True)

    def _do_kickoff(self):
        self.btn_kickoff.setVisible(False)
        self._start_first()

    def _start_first(self):
        flow = self._stage_flow()
        self._stage = self._first_stage()
        meta = flow[self._stage]
        self.stage_lbl.setText(meta["label"])
        if self._fmt == "article":
            kick = ("请基于下面这个选题,先做 HKR 质检和原型判断,再搭出长文框架,"
                    "并明确列出需要我补充的真实细节(红色问题)。\n\n" + self._ctx_block())
        elif self._idea.get("origin") == "external":
            kick = ("我看到一篇外部爆款想二创。先别写稿——按我的内容创作母框架,做三件事:"
                    "①逐条提炼原内容核心观点(是什么/好在哪/对我合不合适) "
                    "②用五层论判断我二创这选题最高能到第几层 "
                    "③列出需要我拍板的(保留哪些观点/我的立场/我能加的独家案例)。\n\n"
                    + self._framework_block() + self._external_block() + self._ctx_block())
        else:
            kick = ("这是我自己的一个选题。先别写稿——按我的内容创作母框架,帮我:"
                    "①提炼这个选题的核心观点 ②用五层论判断它最高到第几层、反常识内核是什么 "
                    "③列出需要我拍板的(我的立场/能撑的真实案例/打哪类人)。\n\n"
                    + self._framework_block() + self._ctx_block())
        self._messages = [{"role": "user", "content": kick}]
        self.chat_view.appendPlainText(f"━ 选题:{self._idea.get('title','')}\n")
        self._send_to_llm(self._load_prompt(meta["prompt"]), max_tokens=meta["max"])

    def _on_advance(self):
        """推进到下一阶段。短视频:angle→script(清空重起,只带选定角度)。
        公众号:frame→write(保留整段框架+补细节)→check(对全文自检)。"""
        flow = self._stage_flow()
        nxt = flow.get(self._stage, {}).get("next")
        if not nxt:
            return
        meta = flow[nxt]
        last_assistant = ""
        for m in reversed(self._messages):
            if m["role"] == "assistant":
                last_assistant = m["content"]; break

        if nxt == "rewrite":
            # 二创 / 普通短视频:都进「套母框架改写」,保留整段对话(含 king 确认的观点+立场)
            self._messages.append({"role": "user",
                "content": "我已经在上面确认了观点和我的立场。现在严格按我的内容创作母框架"
                           "(Why→原理→行动 三步骨架),写成一条可以直接念的口播稿(300-600字,纯口语)。"
                           "保留我确认的精华,用我的立场,写完附七条铁律自查。"})
            self.chat_view.appendPlainText("\n━━━ 切换到母框架改写阶段 ━━━\n")
        elif nxt == "write":
            self._messages.append({"role": "user",
                "content": "好,框架和我补充的真实细节都在上面了。现在请严格按你的写作风格,"
                           "把它扩写成一篇完整的公众号长文（4000-8000 字），直接输出正文；不要擅自添加署名。"})
            self.chat_view.appendPlainText("\n━━━ 切换到全文扩写阶段(稍慢,4000-8000 字) ━━━\n")
        elif nxt == "check":
            self._messages.append({"role": "user",
                "content": "请对上面这篇全文跑完整的四层自检,先输出质检报告,再直接给出修复好的定稿全文。"})
            self.chat_view.appendPlainText("\n━━━ 切换到四层自检阶段 ━━━\n")

        self._stage = nxt
        self.stage_lbl.setText(meta["label"])
        self.btn_advance.setEnabled(False)
        self._send_to_llm(self._load_prompt(meta["prompt"]), max_tokens=meta["max"])

    def _on_send(self):
        # 防重入:正在生成(线程还在跑)时,忽略回车/连点,避免起多个 QThread 抢同一窗口导致崩溃
        t = getattr(self, "_thread", None)
        if t is not None and t.isRunning():
            return
        text = self.input_edit.text().strip()
        if not text:
            return
        try:
            self.input_edit.clear()
            self.chat_view.appendPlainText(f"\n我：{text}\n")
            self._messages.append({"role": "user", "content": text})
            flow = self._stage_flow()
            meta = flow.get(self._stage) or flow[self._first_stage()]
            self._send_to_llm(self._load_prompt(meta["prompt"]), max_tokens=meta["max"])
        except Exception as e:
            import traceback
            self.chat_view.appendPlainText(f"\n[发送出错,已跳过] {e}")
            try:
                (ROOT / "runtime" / "studio-err.log").open("a", encoding="utf-8").write(
                    traceback.format_exc() + "\n---\n")
            except Exception:
                pass

    def _send_to_llm(self, system, max_tokens=2500):
        self.input_edit.setEnabled(False)
        self.btn_send.setEnabled(False)
        self.btn_send.setText("生成中…")
        self.chat_view.appendPlainText("\nAI 生成中…")
        from PySide6.QtCore import QThread
        self._thread = QThread(self)
        self._worker = ContentStudioWorker(system, list(self._messages), max_tokens=max_tokens)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.done.connect(self._on_llm_done)
        self._worker.failed.connect(self._on_llm_failed)
        self._worker.done.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.start()

    def _on_llm_done(self, reply):
        text = self.chat_view.toPlainText()
        if text.endswith("AI 生成中…"):
            self.chat_view.setPlainText(text[:-len("AI 生成中…")] + f"AI:{reply}\n")
        else:
            self.chat_view.appendPlainText(f"\nAI:{reply}\n")
        self._messages.append({"role": "assistant", "content": reply})
        self.input_edit.setEnabled(True)
        self.btn_send.setEnabled(True)
        self.btn_send.setText("发送")
        self.btn_park.setEnabled(True)
        meta = self._stage_flow()[self._stage]
        if meta.get("advance"):
            self.btn_advance.setText(meta["advance"])
            self.btn_advance.setEnabled(True)
        else:
            self.btn_advance.setEnabled(False)
        # 末阶段可定稿:短视频 script / 公众号 write·check / 二创 rewrite
        if self._stage in ("script", "write", "check", "rewrite"):
            self.btn_finalize.setEnabled(True)
        sb = self.chat_view.verticalScrollBar(); sb.setValue(sb.maximum())
        self.input_edit.setFocus()

    def _on_llm_failed(self, err):
        self.chat_view.appendPlainText(f"\n[失败] {err.splitlines()[0]}")
        self.input_edit.setEnabled(True)
        self.btn_send.setEnabled(True)
        self.btn_send.setText("发送")
        QMessageBox.warning(self, "生成失败", err[:800])

    def _restore_draft(self, draft):
        flow = self._stage_flow()
        self._stage = draft.get("stage", self._first_stage())
        if self._stage not in flow:
            self._stage = self._first_stage()
        self._messages = draft.get("messages") or []
        meta = flow[self._stage]
        self.stage_lbl.setText(meta["label"])
        self.chat_view.appendPlainText(f"━ 恢复草稿 · {self._idea.get('title','')}\n")
        for m in self._messages:
            c = m.get("content", "")
            if m["role"] == "user" and (c.startswith("请基于") or c.startswith("基于下面")
                                        or c.startswith("好,框架") or c.startswith("请对上面")):
                continue
            who = "我" if m["role"] == "user" else "AI"
            self.chat_view.appendPlainText(f"\n{who}:{c}\n")
        if any(m["role"] == "assistant" for m in self._messages):
            self.btn_park.setEnabled(True)
            if meta.get("advance"):
                self.btn_advance.setText(meta["advance"])
                self.btn_advance.setEnabled(True)
            if (self._fmt == "shortvideo" and self._stage == "script") or \
               (self._fmt == "article" and self._stage in ("write", "check")):
                self.btn_finalize.setEnabled(True)

    def _final_content(self):
        for m in reversed(self._messages):
            if m["role"] == "assistant":
                return m["content"]
        return ""

    def _on_finalize(self):
        content = self._final_content()
        if not content:
            QMessageBox.information(self, "还没有内容", "先生成内容再定稿")
            return
        p = self._save_archive(content)
        try:
            import content_ideas
            content_ideas.mark_done(self._iid, content, self._messages)
        except Exception:
            pass
        self._exit_taken = True
        msg = f"✓ 已定稿保存到:\n{p}" if p else "✓ 已定稿(vault 未配,没落地文件)"
        QMessageBox.information(self, "定稿", msg)
        self.accept()

    def _save_archive(self, content):
        try:
            from wiki_appender import _wiki_root
            root = _wiki_root()
        except Exception:
            root = None
        if root is None:
            return None
        sub = "短视频" if self._fmt == "shortvideo" else "公众号"
        out_dir = root / "内容产出" / sub
        out_dir.mkdir(parents=True, exist_ok=True)
        import re
        safe = re.sub(r'[\\/:*?"<>|]', "_", self._idea.get("title", ""))[:40]
        day = dt.date.today().isoformat()
        p = out_dir / f"{day}-{safe}.md"
        content_id = f"shengnian-content-{self._iid or safe}-{day}"
        header = ("---\n"
                  "ai_generated: true\n"
                  "ai_service_provider: 声年\n"
                  f"content_id: {content_id}\n"
                  "---\n\n"
                  f"# {self._idea.get('title','')}\n\n"
                  "> AI 辅助生成，使用或发布前请核实。\n\n"
                  f"> {sub} · {day} · 来自语音日记选题\n"
                  f"> 抓手:{self._idea.get('hook','')}\n\n---\n\n")
        try:
            p.write_text(header + content, encoding="utf-8")
            return p
        except Exception:
            return None

    def _on_park(self):
        if not self._iid:
            QMessageBox.warning(self, "无法暂存", "选题没有 id")
            return
        try:
            import content_ideas
            content_ideas.save_draft(self._iid, self._stage, self._messages)
            self._exit_taken = True
            QMessageBox.information(self, "已暂存", "下次点这个选题接着创作")
            self.accept()
        except Exception as e:
            QMessageBox.warning(self, "暂存失败", str(e))

    def _on_drop(self):
        r = QMessageBox.question(self, "放弃选题",
            "这个选题不做了?从选题列表移除(留 archive)")
        if r != QMessageBox.Yes:
            return
        self._detach_running_thread()   # 先安全停掉后台线程,避免崩
        try:
            import content_ideas
            content_ideas.mark_dismissed(self._iid, "user_dropped")
        except Exception:
            pass
        self._exit_taken = True
        self.accept()

    def _detach_running_thread(self):
        """关闭/放弃前,若后台 AI 线程还在跑:断开它对本窗口 UI 的回调(否则线程结束时
        访问已销毁控件→崩),并 reparent 到主窗口让它后台安全跑完,不随 dialog 销毁而崩。"""
        t = getattr(self, "_thread", None)
        w = getattr(self, "_worker", None)
        if t is not None and t.isRunning():
            if w is not None:
                try: w.done.disconnect(self._on_llm_done)
                except Exception: pass
                try: w.failed.disconnect(self._on_llm_failed)
                except Exception: pass
            try:
                mw = self.parent()
                if mw is not None:
                    t.setParent(mw)            # dialog 销毁不再带走 running thread
                t.finished.connect(t.deleteLater)
            except Exception:
                pass
        self._thread = None
        self._worker = None

    def closeEvent(self, e):
        self._detach_running_thread()
        if (not self._exit_taken
                and any(m["role"] == "assistant" for m in self._messages)
                and self._iid):
            r = QMessageBox.question(self, "未保存",
                "有未保存的创作。暂存草稿?\n是=暂存 / 否=丢弃 / 取消=不关",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel, QMessageBox.Yes)
            if r == QMessageBox.Cancel:
                e.ignore(); return
            if r == QMessageBox.Yes:
                try:
                    import content_ideas
                    content_ideas.save_draft(self._iid, self._stage, self._messages)
                except Exception:
                    pass
        e.accept()


# ============================================================
# 入口
# ============================================================
def _dispatch_internal_role() -> bool:
    if len(sys.argv) < 3 or sys.argv[1] != "--role":
        return False
    # PyInstaller 的 windowed EXE 没有控制台，sys.stdout/sys.stderr 为 None。
    # FunASR/tqdm 等依赖仍会输出进度；给后台 role 一个可写的空流，避免
    # 推理过程中出现 "NoneType has no attribute write"。
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    role = sys.argv[2]
    sys.argv = [sys.argv[0], *sys.argv[3:]]
    if role == "recorder":
        import recorder
        recorder.main()
    elif role == "transcriber":
        import transcriber
        transcriber.main()
    elif role == "daily-summary":
        import daily_summary
        daily_summary.main()
    elif role == "ingest-url":
        import ingest_url
        ingest_url.main()
    elif role == "moments":
        import runpy
        # 用模块属性访问，避免 package-smoke 分支里的局部 import 在冻结构建中
        # 让同名函数被错误编译为局部变量，导致朋友圈角色启动即崩溃。
        if _runtime_profile.is_commercial_mode():
            os.environ.setdefault("VOICE_JOURNAL_DATA_ROOT", str(ROOT))
            os.environ.setdefault("VOICE_JOURNAL_MOMENTS_DATA_DIR", str(MOMENTS_WORKFLOW_DIR))
            os.environ.setdefault(
                "VOICE_JOURNAL_CONFIG",
                str(RESOURCE_ROOT / "config.commercial.toml"),
            )
        runpy.run_path(str(MOMENTS_SCRIPT), run_name="__main__")
    elif role == "yt-dlp":
        import yt_dlp
        yt_dlp.main(sys.argv[1:])
    elif role == "package-smoke":
        from runtime_profile import (
            automatic_browser_cookie_access_enabled,
        )
        import recorder
        import transcriber
        try:
            import qrcode

            qr_code_available = qrcode.make(
                "weixin://wxpay/bizpayurl?pr=package-smoke"
            ).getbbox() is not None
        except Exception:
            qr_code_available = False
        skill_import_load_ok = False
        try:
            from cards.skill_import import load_skill as packaged_load_skill
            skill_import_available = callable(packaged_load_skill)
            probe_dir = ROOT / "runtime" / "package-smoke-skill"
            probe_path = probe_dir / "SKILL.md"
            probe_dir.mkdir(parents=True, exist_ok=True)
            probe_path.write_text(
                "---\nname: 打包自检\nlicense: MIT\n---\n"
                "只根据用户提供的文字整理三条清晰结论。",
                encoding="utf-8",
            )
            try:
                probe_skill = packaged_load_skill(str(probe_path))
                skill_import_load_ok = (
                    probe_skill.name == "打包自检"
                    and "三条清晰结论" in probe_skill.rules
                )
            finally:
                probe_path.unlink(missing_ok=True)
                try:
                    probe_dir.rmdir()
                except OSError:
                    pass
        except Exception:
            skill_import_available = False

        report = {
            "commercial_mode": is_commercial_mode(),
            "data_root": str(ROOT),
            "resource_root": str(RESOURCE_ROOT),
            "config_loaded": bool(CONFIG),
            "deep_discussion_enabled": feature_enabled("deep_discussion"),
            "automatic_browser_cookie_access_enabled": (
                automatic_browser_cookie_access_enabled(CONFIG)
            ),
            "recorder_entry_available": callable(getattr(recorder, "main", None)),
            "transcriber_entry_available": callable(getattr(transcriber, "main", None)),
            "skill_import_available": skill_import_available,
            "skill_import_load_ok": skill_import_load_ok,
            "wechat_qr_code_available": qr_code_available,
            "default_hotwords_available": (ROOT / "hotwords.txt").exists(),
            "default_corrections_available": (ROOT / "corrections.json").exists(),
            "bundled_asr_model_available": (
                RESOURCE_ROOT / "models" / "asr" / "model.pt"
            ).exists(),
            "bundled_vad_model_available": (
                RESOURCE_ROOT / "models" / "vad" / "model.pt"
            ).exists(),
            "bundled_punc_model_available": (
                RESOURCE_ROOT / "models" / "punc" / "model.pt"
            ).exists(),
            "bundled_speaker_model_available": (
                RESOURCE_ROOT
                / "models"
                / "speaker"
                / "campplus_cn_common.bin"
            ).exists(),
        }
        target = ROOT / "runtime" / "package-smoke.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        raise SystemExit(f"未知内部 role: {role}")
    return True


def main():
    if _dispatch_internal_role():
        return
    # ── 高 DPI 策略 + FreeType 字体引擎：必须在 QApplication 创建前调 ──
    import os as _os
    _os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    _os.environ.setdefault("QT_SCALE_FACTOR_ROUNDING_POLICY", "PassThrough")
    # 关键：用 FreeType 取代 DirectWrite 渲染中文（解决 Qt6 + ClearType 双重抗锯齿导致中文糊的问题）
    configure_qt_environment()

    from PySide6.QtCore import Qt
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("声年")
    app.setOrganizationName("King")
    launcher_lock = RoleLock(ROOT, "launcher")
    if not launcher_lock.acquire():
        QMessageBox.information(None, "声年", "声年已经在运行，请打开已有窗口。")
        return
    app.aboutToQuit.connect(launcher_lock.release)

    # ── 字体：Segoe UI（英文）+ Microsoft YaHei UI（中文，ClearType 优化版）──
    # 雅黑 UI 是微软专门给 Windows 8+ 做的 hint 优化版，跟 ClearType 完美配合
    from PySide6.QtGui import QFontDatabase, QFont
    families = set(QFontDatabase.families())
    # 优先 Inter（Claude 网页同款），中文 fallback 到雅黑 UI
    primary = "Inter" if "Inter" in families else app.font().family()
    startup_font_scale = load_font_scale(ROOT)
    base_font = QFont(primary, max(8, round(11 * startup_font_scale)))
    fallback = [primary]
    if primary == "Inter" and "Segoe UI" in families:
        fallback.append("Segoe UI")
    for fam in ("PingFang SC", "Microsoft YaHei UI", "Microsoft YaHei"):
        if fam in families:
            fallback.append(fam)
    base_font.setFamilies(fallback)
    base_font.setWeight(QFont.Weight.Medium)
    base_font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    app.setFont(base_font)

    app.setStyleSheet(scale_stylesheet_font_sizes(QSS, startup_font_scale))
    win = Launcher()
    win.show()

    # ── Qt 早期把窗口放在 (-16000, -16000) 等渲染,某些情况下不会自动 move 回来 ──
    # 用多个延迟 single-shot 强制把窗口拉到屏幕中央 + 前台
    def _bring_to_front():
        try:
            from PySide6.QtGui import QGuiApplication as _QGA
            screen = _QGA.primaryScreen()
            if screen:
                geo = screen.availableGeometry()
                w_w = max(1100, win.width())
                w_h = max(620, win.height())
                cur = win.geometry()
                # 如果窗口位置异常(负超大,或宽高过小),硬拽回中央
                if cur.x() < -1000 or cur.y() < -1000 or cur.width() < 800 or cur.height() < 400:
                    x = geo.x() + max(0, (geo.width() - w_w) // 2)
                    y = geo.y() + max(0, (geo.height() - w_h) // 2)
                    win.setGeometry(x, y, w_w, w_h)
        except Exception:
            pass
        win.raise_()
        win.activateWindow()

    QTimer.singleShot(0, _bring_to_front)
    QTimer.singleShot(200, _bring_to_front)
    QTimer.singleShot(800, _bring_to_front)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
