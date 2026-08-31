"""声年自定义卡片工作台的可复用 PySide6 组件。

这个模块刻意不依赖 ``launcher.py``。主窗口只需要提供 ``CardStore``、
可选的后台回调，以及待办/已办等内置正文组件工厂，即可嵌入工作台。

所有可能访问云端或调用大模型的回调都通过 :class:`AsyncTaskRunner`
放入 ``QThreadPool``。本地布局、隐藏、版本指针等轻量操作直接调用 store。
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from PySide6.QtCore import (
    QAbstractAnimation,
    QByteArray,
    QDate,
    QEasingCurve,
    QMimeData,
    QObject,
    QPoint,
    QParallelAnimationGroup,
    QPropertyAnimation,
    QRect,
    QRunnable,
    QSize,
    Qt,
    QThreadPool,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import QAction, QDrag
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDateEdit,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from brief_presenter import render_card_content
from cards.skill_import import load_skill
from ui_preferences import load_font_scale, scale_stylesheet_font_sizes


CARD_MIME_TYPE = "application/x-shengnian-card"
CARD_WIDTHS = ("standard", "full")
CARD_WIDTH_DRAG_THRESHOLD = 72
MIN_CARD_HEIGHT = 260
MAX_CARD_HEIGHT = 760
DEFAULT_CARD_HEIGHT = 320
DEFAULT_CARD_IDS = {
    "today_brief",
    "yesterday_brief",
    "todos",
    "done",
    "short_video",
}
OFFICIAL_CARD_TEMPLATES: tuple[dict[str, str], ...] = (
    {
        "name": "人生建议",
        "description": "从长期语音记录里找出反复出现的人生选择、困惑和价值取向。",
        "time_range": "last_30_days",
        "prompt": (
            "请根据我最近一个月的语音记录，找出我反复提到的重要选择、困惑和"
            "价值取向。给出最多 3 条真正有语料依据的人生建议。每条先说明观察到的"
            "事实，再给建议和一个可以执行的小行动；不要说空泛鸡汤。"
        ),
    },
    {
        "name": "生活建议",
        "description": "发现生活节奏、习惯和状态中可以改善的小问题。",
        "time_range": "last_7_days",
        "prompt": (
            "请根据我最近 7 天的语音记录，找出影响生活质量的习惯、琐事和反复"
            "出现的问题，给出最多 5 条具体的生活建议。优先低成本、能马上执行的"
            "改变，并说明每条建议来自哪些语料线索。"
        ),
    },
    {
        "name": "工作建议",
        "description": "从项目、沟通和执行记录里找到最值得调整的工作方式。",
        "time_range": "last_7_days",
        "prompt": (
            "请根据我最近 7 天的语音记录，分析当前工作中的进展、阻塞、沟通和"
            "执行问题，给出最多 5 条工作建议。区分必须马上处理、可以优化和暂时"
            "不做的事情；不要虚构项目状态。"
        ),
    },
    {
        "name": "时间分配建议",
        "description": "看清时间花在哪里，哪些投入值得保留或减少。",
        "time_range": "last_7_days",
        "prompt": (
            "请根据我最近 7 天的语音记录，判断我的时间主要花在工作、学习、沟通、"
            "生活和杂事中的哪些部分。只在语料有依据时估计占比，指出最值得保留、"
            "减少和重新安排的事项，并给出下周的时间分配建议。"
        ),
    },
    {
        "name": "本周复盘",
        "description": "把一周的进展、决定、问题和下周重点放在一起。",
        "time_range": "last_7_days",
        "prompt": (
            "请复盘我最近 7 天的语音记录，整理本周完成的关键进展、做出的决定、"
            "没有解决的问题、学到的经验和下周最重要的 3 件事。事实和建议分开，"
            "不把随口提到的想法当成已完成事项。"
        ),
    },
    {
        "name": "决策复盘",
        "description": "找到最近做过的决定，并检查依据、风险和后续验证。",
        "time_range": "last_30_days",
        "prompt": (
            "请从我最近 30 天的语音记录中找出明确做过或准备做的关键决定。逐条"
            "说明决定内容、当时依据、仍未验证的假设、潜在风险和下一步验证动作。"
            "没有明确证据时标注不确定，不替我补造理由。"
        ),
    },
    {
        "name": "灵感清单",
        "description": "收集值得继续发展的想法、表达和未完成线索。",
        "time_range": "last_30_days",
        "prompt": (
            "请从我最近 30 天的语音记录中找出值得继续发展的灵感、观点和未完成"
            "想法，合并重复内容后给出最多 10 条。每条保留原意，说明为什么值得"
            "继续，并给出一个下一步。"
        ),
    },
    {
        "name": "反复出现的问题",
        "description": "识别一个月里多次出现但一直没有解决的问题。",
        "time_range": "last_30_days",
        "prompt": (
            "请从我最近 30 天的语音记录中识别反复出现、一直没有真正解决的问题。"
            "按出现频率和影响排序，说明语料依据、可能卡点和一个最小解决动作。"
            "不要把偶尔出现一次的话题判断为长期问题。"
        ),
    },
)


CARD_BOARD_QSS = """
QWidget#shengnianCardBoard {
    background: qlineargradient(
        x1:0, y1:0, x2:0, y2:1,
        stop:0 #f8f5ee, stop:1 #f2eee5
    );
    color: #24211d;
    font-family: "Noto Sans CJK SC", "Microsoft YaHei UI";
}
QLabel#cardBoardEyebrow {
    color: #a45e47;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 4px;
}
QLabel#cardBoardTitle {
    color: #24211d;
    font-family: "Noto Serif CJK SC", "Source Han Serif SC", "SimSun";
    font-size: 25px;
    font-weight: 700;
}
QLabel#cardBoardSubtitle {
    color: #716b61;
    font-size: 12px;
}
QLabel#cardBoardStatus {
    color: #8a6657;
    font-size: 12px;
    padding: 4px 0;
}
QLabel#cardBoardDropZone {
    color: #8a6657;
    background: rgba(255,255,255,0.54);
    border: 1px dashed rgba(190,114,88,0.62);
    border-radius: 8px;
    padding: 10px;
}
QPushButton#cardBoardPrimary {
    background: #be7258;
    color: #fffaf3;
    border: 0;
    border-radius: 8px;
    padding: 8px 16px;
    font-weight: 700;
}
QPushButton#cardBoardPrimary:hover { background: #ad634a; }
QPushButton#cardBoardSecondary, QToolButton#cardBoardSecondary {
    background: rgba(255,255,255,0.72);
    color: #39342e;
    border: 1px solid rgba(54,48,41,0.16);
    border-radius: 8px;
    padding: 8px 14px;
}
QPushButton#cardBoardSecondary:hover, QToolButton#cardBoardSecondary:hover {
    border-color: rgba(190,114,88,0.65);
    color: #a45e47;
}
QFrame#cardFrame {
    background: rgba(255,255,255,0.93);
    border: 1px solid rgba(54,48,41,0.12);
    border-radius: 13px;
}
QFrame#cardFrame[busy="true"] {
    background: rgba(250,247,240,0.94);
    border-color: rgba(190,114,88,0.42);
}
QFrame#cardFrame[useful="true"] {
    border-color: rgba(113,133,88,0.62);
}
QFrame#cardFrame[dragging="true"] {
    background: rgba(255,250,243,0.72);
    border: 2px dashed rgba(190,114,88,0.72);
}
QLabel#cardTitle {
    color: #27231f;
    font-family: "Noto Serif CJK SC", "Source Han Serif SC", "SimSun";
    font-size: 18px;
    font-weight: 700;
}
QLabel#cardMeta {
    color: #918a80;
    font-size: 11px;
}
QLabel#cardScope {
    color: #716b61;
    font-size: 11px;
    background: #f3efe6;
    border-radius: 5px;
    padding: 3px 7px;
}
QLabel#cardSizeBadge {
    color: #a45e47;
    font-size: 11px;
    background: #fff4ec;
    border: 1px solid rgba(190,114,88,0.22);
    border-radius: 5px;
    padding: 3px 7px;
}
QPlainTextEdit#cardContent {
    color: #38332d;
    background: transparent;
    border: 0;
    padding: 4px 0;
    selection-background-color: rgba(190,114,88,0.25);
    font-size: 13px;
}
QLabel#cardDisclaimer {
    color: #aaa39a;
    font-size: 10px;
}
QPushButton#cardAction, QToolButton#cardAction {
    background: transparent;
    color: #6c655c;
    border: 0;
    border-radius: 6px;
    padding: 5px 8px;
    font-size: 12px;
}
QPushButton#cardAction:hover, QToolButton#cardAction:hover {
    background: #f3eee5;
    color: #a45e47;
}
QPushButton#cardUseful {
    background: transparent;
    color: #6f685f;
    border: 1px solid rgba(54,48,41,0.12);
    border-radius: 7px;
    padding: 5px 10px;
}
QPushButton#cardUseful:checked {
    color: #51623f;
    background: #edf1e7;
    border-color: rgba(81,98,63,0.30);
}
QToolButton#cardDragHandle {
    background: transparent;
    color: #b3aaa0;
    border: 0;
    border-radius: 6px;
    padding: 4px 7px;
    font-size: 17px;
}
QToolButton#cardDragHandle:hover {
    background: #f3eee5;
    color: #a45e47;
}
QFrame#cardResizeBar {
    background: rgba(54,48,41,0.08);
    border: 0;
    border-radius: 3px;
    margin: 2px 42px 0 42px;
}
QFrame#cardResizeBar:hover {
    background: rgba(190,114,88,0.58);
}
QFrame#hiddenTray {
    background: rgba(238,232,221,0.88);
    border: 1px dashed rgba(54,48,41,0.20);
    border-radius: 10px;
}
QPushButton#restoreChip {
    background: #fffaf2;
    color: #615a51;
    border: 1px solid rgba(54,48,41,0.13);
    border-radius: 11px;
    padding: 5px 10px;
}
QPushButton#restoreChip:hover { color: #a45e47; }
QDialog {
    background: #faf7f0;
    color: #2c2823;
    font-family: "Noto Sans CJK SC", "Microsoft YaHei UI";
}
QDialog QLineEdit, QDialog QPlainTextEdit, QDialog QComboBox {
    background: #fffefa;
    color: #2c2823;
    border: 1px solid rgba(54,48,41,0.18);
    border-radius: 8px;
    padding: 8px;
    selection-background-color: rgba(190,114,88,0.25);
}
QDialog QLineEdit:focus, QDialog QPlainTextEdit:focus {
    border-color: rgba(190,114,88,0.75);
}
"""


def _read(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _format_time(value: Any) -> str:
    if not value:
        return "尚未生成"
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return text
    return parsed.astimezone().strftime("%m月%d日 %H:%M")


def _source_label(card: Any) -> str:
    sources = tuple(_read(card, "sources", ()) or ())
    time_range = str(_read(card, "time_range", "") or "").strip()
    labels = {
        "transcripts": "语音记录",
        "imports": "导入资料",
        "todos": "待办",
        "done": "已办",
        "cards": "卡片",
        "confirmed_cards": "已确认卡片",
        "all": "全部资料",
    }
    source_text = "、".join(labels.get(str(item), str(item)) for item in sources)
    if not source_text:
        source_text = "本地资料"
    if time_range:
        custom_match = re.fullmatch(
            r"custom:(\d{4}-\d{2}-\d{2}):(\d{4}-\d{2}-\d{2})",
            time_range,
        )
        range_label = {
            "today": "今天",
            "yesterday": "昨天",
            "last_7_days": "最近7天",
            "last_30_days": "最近30天",
            "all": "全部历史",
        }.get(time_range, time_range)
        if custom_match:
            range_label = (
                f"{custom_match.group(1)} 至 {custom_match.group(2)}"
            )
        return f"{range_label} · {source_text}"
    return source_text


def _content_of(revision: Any) -> str:
    if revision is None:
        return ""
    if isinstance(revision, str):
        return revision
    return str(_read(revision, "content", "") or "")


def _is_unprocessed_yesterday_content(content: str) -> bool:
    """识别旧版误导入的原始语音日记，避免它进入可回退版本。"""

    return bool(re.search(r"(?m)^\s*#\s*语音日记\b", str(content or "")))


def _revision_id(revision: Any) -> str:
    return str(
        _read(revision, "revision_id", _read(revision, "id", "")) or ""
    )


def _is_default(card: Any) -> bool:
    card_id = str(_read(card, "card_id", "") or "")
    return bool(
        _read(card, "is_default", False)
        or _read(card, "card_type", "") == "default"
        or card_id in DEFAULT_CARD_IDS
    )


def _looks_durable(instruction: str) -> bool:
    text = instruction.strip()
    return bool(
        re.search(
            r"(以后|今后|每次|总是|一直|长期|固定|默认|不要再|都要|都用|记住)",
            text,
        )
    )


def _dropped_skill_path(mime_data: QMimeData) -> str:
    """从拖放数据中提取一个本地 Markdown Skill 文件。"""

    if mime_data is None or not mime_data.hasUrls():
        return ""
    local_paths = [
        Path(url.toLocalFile())
        for url in mime_data.urls()
        if url.isLocalFile() and url.toLocalFile()
    ]
    if len(local_paths) != 1:
        return ""
    path = local_paths[0]
    if not path.is_file() or path.suffix.lower() not in {".md", ".markdown"}:
        return ""
    return str(path.resolve())


@dataclass(slots=True)
class CardBoardCallbacks:
    """卡片工作台可选的长任务回调。

    这些函数不会在 GUI 线程运行。若未提供函数，工作台会发射相应 signal，
    由宿主自行异步处理后调用 ``complete_*`` 方法回填。
    """

    generation_gate: Callable[[str], bool] | None = None
    compile_card: Callable[[str], Any] | None = None
    generate_card: Callable[[str], Any] | None = None
    revise_card: Callable[[str, str, bool], Any] | None = None
    chat_card: Callable[[str, list[dict[str, str]]], Any] | None = None
    open_todo_capture: Callable[[], Any] | None = None
    convert_to_todo: Callable[[str, str], Any] | None = None
    open_short_video: Callable[[str], Any] | None = None


class _TaskSignals(QObject):
    succeeded = Signal(str, object)
    failed = Signal(str, str)


class _Task(QRunnable):
    def __init__(self, key: str, fn: Callable[[], Any]):
        super().__init__()
        self.key = key
        self.fn = fn
        self.signals = _TaskSignals()
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:
        try:
            result = self.fn()
        except Exception as exc:  # pragma: no cover - exercised via runner signal
            self.signals.failed.emit(self.key, str(exc))
        else:
            self.signals.succeeded.emit(self.key, result)


class AsyncTaskRunner(QObject):
    """在共享 QThreadPool 中运行长任务，按 key 防止重复提交。"""

    busyChanged = Signal(str, bool)
    taskFailed = Signal(str, str)

    def __init__(self, parent: QObject | None = None, max_workers: int = 3):
        super().__init__(parent)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(max(1, int(max_workers)))
        self._tasks: dict[str, _Task] = {}
        self._success: dict[str, Callable[[Any], None] | None] = {}
        self._failure: dict[str, Callable[[str], None] | None] = {}

    def is_busy(self, key: str) -> bool:
        return key in self._tasks

    def submit(
        self,
        key: str,
        fn: Callable[[], Any],
        on_success: Callable[[Any], None] | None = None,
        on_failure: Callable[[str], None] | None = None,
    ) -> bool:
        if key in self._tasks:
            return False
        task = _Task(key, fn)
        task.signals.succeeded.connect(self._on_succeeded)
        task.signals.failed.connect(self._on_failed)
        self._tasks[key] = task
        self._success[key] = on_success
        self._failure[key] = on_failure
        self.busyChanged.emit(key, True)
        self.pool.start(task)
        return True

    @Slot(str, object)
    def _on_succeeded(self, key: str, result: Any) -> None:
        callback = self._success.pop(key, None)
        self._failure.pop(key, None)
        self._tasks.pop(key, None)
        self.busyChanged.emit(key, False)
        if callback:
            callback(result)

    @Slot(str, str)
    def _on_failed(self, key: str, message: str) -> None:
        callback = self._failure.pop(key, None)
        self._success.pop(key, None)
        self._tasks.pop(key, None)
        self.busyChanged.emit(key, False)
        self.taskFailed.emit(key, message)
        if callback:
            callback(message)


class DragHandle(QToolButton):
    dragRequested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setText("⠿")
        self.setToolTip("按住拖动，直接调整卡片顺序")
        self.setObjectName("cardDragHandle")
        self._press_pos: QPoint | None = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.position().toPoint()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if (
            self._press_pos is not None
            and event.buttons() & Qt.MouseButton.LeftButton
            and (event.position().toPoint() - self._press_pos).manhattanLength() >= 8
        ):
            self.dragRequested.emit()
            self._press_pos = None
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._press_pos = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        super().mouseReleaseEvent(event)


class CardResizeBar(QFrame):
    """通过同一个底边控制条调整高度，并在两档宽度之间切换。"""

    resizeCommitted = Signal(int, str)
    widthPreviewed = Signal(str)

    def __init__(
        self,
        card: QWidget,
        *,
        height: int,
        width: str,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._card = card
        self._height = max(MIN_CARD_HEIGHT, min(MAX_CARD_HEIGHT, int(height)))
        self._width = "full" if width == "full" else "standard"
        self._press_global: QPoint | None = None
        self._start_height = self._height
        self._start_width = self._width
        self.setObjectName("cardResizeBar")
        self.setFixedHeight(10)
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.setToolTip(
            "上下拖动调整高度；向右拖变成大卡片，向左拖变成标准卡片"
        )

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_global = event.globalPosition().toPoint()
            self._start_height = self._card.height()
            self._start_width = self._width
            event.accept()
            return
        super().mousePressEvent(event)

    def _width_for_horizontal_delta(self, delta_x: int) -> str:
        if delta_x >= CARD_WIDTH_DRAG_THRESHOLD:
            return "full"
        if delta_x <= -CARD_WIDTH_DRAG_THRESHOLD:
            return "standard"
        return self._start_width

    def _apply_drag_delta(self, delta: QPoint) -> None:
        self._height = max(
            MIN_CARD_HEIGHT,
            min(MAX_CARD_HEIGHT, self._start_height + delta.y()),
        )
        self._card.setFixedHeight(self._height)
        next_width = self._width_for_horizontal_delta(delta.x())
        if next_width != self._width:
            self._width = next_width
            self.widthPreviewed.emit(self._width)
        size_text = "大卡片" if self._width == "full" else "标准卡片"
        self.setToolTip(
            f"当前高度 {self._height}px · {size_text}；松开保存"
        )

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if (
            self._press_global is None
            or not event.buttons() & Qt.MouseButton.LeftButton
        ):
            super().mouseMoveEvent(event)
            return
        delta = event.globalPosition().toPoint() - self._press_global
        self._apply_drag_delta(delta)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if (
            self._press_global is not None
            and event.button() == Qt.MouseButton.LeftButton
        ):
            self._press_global = None
            self.setToolTip(
                "上下拖动调整高度；向右拖变成大卡片，向左拖变成标准卡片"
            )
            self.resizeCommitted.emit(self._height, self._width)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class CardCanvas(QWidget):
    """接受卡片拖放的网格容器。"""

    cardDropped = Signal(str, int)
    cardPreviewed = Signal(str, int)
    dragPositionChanged = Signal(QPoint)
    widthChanged = Signal(int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(14)
        self.grid.setVerticalSpacing(14)
        self._ordered_widgets: list[QWidget] = []
        self._drop_slots: dict[str, QRect] = {}
        self._active_card_id = ""
        self._preview_index: int | None = None
        self._last_preview_point: QPoint | None = None

    def set_ordered_widgets(self, widgets: list[QWidget]) -> None:
        self._ordered_widgets = widgets

    def set_drop_slots(self, slots: Mapping[str, QRect]) -> None:
        self._drop_slots = {
            str(card_id): QRect(rect)
            for card_id, rect in slots.items()
        }

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.widthChanged.emit(event.size().width())

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat(CARD_MIME_TYPE):
            self._active_card_id = self._decode_card_id(event)
            self._preview_index = None
            self._last_preview_point = None
            self.set_drop_slots(
                {
                    str(getattr(widget, "card_id", "")): widget.geometry()
                    for widget in self._ordered_widgets
                }
            )
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat(CARD_MIME_TYPE):
            card_id = self._decode_card_id(event) or self._active_card_id
            position = event.position().toPoint()
            target_index = self.insertion_index(card_id, position)
            moved_far_enough = (
                self._last_preview_point is None
                or (position - self._last_preview_point).manhattanLength() >= 10
            )
            if target_index != self._preview_index and moved_far_enough:
                self._preview_index = target_index
                self._last_preview_point = QPoint(position)
                self.cardPreviewed.emit(card_id, target_index)
            self.dragPositionChanged.emit(position)
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()

    def dropEvent(self, event) -> None:  # noqa: N802
        if not event.mimeData().hasFormat(CARD_MIME_TYPE):
            return
        card_id = self._decode_card_id(event)
        position = event.position().toPoint()
        target_index = (
            self._preview_index
            if card_id == self._active_card_id and self._preview_index is not None
            else self.insertion_index(card_id, position)
        )
        self.cardDropped.emit(card_id, target_index)
        self._reset_drag_state()
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        # 离开画布不立即撤销预排，允许用户滑到滚动条附近后再回来。
        event.accept()

    def _decode_card_id(self, event) -> str:
        return bytes(event.mimeData().data(CARD_MIME_TYPE)).decode(
            "utf-8", errors="ignore"
        )

    def _reset_drag_state(self) -> None:
        self._active_card_id = ""
        self._preview_index = None
        self._last_preview_point = None

    def cancel_drag(self) -> None:
        self._reset_drag_state()

    def insertion_index(self, card_id: str, position: QPoint) -> int:
        """按真实网格行和左右中心点计算插入位置。

        旧逻辑只有鼠标 y 坐标与中心点完全相等时才比较 x，导致同一行横向拖动
        经常整行跳转。这里先按网格行分组，再在当前行内判断左右位置。
        """
        targets = [
            widget
            for widget in self._ordered_widgets
            if str(getattr(widget, "card_id", "")) != card_id
        ]
        entries: list[tuple[int, int, QWidget, QRect]] = []
        for fallback_order, widget in enumerate(targets):
            card_key = str(getattr(widget, "card_id", ""))
            rect = (
                QRect(self._drop_slots.get(card_key, widget.geometry()))
                if self._active_card_id
                else QRect(widget.geometry())
            )
            layout_index = self.grid.indexOf(widget)
            if layout_index >= 0:
                row, column, _row_span, _column_span = self.grid.getItemPosition(
                    layout_index
                )
            else:
                row, column = fallback_order, 0
            entries.append((row, column, widget, rect))
        entries.sort(key=lambda item: (item[0], item[1]))

        consumed = 0
        rows: dict[int, list[tuple[int, int, QWidget, QRect]]] = {}
        for entry in entries:
            rows.setdefault(entry[0], []).append(entry)
        for row_number in sorted(rows):
            row_entries = rows[row_number]
            row_midpoint = sum(
                entry[3].center().y() for entry in row_entries
            ) // max(1, len(row_entries))
            if position.y() <= row_midpoint:
                for offset, entry in enumerate(row_entries):
                    if position.x() < entry[3].center().x():
                        return consumed + offset
                return consumed + len(row_entries)
            consumed += len(row_entries)
        return len(entries)


class CardChatDialog(QDialog):
    """围绕当前卡片内容继续追问的轻量对话框。"""

    messageSubmitted = Signal(str)
    saveRequested = Signal()

    def __init__(self, card_name: str, current_content: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"对话 · {card_name}")
        self.resize(680, 560)
        self.messages: list[dict[str, str]] = []
        self.latest_reply = ""
        self._busy = False

        root = QVBoxLayout(self)
        title = QLabel(f"围绕「{card_name}」继续对话")
        title.setObjectName("cardBoardTitle")
        root.addWidget(title)
        hint = QLabel("对话只使用这张卡片当前内容和它的相关语料；不会把整个语音库发送出去。")
        hint.setWordWrap(True)
        hint.setObjectName("cardBoardSubtitle")
        root.addWidget(hint)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setObjectName("cardChatView")
        preview = str(current_content or "").strip()
        if preview:
            self.view.setPlainText("当前卡片资料已载入。你可以直接提问，例如：\n\n· 把其中最重要的三点展开\n· 哪些内容还缺少依据？")
        else:
            self.view.setPlainText("这张卡片还没有生成内容，请先生成后再开始对话。")
        root.addWidget(self.view, 1)

        self.input = QPlainTextEdit()
        self.input.setPlaceholderText("输入你想继续追问或修改的内容…")
        self.input.setMaximumHeight(110)
        self.input.textChanged.connect(self._sync_send_button)
        root.addWidget(self.input)
        actions = QHBoxLayout()
        self.send_button = QPushButton("发送")
        self.send_button.setObjectName("cardBoardPrimary")
        self.send_button.clicked.connect(self._submit)
        self.save_button = QPushButton("保存到卡片")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self.saveRequested.emit)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.close)
        actions.addWidget(self.send_button)
        actions.addWidget(self.save_button)
        actions.addStretch(1)
        actions.addWidget(close_button)
        root.addLayout(actions)

    def _submit(self) -> None:
        text = self.input.toPlainText().strip()
        if not text:
            return
        self.messages.append({"role": "user", "content": text})
        self.view.appendPlainText(f"\n你：\n{text}")
        self.input.clear()
        self.send_button.setEnabled(False)
        self.messageSubmitted.emit(text)

    def _sync_send_button(self) -> None:
        self.send_button.setEnabled(not self._busy and bool(self.input.toPlainText().strip()))

    def set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        self.input.setEnabled(not busy)
        self._sync_send_button()
        if not busy and self.latest_reply:
            self.save_button.setEnabled(True)

    def add_reply(self, reply: str) -> None:
        text = str(reply or "").strip()
        if not text:
            return
        self.messages.append({"role": "assistant", "content": text})
        self.latest_reply = text
        self.view.appendPlainText(f"\n声年：\n{text}")
        self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().maximum())
        self.save_button.setEnabled(True)
        self.set_busy(False)


class CardFrame(QFrame):
    usefulRequested = Signal(str)
    generateRequested = Signal(str)
    chatRequested = Signal(str)
    reviseRequested = Signal(str)
    manualEditRequested = Signal(str)
    settingsRequested = Signal(str)
    historyRequested = Signal(str)
    preferencesRequested = Signal(str)
    undoRequested = Signal(str)
    redoRequested = Signal(str)
    initialRequested = Signal(str)
    convertToTodoRequested = Signal(str)
    shortVideoRequested = Signal(str)
    reorderRequested = Signal(str, int)
    renameRequested = Signal(str)
    timeRangeRequested = Signal(str)
    widthRequested = Signal(str, str)
    layoutWidthPreviewRequested = Signal(str, str)
    layoutResizeRequested = Signal(str, int, str)
    hideRequested = Signal(str)
    deleteRequested = Signal(str)
    dragStarted = Signal(str)
    dragFinished = Signal(str, bool)

    def __init__(
        self,
        card: Any,
        content: str = "",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.card = card
        self.card_id = str(_read(card, "card_id", ""))
        self.is_default = _is_default(card)
        self.is_structured = str(
            _read(card, "output_type", "") or ""
        ) in {"structured_todos", "structured_done"}
        self.setObjectName("cardFrame")
        self.setProperty("busy", False)
        self.setProperty("useful", False)
        self.setProperty("dragging", False)
        self._drag_opacity_effect: QGraphicsOpacityEffect | None = None
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.card_width = (
            "full"
            if str(_read(card, "width", "standard") or "standard") == "full"
            else "standard"
        )
        self.card_height = max(
            MIN_CARD_HEIGHT,
            min(
                MAX_CARD_HEIGHT,
                int(
                    _read(card, "height", DEFAULT_CARD_HEIGHT)
                    or DEFAULT_CARD_HEIGHT
                ),
            ),
        )
        self.setFixedHeight(self.card_height)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 12)
        root.setSpacing(9)

        header = QHBoxLayout()
        header.setSpacing(8)
        self.drag_handle = DragHandle()
        self.drag_handle.dragRequested.connect(self._start_drag)
        header.addWidget(self.drag_handle)

        title_box = QVBoxLayout()
        title_box.setSpacing(1)
        self.title_label = QLabel(str(_read(card, "name", "未命名卡片")))
        self.title_label.setObjectName("cardTitle")
        self.title_label.setTextFormat(Qt.TextFormat.PlainText)
        self.title_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        title_box.addWidget(self.title_label)
        self.updated_label = QLabel(
            f"更新于 {_format_time(_read(card, 'updated_at', ''))}"
        )
        self.updated_label.setObjectName("cardMeta")
        title_box.addWidget(self.updated_label)
        header.addLayout(title_box, 1)

        self.more_button = QToolButton()
        self.more_button.setText("•••")
        self.more_button.setObjectName("cardAction")
        self.more_button.setToolTip("更多操作")
        self.more_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.more_menu = QMenu(self.more_button)
        self._build_more_menu()
        self.more_button.setMenu(self.more_menu)
        header.addWidget(self.more_button)
        root.addLayout(header)

        scope_row = QHBoxLayout()
        scope_row.setSpacing(6)
        self.scope_label = QLabel(_source_label(card))
        self.scope_label.setObjectName("cardScope")
        self.scope_label.setTextFormat(Qt.TextFormat.PlainText)
        self.scope_label.setSizePolicy(
            QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred
        )
        scope_row.addWidget(self.scope_label, 0, Qt.AlignmentFlag.AlignLeft)
        self.size_label = QLabel(
            "大卡片" if self.card_width == "full" else "标准卡片"
        )
        self.size_label.setObjectName("cardSizeBadge")
        self.size_label.setToolTip(
            "可在右上角“••• → 卡片大小”中切换"
        )
        scope_row.addWidget(self.size_label, 0, Qt.AlignmentFlag.AlignLeft)
        scope_row.addStretch(1)
        root.addLayout(scope_row)

        self.body_stack = QStackedWidget()
        self.body_stack.setObjectName("cardBodyStack")
        self.content_editor = QPlainTextEdit(render_card_content(content))
        self.content_editor.setObjectName("cardContent")
        self.content_editor.setReadOnly(True)
        self.content_editor.setPlaceholderText("还没有内容。点击“生成”开始整理。")
        self.content_editor.setMinimumHeight(115)
        self.body_stack.addWidget(self.content_editor)
        root.addWidget(self.body_stack, 1)
        self._body_widget: QWidget = self.content_editor
        self._owns_body_widget = True

        footer = QHBoxLayout()
        footer.setSpacing(6)
        # 首页只保留最高频的“生成”。确认、修改等低频能力放进更多菜单，
        # 避免每张卡片底部出现一排需要用户理解的按钮。
        self.useful_button = None
        self.revise_button = None

        self.generate_button = QPushButton("生成")
        self.generate_button.setObjectName("cardAction")
        self.generate_button.setProperty("action", "generate")
        self.generate_button.clicked.connect(
            lambda: self.generateRequested.emit(self.card_id)
        )
        if (
            (not self.is_structured or self.card_id == "todos")
            and self.card_id != "short_video"
        ):
            footer.addWidget(self.generate_button)

        chat_label = "对话添加" if self.card_id == "todos" else "对话"
        self.chat_button = QPushButton(chat_label)
        self.chat_button.setObjectName("cardAction")
        self.chat_button.setToolTip(
            "说说今天要做什么，声年会先整理成待办供你确认"
            if self.card_id == "todos"
            else "基于当前卡片内容继续和声年对话"
        )
        self.chat_button.clicked.connect(
            lambda: self.chatRequested.emit(self.card_id)
        )
        footer.addWidget(self.chat_button)

        self.video_button = None
        footer.addStretch(1)
        disclaimer = QLabel("AI 生成 · 使用前请核实")
        disclaimer.setObjectName("cardDisclaimer")
        if not self.is_structured:
            footer.addWidget(disclaimer)
        root.addLayout(footer)

        self.resize_bar = CardResizeBar(
            self,
            height=self.card_height,
            width=self.card_width,
        )
        self.resize_bar.widthPreviewed.connect(self._preview_width)
        self.resize_bar.resizeCommitted.connect(self._commit_resize)
        root.addWidget(self.resize_bar)

    def _build_more_menu(self) -> None:
        rename_action = QAction("重命名", self.more_menu)
        rename_action.triggered.connect(
            lambda _checked=False: self.renameRequested.emit(self.card_id)
        )
        self.more_menu.addAction(rename_action)
        if not self.is_structured:
            time_action = QAction("读取语料的时间", self.more_menu)
            time_action.triggered.connect(
                lambda _checked=False: self.timeRangeRequested.emit(
                    self.card_id
                )
            )
            self.more_menu.addAction(time_action)

        actions: tuple[tuple[str, Signal], ...] = ()
        if not self.is_structured and self.card_id != "short_video":
            actions = (("版本管理", self.historyRequested),)
        for text, signal in actions:
            action = QAction(text, self.more_menu)
            action.triggered.connect(
                lambda _checked=False, bound=signal: bound.emit(self.card_id)
            )
            self.more_menu.addAction(action)
        self.size_menu = self.more_menu.addMenu("卡片大小")
        self.size_actions: dict[str, QAction] = {}
        for text, width in (
            ("标准卡片 · 半宽", "standard"),
            ("大卡片 · 通栏", "full"),
        ):
            action = QAction(text, self.size_menu)
            action.setCheckable(True)
            action.setChecked(self.card_width == width)
            action.triggered.connect(
                lambda _checked=False, value=width: self.widthRequested.emit(
                    self.card_id, value
                )
            )
            self.size_menu.addAction(action)
            self.size_actions[width] = action
        self.more_menu.addSeparator()
        hide_action = QAction("隐藏卡片", self.more_menu)
        hide_action.triggered.connect(
            lambda _checked=False: self.hideRequested.emit(self.card_id)
        )
        self.more_menu.addAction(hide_action)
        if not self.is_default:
            delete_action = QAction("删除卡片", self.more_menu)
            delete_action.triggered.connect(
                lambda _checked=False: self.deleteRequested.emit(self.card_id)
            )
            self.more_menu.addAction(delete_action)

    def _commit_resize(self, height: int, width: str) -> None:
        self.card_height = height
        self._set_width_presentation(width)
        self.layoutResizeRequested.emit(self.card_id, height, width)

    def _preview_width(self, width: str) -> None:
        if width == self.card_width:
            return
        self._set_width_presentation(width)
        self.layoutWidthPreviewRequested.emit(self.card_id, width)

    def _set_width_presentation(self, width: str) -> None:
        self.card_width = "full" if width == "full" else "standard"
        self.size_label.setText(
            "大卡片" if self.card_width == "full" else "标准卡片"
        )
        for value, action in self.size_actions.items():
            action.setChecked(value == self.card_width)

    def _mark_useful(self, checked: bool) -> None:
        self.setProperty("useful", checked)
        self.style().unpolish(self)
        self.style().polish(self)
        if checked:
            self.usefulRequested.emit(self.card_id)

    def _start_drag(self) -> None:
        mime = QMimeData()
        mime.setData(CARD_MIME_TYPE, QByteArray(self.card_id.encode("utf-8")))
        drag = QDrag(self)
        drag.setMimeData(mime)
        preview = self.grab()
        preview = preview.scaled(
            QSize(520, 280),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        drag.setPixmap(preview)
        drag.setHotSpot(
            QPoint(
                min(34, max(0, preview.width() - 1)),
                min(24, max(0, preview.height() - 1)),
            )
        )
        self.dragStarted.emit(self.card_id)
        self._set_dragging(True)
        moved = False
        try:
            moved = drag.exec(Qt.DropAction.MoveAction) == Qt.DropAction.MoveAction
        finally:
            self._set_dragging(False)
            self.dragFinished.emit(self.card_id, moved)

    def _set_dragging(self, dragging: bool) -> None:
        self.setProperty("dragging", dragging)
        if dragging:
            effect = QGraphicsOpacityEffect(self)
            effect.setOpacity(0.38)
            self._drag_opacity_effect = effect
            self.setGraphicsEffect(effect)
        else:
            self.setGraphicsEffect(None)
            self._drag_opacity_effect = None
        self.style().unpolish(self)
        self.style().polish(self)

    def set_organize_mode(self, enabled: bool) -> None:
        # 兼容旧调用：排序与缩放始终直接可用，不再依赖整理模式。
        self.drag_handle.setVisible(True)
        self.resize_bar.setVisible(True)

    def set_busy(self, busy: bool) -> None:
        self.setProperty("busy", busy)
        self.more_button.setEnabled(not busy)
        if self.useful_button is not None:
            self.useful_button.setEnabled(not busy)
        if self.revise_button is not None:
            self.revise_button.setEnabled(not busy)
        self.generate_button.setEnabled(not busy)
        self.chat_button.setEnabled(not busy)
        if self.video_button is not None:
            self.video_button.setEnabled(not busy)
        self.drag_handle.setEnabled(not busy)
        self.resize_bar.setEnabled(not busy)
        self.style().unpolish(self)
        self.style().polish(self)

    def set_content(self, content: str) -> None:
        self.content_editor.setPlainText(render_card_content(content))

    def set_body_widget(self, widget: QWidget, *, owned: bool = False) -> None:
        if widget is self._body_widget:
            return
        if self._body_widget is not self.content_editor:
            previous = self._body_widget
            self.body_stack.removeWidget(previous)
            if self._owns_body_widget:
                previous.deleteLater()
            else:
                previous.setParent(None)
        if self.body_stack.indexOf(widget) < 0:
            self.body_stack.addWidget(widget)
        self.body_stack.setCurrentWidget(widget)
        self._body_widget = widget
        self._owns_body_widget = owned

    def take_body_widget(self) -> QWidget | None:
        if self._body_widget is self.content_editor or self._owns_body_widget:
            return None
        widget = self._body_widget
        self.body_stack.removeWidget(widget)
        widget.setParent(None)
        self._body_widget = self.content_editor
        self.body_stack.setCurrentWidget(self.content_editor)
        return widget

    def body_widget(self) -> QWidget:
        return self._body_widget


class AddCardDialog(QDialog):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setWindowTitle("添加卡片")
        self.resize(680, 600)
        self._skill_prompt = ""
        today = QDate.currentDate()
        self._custom_time_range = (
            f"custom:{today.addDays(-6).toString('yyyy-MM-dd')}:"
            f"{today.toString('yyyy-MM-dd')}"
        )
        layout = QVBoxLayout(self)
        title = QLabel("添加一张生成卡片")
        title.setObjectName("cardBoardTitle")
        layout.addWidget(title)
        hint = QLabel(
            "选择声年官方卡片、自己写提示词，或导入外部文字生成 Skill。"
            "创建后都可以继续改成自己的版本。"
        )
        hint.setWordWrap(True)
        hint.setObjectName("cardBoardSubtitle")
        layout.addWidget(hint)

        self.mode_tabs = QTabWidget()
        self.mode_tabs.setObjectName("cardCreateModes")

        official_page = QWidget()
        official_layout = QHBoxLayout(official_page)
        official_layout.setContentsMargins(10, 12, 10, 10)
        self.official_list = QListWidget()
        for index, template in enumerate(OFFICIAL_CARD_TEMPLATES):
            item = QListWidgetItem(
                f"{template['name']}\n{template['description']}"
            )
            item.setData(Qt.ItemDataRole.UserRole, index)
            self.official_list.addItem(item)
        official_layout.addWidget(self.official_list, 1)
        official_right = QVBoxLayout()
        official_note = QLabel(
            "官方卡片已经写好提示词。生成时只读取你选择时间范围内的本地语音记录。"
        )
        official_note.setWordWrap(True)
        official_note.setObjectName("cardBoardSubtitle")
        official_right.addWidget(official_note)
        self.official_preview = QPlainTextEdit()
        self.official_preview.setReadOnly(True)
        official_right.addWidget(self.official_preview, 1)
        official_layout.addLayout(official_right, 2)
        self.mode_tabs.addTab(official_page, "官方卡片")

        prompt_page = QWidget()
        prompt_layout = QVBoxLayout(prompt_page)
        prompt_layout.setContentsMargins(10, 12, 10, 10)
        prompt_hint = QLabel(
            "写下这张卡片要从本地语音知识库中寻找什么、怎么整理、输出什么。"
        )
        prompt_hint.setWordWrap(True)
        prompt_hint.setObjectName("cardBoardSubtitle")
        prompt_layout.addWidget(prompt_hint)
        self.description_edit = QPlainTextEdit()
        self.description_edit.setPlaceholderText(
            "例如：从最近 7 天的语音记录里，找出值得拍成短视频的反常识观点，每次给我 3 个。"
        )
        self.description_edit.setObjectName("cardDescription")
        prompt_layout.addWidget(self.description_edit, 1)
        self.mode_tabs.addTab(prompt_page, "自己写提示词")

        skill_page = QWidget()
        skill_layout = QVBoxLayout(skill_page)
        skill_layout.setContentsMargins(10, 12, 10, 10)
        skill_hint = QLabel(
            "选择本地 SKILL.md 或粘贴 HTTPS 地址。声年只读取其中的文字生成规则，"
            "不会执行脚本、命令、插件或联网操作。"
        )
        skill_hint.setWordWrap(True)
        skill_hint.setObjectName("cardBoardSubtitle")
        skill_layout.addWidget(skill_hint)
        self.skill_button = QPushButton("选择或读取外部 Skill…")
        self.skill_button.setObjectName("cardBoardSecondary")
        self.skill_button.clicked.connect(self._choose_skill)
        skill_layout.addWidget(self.skill_button)
        self.skill_drop_hint = QLabel(
            "也可以把下载好的 SKILL.md 直接拖到这个窗口"
        )
        self.skill_drop_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.skill_drop_hint.setObjectName("cardBoardDropZone")
        skill_layout.addWidget(self.skill_drop_hint)
        self.skill_status = QLabel("尚未选择 Skill")
        self.skill_status.setObjectName("cardBoardStatus")
        self.skill_status.setWordWrap(True)
        skill_layout.addWidget(self.skill_status)
        self.skill_preview = QPlainTextEdit()
        self.skill_preview.setReadOnly(True)
        self.skill_preview.setPlaceholderText("读取后会在这里预览安全过滤后的文字规则。")
        skill_layout.addWidget(self.skill_preview, 1)
        self.mode_tabs.addTab(skill_page, "导入外部 Skill")
        layout.addWidget(self.mode_tabs, 1)

        time_row = QHBoxLayout()
        time_label = QLabel("读取语料的时间")
        time_label.setObjectName("cardMeta")
        time_row.addWidget(time_label)
        self.time_combo = QComboBox()
        for label, code in (
            ("每天 · 今天", "today"),
            ("每周 · 最近 7 天", "last_7_days"),
            ("每月 · 最近 30 天", "last_30_days"),
            ("自定义日期", "custom"),
        ):
            self.time_combo.addItem(label, code)
        self.time_combo.setCurrentIndex(
            self.time_combo.findData("last_7_days")
        )
        time_row.addWidget(self.time_combo, 1)
        self.custom_time_button = QPushButton("选择日期…")
        self.custom_time_button.setObjectName("cardBoardSecondary")
        self.custom_time_button.clicked.connect(self._choose_custom_time)
        self.custom_time_button.hide()
        time_row.addWidget(self.custom_time_button)
        layout.addLayout(time_row)

        size_row = QHBoxLayout()
        size_label = QLabel("卡片大小")
        size_label.setObjectName("cardMeta")
        size_row.addWidget(size_label)
        self.width_combo = QComboBox()
        self.width_combo.addItem("标准卡片 · 半宽", "standard")
        self.width_combo.addItem("大卡片 · 通栏", "full")
        self.width_combo.setToolTip("标准卡片占半宽，大卡片通栏展示")
        size_row.addWidget(self.width_combo, 1)
        layout.addLayout(size_row)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Ok
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "添加卡片"
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self.description_edit.textChanged.connect(self._validate)
        self.mode_tabs.currentChanged.connect(self._validate)
        self.official_list.currentRowChanged.connect(
            self._official_template_changed
        )
        self.time_combo.currentIndexChanged.connect(self._time_mode_changed)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        if OFFICIAL_CARD_TEMPLATES:
            self.official_list.setCurrentRow(0)
        self._time_mode_changed()
        self._validate()

    def _validate(self) -> None:
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            bool(self.description())
        )

    def description(self) -> str:
        if self.source_mode() == "official":
            template = self.official_template()
            if not template:
                return ""
            return (
                f"名称：{template['name']}\n"
                "【声年官方卡片】\n"
                f"{template['prompt']}"
            )
        if self.source_mode() == "skill":
            return self._skill_prompt.strip()
        return self.description_edit.toPlainText().strip()

    def source_mode(self) -> str:
        return ("official", "prompt", "skill")[self.mode_tabs.currentIndex()]

    def official_template(self) -> dict[str, str] | None:
        item = self.official_list.currentItem()
        if item is None:
            return None
        index = int(item.data(Qt.ItemDataRole.UserRole))
        if not 0 <= index < len(OFFICIAL_CARD_TEMPLATES):
            return None
        return dict(OFFICIAL_CARD_TEMPLATES[index])

    def _official_template_changed(self, _row: int) -> None:
        template = self.official_template()
        if not template:
            self.official_preview.clear()
            self._validate()
            return
        self.official_preview.setPlainText(template["prompt"])
        index = self.time_combo.findData(template["time_range"])
        if index >= 0:
            self.time_combo.setCurrentIndex(index)
        self._validate()

    def _time_mode_changed(self, _index: int = -1) -> None:
        custom = self.time_combo.currentData() == "custom"
        self.custom_time_button.setVisible(custom)
        if custom:
            _, start, end = self._custom_time_range.split(":", 2)
            self.custom_time_button.setText(f"{start} 至 {end}")

    def _choose_custom_time(self) -> None:
        dialog = SourceTimeRangeDialog(
            "新卡片",
            self._custom_time_range,
            self,
        )
        dialog.range_combo.setCurrentIndex(
            dialog.range_combo.findData("custom")
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._custom_time_range = dialog.value()
            self._time_mode_changed()

    def time_range(self) -> str:
        selected = str(self.time_combo.currentData() or "last_7_days")
        return self._custom_time_range if selected == "custom" else selected

    def _choose_skill(self) -> None:
        self._open_skill_import()

    def _open_skill_import(self, initial_source: str = "") -> None:
        dialog = SkillImportDialog(self, initial_source=initial_source)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        prompt = dialog.imported_prompt().strip()
        if not prompt:
            return
        self._skill_prompt = prompt
        name = dialog.imported_name() or "外部 Skill"
        self.skill_status.setText(
            f"已读取「{name}」。创建后可直接在卡片上点击“生成”。"
        )
        self.skill_preview.setPlainText(prompt)
        self._validate()

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if _dropped_skill_path(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802
        path = _dropped_skill_path(event.mimeData())
        if not path:
            event.ignore()
            return
        event.acceptProposedAction()
        self.mode_tabs.setCurrentIndex(2)
        QTimer.singleShot(0, lambda: self._open_skill_import(path))

    def card_width(self) -> str:
        return str(self.width_combo.currentData() or "standard")


class ReviseCardDialog(QDialog):
    def __init__(self, card_name: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"修改「{card_name}」")
        self.resize(500, 300)
        layout = QVBoxLayout(self)
        title = QLabel("告诉声年，哪里需要改")
        title.setObjectName("cardBoardTitle")
        layout.addWidget(title)
        self.instruction_edit = QPlainTextEdit()
        self.instruction_edit.setPlaceholderText(
            "例如：删掉重复内容，保留今天真正需要我决定的三件事。"
        )
        layout.addWidget(self.instruction_edit, 1)
        self.durable_check = QCheckBox("以后这张卡片都按这个要求生成")
        self.durable_check.setToolTip("只有明确的长期要求才会被记住")
        layout.addWidget(self.durable_check)
        self.instruction_edit.textChanged.connect(self._suggest_scope)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Ok
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("开始修改")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _suggest_scope(self) -> None:
        self.durable_check.setChecked(_looks_durable(self.instruction()))

    def instruction(self) -> str:
        return self.instruction_edit.toPlainText().strip()

    def durable(self) -> bool:
        return self.durable_check.isChecked()


class ContentEditDialog(QDialog):
    def __init__(
        self, card_name: str, content: str, parent: QWidget | None = None
    ):
        super().__init__(parent)
        self.setWindowTitle(f"手动编辑「{card_name}」")
        self.resize(650, 520)
        layout = QVBoxLayout(self)
        label = QLabel("你的修改会作为一个新版本保存，可以随时撤销。")
        label.setObjectName("cardBoardSubtitle")
        layout.addWidget(label)
        self.editor = QPlainTextEdit(content)
        layout.addWidget(self.editor, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Save
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存版本")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def content(self) -> str:
        return self.editor.toPlainText()


class SkillImportDialog(QDialog):
    """把本地或在线 SKILL.md 安全转换成可编辑的文字 Prompt。"""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        initial_source: str = "",
    ):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setWindowTitle("导入文字生成 Skill")
        self.resize(680, 560)
        self._skill = None
        self.runner = AsyncTaskRunner(self, max_workers=1)

        root = QVBoxLayout(self)
        title = QLabel("导入 Skill 的文字规则")
        title.setObjectName("cardBoardTitle")
        root.addWidget(title)
        note = QLabel(
            "可以选择本地 SKILL.md，或粘贴 HTTPS 地址。声年只读取其中的文字生成规则，"
            "不会执行脚本、命令、工具、插件、联网操作或自动发布。"
        )
        note.setWordWrap(True)
        note.setObjectName("cardBoardSubtitle")
        root.addWidget(note)
        drop_hint = QLabel("把下载好的 SKILL.md 拖到此窗口即可读取")
        drop_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        drop_hint.setObjectName("cardBoardDropZone")
        root.addWidget(drop_hint)

        source_row = QHBoxLayout()
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText("https://…/SKILL.md，或本地文件路径")
        self.source_edit.textChanged.connect(self._source_changed)
        source_row.addWidget(self.source_edit, 1)
        browse = QPushButton("选择本地文件")
        browse.setObjectName("cardBoardSecondary")
        browse.clicked.connect(self._browse)
        source_row.addWidget(browse)
        self.read_button = QPushButton("读取预览")
        self.read_button.setObjectName("cardBoardPrimary")
        self.read_button.clicked.connect(self._load)
        source_row.addWidget(self.read_button)
        root.addLayout(source_row)

        self.status = QLabel("尚未读取")
        self.status.setObjectName("cardBoardStatus")
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText("读取后会在这里显示即将导入的安全文本。")
        root.addWidget(self.preview, 1)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Ok
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "使用这个 Skill"
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)
        if initial_source:
            self.source_edit.setText(initial_source)
            QTimer.singleShot(0, self._load)

    def _source_changed(self) -> None:
        self._skill = None
        self.preview.clear()
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)

    def _browse(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "选择 SKILL.md",
            "",
            "Skill Markdown (SKILL.md *.md *.markdown)",
        )
        if path:
            self.source_edit.setText(path)
            self._load()

    def _set_busy(self, busy: bool) -> None:
        self.source_edit.setEnabled(not busy)
        self.read_button.setEnabled(not busy and bool(self.source_edit.text().strip()))
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            not busy and self._skill is not None
        )

    def _load(self) -> None:
        source = self.source_edit.text().strip()
        if not source:
            self.status.setText("请先粘贴地址或选择本地 SKILL.md。")
            return
        self._set_busy(True)
        self.status.setText("正在安全读取 Skill…")
        self.runner.submit(
            "skill:load",
            lambda: load_skill(source),
            self._loaded,
            self._failed,
        )

    def _loaded(self, skill: Any) -> None:
        self._skill = skill
        removed = int(getattr(skill, "removed_sections", 0)) + int(
            getattr(skill, "removed_lines", 0)
        )
        license_text = getattr(skill, "license", "") or "未声明"
        self.status.setText(
            f"已读取：{getattr(skill, 'name', 'Skill')} · 许可证：{license_text}"
            + (f" · 已移除 {removed} 处非文字生成内容" if removed else "")
        )
        self.preview.setPlainText(skill.prompt_text())
        self._set_busy(False)

    def _failed(self, message: str) -> None:
        self._skill = None
        self.status.setText(f"读取失败：{message}")
        self.preview.clear()
        self._set_busy(False)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if _dropped_skill_path(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802
        path = _dropped_skill_path(event.mimeData())
        if not path:
            event.ignore()
            return
        event.acceptProposedAction()
        self.source_edit.setText(path)
        self.status.setText("已接收拖入的 SKILL.md，正在读取…")
        self._load()

    def imported_prompt(self) -> str:
        return self._skill.prompt_text() if self._skill is not None else ""

    def imported_name(self) -> str:
        return str(getattr(self._skill, "name", "") or "").strip()


class CardSettingsDialog(QDialog):
    def __init__(self, card: Any, parent: QWidget | None = None):
        super().__init__(parent)
        self.card = card
        self.setWindowTitle(f"设置「{_read(card, 'name', '卡片')}」")
        self.resize(640, 570)
        root = QVBoxLayout(self)
        title = QLabel("用你自己的话规定这张卡片")
        title.setObjectName("cardBoardTitle")
        root.addWidget(title)
        form = QFormLayout()
        self.name_edit = QLineEdit(str(_read(card, "name", "") or ""))
        self.name_edit.setEnabled(not _is_default(card))
        form.addRow("卡片名称", self.name_edit)
        sources_box = QWidget()
        sources_layout = QHBoxLayout(sources_box)
        sources_layout.setContentsMargins(0, 0, 0, 0)
        sources_layout.setSpacing(8)
        selected_sources = set(_read(card, "sources", ()) or ())
        dependencies = tuple(_read(card, "dependencies", ()) or ())
        self.source_checks: dict[str, QCheckBox] = {}
        for code, label in (
            ("transcripts", "语音记录"),
            ("imports", "导入资料"),
            ("todos", "待办"),
            ("done", "已办"),
            ("confirmed_cards", "已确认卡片"),
        ):
            check = QCheckBox(label)
            check.setChecked(code in selected_sources)
            if code == "confirmed_cards" and not dependencies:
                check.setEnabled(False)
                check.setToolTip("关联其他卡片需要在创建卡片时明确指定")
            self.source_checks[code] = check
            sources_layout.addWidget(check)
        sources_layout.addStretch(1)
        form.addRow("资料来源", sources_box)
        self.range_combo = QComboBox()
        for label, code in (
            ("今天", "today"),
            ("昨天", "yesterday"),
            ("最近7天", "last_7_days"),
            ("最近30天", "last_30_days"),
            ("全部历史", "all"),
        ):
            self.range_combo.addItem(label, code)
        current_range = str(_read(card, "time_range", "") or "")
        range_index = self.range_combo.findData(current_range)
        self.range_combo.setCurrentIndex(max(0, range_index))
        self.range_combo.setToolTip("第一版提供五档受控时间范围")
        form.addRow("时间范围", self.range_combo)
        self.trigger_combo = QComboBox()
        self.trigger_combo.addItem("手动更新", "manual")
        self.trigger_combo.addItem(
            "每天自动更新（使用你自己的 API）", "daily"
        )
        trigger_index = self.trigger_combo.findData(
            str(_read(card, "trigger_mode", "manual"))
        )
        self.trigger_combo.setCurrentIndex(max(0, trigger_index))
        form.addRow("更新方式", self.trigger_combo)
        self.width_combo = QComboBox()
        self.width_combo.addItem("标准卡片 · 半宽", "standard")
        self.width_combo.addItem("大卡片 · 通栏", "full")
        width_index = self.width_combo.findData(
            "full" if str(_read(card, "width", "standard")) == "full" else "standard"
        )
        self.width_combo.setCurrentIndex(max(0, width_index))
        form.addRow("卡片大小", self.width_combo)
        root.addLayout(form)
        rules_label = QLabel("生成要求")
        rules_label.setObjectName("cardMeta")
        root.addWidget(rules_label)
        self.rules_edit = QPlainTextEdit(str(_read(card, "rules", "") or ""))
        self.rules_edit.setPlaceholderText(
            "例如：只保留有行动价值的内容，不要重复，不确定的事实明确标注。"
        )
        root.addWidget(self.rules_edit, 1)
        advanced_row = QHBoxLayout()
        self.advanced_check = QCheckBox("显示高级 Prompt")
        advanced_row.addWidget(self.advanced_check)
        advanced_row.addStretch(1)
        self.import_skill_button = QPushButton("导入 Skill…")
        self.import_skill_button.setObjectName("cardBoardSecondary")
        self.import_skill_button.setToolTip(
            "只导入文字生成规则，不执行 Skill 中的脚本、命令或工具"
        )
        self.import_skill_button.clicked.connect(self._import_skill)
        advanced_row.addWidget(self.import_skill_button)
        root.addLayout(advanced_row)
        self.prompt_edit = QPlainTextEdit(
            str(_read(card, "user_prompt", "") or "")
        )
        self.prompt_edit.setPlaceholderText(
            "只影响用户表达与输出要求，不能覆盖真实性、隐私和安全规则。"
        )
        self.prompt_edit.hide()
        self.advanced_check.toggled.connect(self.prompt_edit.setVisible)
        root.addWidget(self.prompt_edit, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Save
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存规则")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _import_skill(self) -> None:
        dialog = SkillImportDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        prompt = dialog.imported_prompt()
        if not prompt:
            return
        self.prompt_edit.setPlainText(prompt)
        self.advanced_check.setChecked(True)
        self.prompt_edit.setFocus()

    def values(self) -> dict[str, Any]:
        sources = tuple(
            code for code, check in self.source_checks.items() if check.isChecked()
        )
        values: dict[str, Any] = {
            "sources": sources,
            "time_range": str(self.range_combo.currentData()),
            "trigger_mode": str(self.trigger_combo.currentData()),
            "width": str(self.width_combo.currentData() or "standard"),
            "rules": self.rules_edit.toPlainText().strip(),
            "user_prompt": self.prompt_edit.toPlainText().strip(),
        }
        if not _is_default(self.card):
            values["name"] = self.name_edit.text().strip()
        return values


class SourceTimeRangeDialog(QDialog):
    """只设置一张卡片读取语料的日期范围。"""

    def __init__(
        self,
        card_name: str,
        current_range: str,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle(f"读取语料的时间 · {card_name}")
        self.resize(480, 250)
        root = QVBoxLayout(self)
        note = QLabel(
            "声年只会从所选时间范围内检索语音记录；修改后在下一次生成时生效。"
        )
        note.setWordWrap(True)
        note.setObjectName("cardBoardSubtitle")
        root.addWidget(note)

        form = QFormLayout()
        self.range_combo = QComboBox()
        for label, code in (
            ("每天 · 今天的语料", "today"),
            ("昨天 · 昨天的语料", "yesterday"),
            ("每周 · 最近 7 天", "last_7_days"),
            ("每月 · 最近 30 天", "last_30_days"),
            ("自定义日期", "custom"),
        ):
            self.range_combo.addItem(label, code)
        form.addRow("读取语料的时间", self.range_combo)

        self.custom_row = QWidget()
        dates = QHBoxLayout(self.custom_row)
        dates.setContentsMargins(0, 0, 0, 0)
        self.start_date = QDateEdit()
        self.start_date.setCalendarPopup(True)
        self.start_date.setDisplayFormat("yyyy-MM-dd")
        self.end_date = QDateEdit()
        self.end_date.setCalendarPopup(True)
        self.end_date.setDisplayFormat("yyyy-MM-dd")
        today = QDate.currentDate()
        self.start_date.setMaximumDate(today)
        self.end_date.setMaximumDate(today)
        self.start_date.setDate(today.addDays(-6))
        self.end_date.setDate(today)
        dates.addWidget(self.start_date)
        dates.addWidget(QLabel("至"))
        dates.addWidget(self.end_date)
        form.addRow("自定义范围", self.custom_row)
        root.addLayout(form)

        custom_match = re.fullmatch(
            r"custom:(\d{4}-\d{2}-\d{2}):(\d{4}-\d{2}-\d{2})",
            current_range,
        )
        selected = "custom" if custom_match else current_range
        index = self.range_combo.findData(selected)
        self.range_combo.setCurrentIndex(max(0, index))
        if custom_match:
            self.start_date.setDate(
                QDate.fromString(custom_match.group(1), "yyyy-MM-dd")
            )
            self.end_date.setDate(
                QDate.fromString(custom_match.group(2), "yyyy-MM-dd")
            )

        self.validation_label = QLabel("")
        self.validation_label.setObjectName("cardBoardStatus")
        root.addWidget(self.validation_label)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Save
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText(
            "保存时间范围"
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(
            "取消"
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)
        self.range_combo.currentIndexChanged.connect(self._sync_state)
        self.start_date.dateChanged.connect(self._sync_state)
        self.end_date.dateChanged.connect(self._sync_state)
        self._sync_state()

    def _sync_state(self) -> None:
        custom = self.range_combo.currentData() == "custom"
        self.custom_row.setVisible(custom)
        valid = not custom or self.start_date.date() <= self.end_date.date()
        self.validation_label.setText(
            "" if valid else "开始日期不能晚于结束日期。"
        )
        self.buttons.button(
            QDialogButtonBox.StandardButton.Save
        ).setEnabled(valid)

    def value(self) -> str:
        selected = str(self.range_combo.currentData() or "today")
        if selected != "custom":
            return selected
        return (
            f"custom:{self.start_date.date().toString('yyyy-MM-dd')}:"
            f"{self.end_date.date().toString('yyyy-MM-dd')}"
        )


class RevisionHistoryDialog(QDialog):
    revisionRestoreRequested = Signal(str)
    promptSaveRequested = Signal(str)

    def __init__(
        self,
        card_name: str,
        revisions: list[Any],
        can_restore: bool,
        prompt_text: str = "",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle(f"版本管理 · {card_name}")
        self.resize(760, 560)
        self.revisions = revisions
        root = QVBoxLayout(self)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        versions_page = QWidget()
        layout = QHBoxLayout(versions_page)
        self.list_widget = QListWidget()
        for index, revision in enumerate(revisions):
            kind = str(_read(revision, "kind", "版本"))
            created = _format_time(_read(revision, "created_at", ""))
            item = QListWidgetItem(f"{index + 1}. {kind} · {created}")
            item.setData(Qt.ItemDataRole.UserRole, _revision_id(revision))
            self.list_widget.addItem(item)
        layout.addWidget(self.list_widget, 1)
        right = QVBoxLayout()
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        right.addWidget(self.preview, 1)
        self.restore_button = QPushButton("恢复为当前版本")
        self.restore_button.setObjectName("cardBoardPrimary")
        self.restore_button.setEnabled(can_restore and bool(revisions))
        self.restore_button.clicked.connect(self._restore)
        right.addWidget(self.restore_button)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        right.addWidget(close)
        layout.addLayout(right, 2)
        self.tabs.addTab(versions_page, "生成结果版本")

        prompt_page = QWidget()
        prompt_layout = QVBoxLayout(prompt_page)
        prompt_note = QLabel(
            "这是这张卡片生成内容时实际使用的用户提示词。可以直接修改；"
            "保存后会作为新的规则版本，从下一次生成开始生效。"
        )
        prompt_note.setWordWrap(True)
        prompt_note.setObjectName("cardBoardSubtitle")
        prompt_layout.addWidget(prompt_note)
        self.prompt_edit = QPlainTextEdit(prompt_text)
        self.prompt_edit.setPlaceholderText(
            "写清楚要从语音记录里寻找什么、如何判断、按什么格式输出。"
        )
        prompt_layout.addWidget(self.prompt_edit, 1)
        prompt_buttons = QHBoxLayout()
        prompt_buttons.addStretch(1)
        self.prompt_save_button = QPushButton("保存提示词")
        self.prompt_save_button.setObjectName("cardBoardPrimary")
        self.prompt_save_button.clicked.connect(self._save_prompt)
        prompt_buttons.addWidget(self.prompt_save_button)
        prompt_layout.addLayout(prompt_buttons)
        self.tabs.addTab(prompt_page, "生成提示词")

        self.list_widget.currentRowChanged.connect(self._preview)
        self.prompt_edit.textChanged.connect(self._sync_prompt_button)
        if revisions:
            self.list_widget.setCurrentRow(len(revisions) - 1)
        self._sync_prompt_button()

    def _preview(self, row: int) -> None:
        if 0 <= row < len(self.revisions):
            self.preview.setPlainText(
                render_card_content(_content_of(self.revisions[row]))
            )

    def _restore(self) -> None:
        item = self.list_widget.currentItem()
        if not item:
            return
        revision_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
        if revision_id:
            self.revisionRestoreRequested.emit(revision_id)
            self.accept()

    def _sync_prompt_button(self) -> None:
        self.prompt_save_button.setEnabled(
            bool(self.prompt_edit.toPlainText().strip())
        )

    def _save_prompt(self) -> None:
        prompt = self.prompt_edit.toPlainText().strip()
        if not prompt:
            return
        self.promptSaveRequested.emit(prompt)
        self.prompt_save_button.setText("已保存")


class PreferencesDialog(QDialog):
    preferenceRevokeRequested = Signal(str)

    def __init__(
        self,
        card_name: str,
        preferences: list[Any],
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle(f"「{card_name}」已记住的要求")
        self.resize(620, 380)
        self.preferences = preferences
        root = QVBoxLayout(self)
        note = QLabel(
            "只有你明确选择“以后都……”并确认有用的要求才会出现在这里。"
        )
        note.setWordWrap(True)
        root.addWidget(note)
        self.list_widget = QListWidget()
        for preference in preferences:
            item = QListWidgetItem(
                str(_read(preference, "rule_text", "") or "")
            )
            item.setData(
                Qt.ItemDataRole.UserRole,
                str(_read(preference, "preference_id", "") or ""),
            )
            self.list_widget.addItem(item)
        if not preferences:
            self.list_widget.addItem("还没有记住任何长期要求")
            self.list_widget.setEnabled(False)
        root.addWidget(self.list_widget, 1)
        buttons = QHBoxLayout()
        revoke = QPushButton("不再记住选中要求")
        revoke.setEnabled(bool(preferences))
        revoke.clicked.connect(self._revoke)
        buttons.addWidget(revoke)
        buttons.addStretch(1)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        root.addLayout(buttons)

    def _revoke(self) -> None:
        item = self.list_widget.currentItem()
        if item is None:
            return
        preference_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
        if not preference_id:
            return
        self.preferenceRevokeRequested.emit(preference_id)
        self.accept()


class RecycleBinDialog(QDialog):
    restoreRequested = Signal(str)

    def __init__(self, cards: list[Any], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("卡片回收站")
        self.resize(480, 360)
        layout = QVBoxLayout(self)
        note = QLabel("自定义卡片删除后在本机保留30天。")
        note.setObjectName("cardBoardSubtitle")
        layout.addWidget(note)
        self.list_widget = QListWidget()
        for card in cards:
            item = QListWidgetItem(
                f"{_read(card, 'name', '未命名卡片')} · "
                f"{_format_time(_read(card, 'deleted_at', ''))}"
            )
            item.setData(Qt.ItemDataRole.UserRole, str(_read(card, "card_id", "")))
            self.list_widget.addItem(item)
        layout.addWidget(self.list_widget, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        restore = buttons.addButton(
            "恢复卡片", QDialogButtonBox.ButtonRole.AcceptRole
        )
        restore.setEnabled(bool(cards))
        restore.clicked.connect(self._restore)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _restore(self) -> None:
        item = self.list_widget.currentItem()
        if not item:
            return
        card_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
        if card_id:
            self.restoreRequested.emit(card_id)
            self.accept()


class CardBoard(QWidget):
    """卡片工作台。

    参数 ``builtin_widget_factories`` 用来复用现有 ``TodoWidget``、
    ``DoneWidget`` 等结构化组件。工厂可以是不带参数的 callable，也可以接收
    当前 ``CardSpec``。此外可随时调用 ``set_body_widget`` 注入现有实例。
    """

    cardCreationRequested = Signal(str)
    cardCreated = Signal(str)
    cardGenerateRequested = Signal(str)
    cardReviseRequested = Signal(str, str, bool)
    cardUseful = Signal(str)
    cardRevisionRestored = Signal(str, str)
    cardShortVideoRequested = Signal(str)
    cardConvertToTodoRequested = Signal(str, str)
    documentImportRequested = Signal()
    documentLibraryRequested = Signal()
    cardOrderChanged = Signal(list)
    errorRaised = Signal(str)
    statusChanged = Signal(str)

    def __init__(
        self,
        store: Any,
        callbacks: CardBoardCallbacks | None = None,
        builtin_widget_factories: Mapping[str, Callable[..., QWidget]] | None = None,
        parent: QWidget | None = None,
        *,
        auto_initialize: bool = True,
    ):
        super().__init__(parent)
        self.store = store
        self.callbacks = callbacks or CardBoardCallbacks()
        self.builtin_widget_factories = dict(builtin_widget_factories or {})
        self._direct_body_widgets: dict[str, QWidget] = {}
        self._generate_handlers: dict[str, Callable[[], bool | None]] = {}
        self._handled_busy: set[str] = set()
        self.card_frames: dict[str, CardFrame] = {}
        self._chat_dialogs: dict[str, CardChatDialog] = {}
        self._cards: list[Any] = []
        self._organize_mode = False
        self._columns = 2
        self._refreshing = False
        self._drag_card_id = ""
        self._drag_original_ids: list[str] = []
        self._reflow_animation: QParallelAnimationGroup | None = None
        self._open_dialogs: list[QDialog] = []
        self._pending_preferences: dict[str, tuple[str, str]] = {}
        self.setObjectName("shengnianCardBoard")
        data_root = getattr(self.store, "data_root", None)
        self._font_scale = load_font_scale(data_root) if data_root else 1.0
        self.setStyleSheet(
            scale_stylesheet_font_sizes(CARD_BOARD_QSS, self._font_scale)
        )

        self.task_runner = AsyncTaskRunner(self, max_workers=3)
        self.task_runner.busyChanged.connect(self._on_busy_changed)
        self.task_runner.taskFailed.connect(self._on_task_failed)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(12)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(0)
        eyebrow = QLabel("SOUND · MEMORY")
        eyebrow.setObjectName("cardBoardEyebrow")
        title_box.addWidget(eyebrow)
        title = QLabel("我的卡片")
        title.setObjectName("cardBoardTitle")
        title_box.addWidget(title)
        subtitle = QLabel(
            "拖动左上角调整顺序；拖动底边可上下调高度、左右切换标准／大卡片。"
        )
        subtitle.setObjectName("cardBoardSubtitle")
        title_box.addWidget(subtitle)
        header.addLayout(title_box, 1)

        self.add_button = QPushButton("＋ 添加卡片")
        self.add_button.setObjectName("cardBoardPrimary")
        self.add_button.clicked.connect(self.show_add_dialog)
        header.addWidget(self.add_button)
        self.materials_button = QToolButton()
        self.materials_button.setText("资料管理")
        self.materials_button.setObjectName("cardBoardSecondary")
        self.materials_button.setToolTip("导入、查看或删除本地文字资料")
        self.materials_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        materials_menu = QMenu(self.materials_button)
        import_action = materials_menu.addAction("导入 TXT / Markdown…")
        import_action.triggered.connect(
            lambda _checked=False: self.documentImportRequested.emit()
        )
        library_action = materials_menu.addAction("查看和删除已导入资料")
        library_action.triggered.connect(
            lambda _checked=False: self.documentLibraryRequested.emit()
        )
        self.materials_button.setMenu(materials_menu)
        self.materials_button.hide()
        header.addWidget(self.materials_button)
        self.organize_button = QPushButton("卡片管理")
        self.organize_button.setObjectName("cardBoardSecondary")
        self.organize_button.setCheckable(True)
        self.organize_button.toggled.connect(self.set_organize_mode)
        header.addWidget(self.organize_button)
        outer.addLayout(header)

        self.status_label = QLabel("")
        self.status_label.setObjectName("cardBoardStatus")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.hide()
        outer.addWidget(self.status_label)

        self.hidden_tray = QFrame()
        self.hidden_tray.setObjectName("hiddenTray")
        self.hidden_layout = QHBoxLayout(self.hidden_tray)
        self.hidden_layout.setContentsMargins(10, 8, 10, 8)
        self.hidden_layout.setSpacing(6)
        self.hidden_tray.hide()
        outer.addWidget(self.hidden_tray)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setStyleSheet("QScrollArea { background: transparent; }")
        self.canvas = CardCanvas()
        self.canvas.widthChanged.connect(self._on_canvas_width)
        self.canvas.cardPreviewed.connect(self.preview_card_move)
        self.canvas.cardDropped.connect(self.move_card)
        self.canvas.dragPositionChanged.connect(self._auto_scroll_for_drag)
        self.scroll.setWidget(self.canvas)
        outer.addWidget(self.scroll, 1)

        if auto_initialize and hasattr(self.store, "initialize_defaults"):
            try:
                self.store.initialize_defaults()
            except Exception as exc:
                self._report_error(f"默认卡片初始化失败：{exc}")
        self.refresh()

    def set_font_scale(self, scale: float) -> None:
        """即时调整卡片字体；不改变卡片尺寸和拖拽布局。"""

        self._font_scale = float(scale)
        self.setStyleSheet(
            scale_stylesheet_font_sizes(CARD_BOARD_QSS, self._font_scale)
        )

    def _list_cards(
        self, *, include_hidden: bool = True, include_deleted: bool = False
    ) -> list[Any]:
        try:
            return list(
                self.store.list_cards(
                    include_hidden=include_hidden,
                    include_deleted=include_deleted,
                )
            )
        except TypeError:
            return list(self.store.list_cards())

    def _current_revision(self, card_id: str) -> Any:
        if not hasattr(self.store, "current_revision"):
            return None
        return self.store.current_revision(card_id)

    def refresh(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        preserved: dict[str, QWidget] = {}
        try:
            for card_id, frame in self.card_frames.items():
                body = frame.take_body_widget()
                if body is not None:
                    preserved[card_id] = body
            self._direct_body_widgets.update(preserved)
            self._clear_grid()
            self.card_frames.clear()
            self._cards = sorted(
                self._list_cards(include_hidden=True, include_deleted=False),
                key=lambda card: int(_read(card, "position", 0) or 0),
            )
            for card in self._cards:
                if not bool(_read(card, "visible", True)):
                    continue
                card_id = str(_read(card, "card_id", ""))
                revision = self._current_revision(card_id)
                frame = CardFrame(card, _content_of(revision))
                frame.set_organize_mode(self._organize_mode)
                self._wire_frame(frame)
                body = self._body_for(card)
                if body is not None:
                    frame.set_body_widget(
                        body,
                        owned=card_id not in self._direct_body_widgets,
                    )
                self.card_frames[card_id] = frame
            self.canvas.set_ordered_widgets(list(self.card_frames.values()))
            self._reflow()
            self._rebuild_hidden_tray()
        except Exception as exc:
            self._report_error(f"卡片加载失败：{exc}")
        finally:
            self._refreshing = False

    def _body_for(self, card: Any) -> QWidget | None:
        card_id = str(_read(card, "card_id", ""))
        direct = self._direct_body_widgets.get(card_id)
        if direct is not None:
            return direct
        factory = self.builtin_widget_factories.get(card_id)
        if factory is None:
            return None
        try:
            signature = inspect.signature(factory)
            positional = [
                parameter
                for parameter in signature.parameters.values()
                if parameter.kind
                in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                )
                and parameter.default is inspect.Parameter.empty
            ]
            widget = factory(card) if positional else factory()
        except (TypeError, ValueError):
            widget = factory(card)
        if not isinstance(widget, QWidget):
            raise TypeError(f"{card_id} 的正文工厂必须返回 QWidget")
        return widget

    def _wire_frame(self, frame: CardFrame) -> None:
        frame.usefulRequested.connect(self.mark_useful)
        frame.generateRequested.connect(self.request_generate)
        frame.chatRequested.connect(self.show_chat_dialog)
        frame.reviseRequested.connect(self.show_revise_dialog)
        frame.manualEditRequested.connect(self.show_manual_edit_dialog)
        frame.settingsRequested.connect(self.show_settings_dialog)
        frame.preferencesRequested.connect(self.show_preferences_dialog)
        frame.historyRequested.connect(self.show_history_dialog)
        frame.undoRequested.connect(self.undo)
        frame.redoRequested.connect(self.redo)
        frame.initialRequested.connect(self.restore_initial)
        frame.convertToTodoRequested.connect(self.convert_current_to_todo)
        frame.shortVideoRequested.connect(self.request_short_video)
        frame.reorderRequested.connect(self.move_card_by)
        frame.renameRequested.connect(self.show_rename_dialog)
        frame.timeRangeRequested.connect(self.show_time_range_dialog)
        frame.widthRequested.connect(self.set_card_width)
        frame.layoutWidthPreviewRequested.connect(self.preview_card_width)
        frame.layoutResizeRequested.connect(self.set_card_size)
        frame.hideRequested.connect(self.hide_card)
        frame.deleteRequested.connect(self.soft_delete_card)
        frame.dragStarted.connect(self._begin_card_drag)
        frame.dragFinished.connect(self._finish_card_drag)

    def _clear_grid(self) -> None:
        if self._reflow_animation is not None:
            self._reflow_animation.stop()
            self._reflow_animation.deleteLater()
            self._reflow_animation = None
        grid = self.canvas.grid
        while grid.count():
            item = grid.takeAt(0)
            widget = item.widget()
            if widget:
                widget.setParent(None)
                widget.deleteLater()

    def _on_canvas_width(self, width: int) -> None:
        # 工作台默认采用双列。只有窗口确实窄到无法保证卡片可读性时才退回单列，
        # 避免刚进入页面先闪成一列，或常见分屏宽度下意外变成单列。
        columns = 2 if width >= 640 else 1
        if columns != self._columns:
            self._columns = columns
            self._reflow()

    def _span_for(self, frame: CardFrame) -> int:
        width = frame.card_width
        desired = 2 if width == "full" else 1
        return min(self._columns, desired)

    def preview_card_width(self, card_id: str, width: str) -> None:
        """拖动期间只重排画布，不写数据库；松手后再统一持久化。"""
        frame = self.card_frames.get(card_id)
        if frame is None or width not in CARD_WIDTHS:
            return
        frame.card_width = width
        self._reflow()

    def _reflow(self, *, animate: bool = False) -> None:
        grid = self.canvas.grid
        old_geometries = {
            card_id: QRect(frame.geometry())
            for card_id, frame in self.card_frames.items()
        }
        while grid.count():
            grid.takeAt(0)
        row = 0
        column = 0
        for frame in self.card_frames.values():
            span = self._span_for(frame)
            if column and column + span > self._columns:
                row += 1
                column = 0
            grid.addWidget(
                frame,
                row,
                column,
                1,
                span,
                Qt.AlignmentFlag.AlignTop,
            )
            column += span
            if column >= self._columns:
                row += 1
                column = 0
        for index in range(2):
            grid.setColumnStretch(index, 1 if index < self._columns else 0)
        grid.activate()
        self.canvas.set_ordered_widgets(list(self.card_frames.values()))
        new_geometries = {
            card_id: QRect(frame.geometry())
            for card_id, frame in self.card_frames.items()
        }
        self.canvas.set_drop_slots(new_geometries)
        if animate:
            self._animate_reflow(old_geometries, new_geometries)

    def _animate_reflow(
        self,
        old_geometries: Mapping[str, QRect],
        new_geometries: Mapping[str, QRect],
    ) -> None:
        if self._reflow_animation is not None:
            self._reflow_animation.stop()
            self._reflow_animation.deleteLater()
            self._reflow_animation = None
        group = QParallelAnimationGroup(self)
        for card_id, frame in self.card_frames.items():
            if card_id == self._drag_card_id:
                continue
            start = old_geometries.get(card_id)
            end = new_geometries.get(card_id)
            if start is None or end is None or start == end:
                continue
            frame.setGeometry(start)
            animation = QPropertyAnimation(frame, b"geometry", group)
            animation.setDuration(135)
            animation.setStartValue(start)
            animation.setEndValue(end)
            animation.setEasingCurve(QEasingCurve.Type.OutCubic)
            group.addAnimation(animation)
        if group.animationCount() == 0:
            group.deleteLater()
            return
        self._reflow_animation = group
        group.finished.connect(
            lambda current=group: self._finish_reflow_animation(current)
        )
        group.start(QAbstractAnimation.DeletionPolicy.KeepWhenStopped)

    def _finish_reflow_animation(
        self, group: QParallelAnimationGroup
    ) -> None:
        if self._reflow_animation is group:
            self._reflow_animation = None
        group.deleteLater()

    def _set_frame_order(self, ordered_ids: list[str]) -> None:
        existing = self.card_frames
        seen: set[str] = set()
        reordered: dict[str, CardFrame] = {}
        for card_id in ordered_ids:
            frame = existing.get(card_id)
            if frame is not None and card_id not in seen:
                reordered[card_id] = frame
                seen.add(card_id)
        for card_id, frame in existing.items():
            if card_id not in seen:
                reordered[card_id] = frame
        self.card_frames = reordered

    def _begin_card_drag(self, card_id: str) -> None:
        if self._drag_card_id and self._drag_card_id != card_id:
            self._finish_card_drag(self._drag_card_id, False)
        self._drag_card_id = card_id
        self._drag_original_ids = list(self.card_frames)

    def preview_card_move(self, card_id: str, new_position: int) -> bool:
        """只重排当前界面，鼠标松开前不写数据库。"""
        if not self._drag_card_id:
            self._begin_card_drag(card_id)
        if card_id != self._drag_card_id or card_id not in self.card_frames:
            return False
        ids = list(self.card_frames)
        ids.remove(card_id)
        target = max(0, min(len(ids), int(new_position)))
        ids.insert(target, card_id)
        if ids == list(self.card_frames):
            return False
        self._set_frame_order(ids)
        # 拖动过程中不要反复启动几何动画。鼠标每移动一点都会触发一次
        # preview，如果这里重启动画，布局管理器会和动画同时抢写 geometry，
        # 表现就是卡片卡顿、跳回甚至松手后顺序反弹。拖动预览只做一次轻量
        # reflow，松手后由 move_card 统一持久化。
        self._reflow(animate=False)
        return True

    def _finish_card_drag(self, card_id: str, moved: bool) -> None:
        if card_id != self._drag_card_id:
            return
        if not moved and self._drag_original_ids:
            self._set_frame_order(self._drag_original_ids)
            self._reflow(animate=False)
        self.canvas.cancel_drag()
        self._drag_card_id = ""
        self._drag_original_ids = []

    def _auto_scroll_for_drag(self, canvas_position: QPoint) -> None:
        viewport = self.scroll.viewport()
        viewport_position = self.canvas.mapTo(viewport, canvas_position)
        margin = 58
        delta = 0
        if viewport_position.y() < margin:
            delta = -max(8, (margin - viewport_position.y()) // 3)
        elif viewport_position.y() > viewport.height() - margin:
            delta = max(
                8,
                (viewport_position.y() - (viewport.height() - margin)) // 3,
            )
        if delta:
            bar = self.scroll.verticalScrollBar()
            bar.setValue(bar.value() + delta)

    def set_organize_mode(self, enabled: bool) -> None:
        self._organize_mode = bool(enabled)
        self.organize_button.setChecked(self._organize_mode)
        self.organize_button.setText(
            "收起管理" if self._organize_mode else "卡片管理"
        )
        self.materials_button.setVisible(self._organize_mode)
        for frame in self.card_frames.values():
            frame.set_organize_mode(self._organize_mode)
        self._rebuild_hidden_tray()

    def _rebuild_hidden_tray(self) -> None:
        while self.hidden_layout.count():
            item = self.hidden_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        if not self._organize_mode:
            self.hidden_tray.hide()
            return
        label = QLabel("已隐藏")
        label.setObjectName("cardMeta")
        self.hidden_layout.addWidget(label)
        hidden = [card for card in self._cards if not _read(card, "visible", True)]
        if hidden:
            for card in hidden:
                button = QPushButton(f"＋ {_read(card, 'name', '卡片')}")
                button.setObjectName("restoreChip")
                card_id = str(_read(card, "card_id", ""))
                button.clicked.connect(
                    lambda _checked=False, value=card_id: self.restore_card(value)
                )
                self.hidden_layout.addWidget(button)
        else:
            empty = QLabel("没有隐藏的卡片")
            empty.setObjectName("cardMeta")
            self.hidden_layout.addWidget(empty)
        self.hidden_layout.addStretch(1)
        recycle = QPushButton("回收站")
        recycle.setObjectName("cardAction")
        recycle.clicked.connect(self.show_recycle_bin)
        self.hidden_layout.addWidget(recycle)
        self.hidden_tray.show()

    def set_body_widget(self, card_id: str, widget: QWidget) -> None:
        """把现有结构化组件挂载到指定卡片，并在 refresh 后继续保留。"""

        if not isinstance(widget, QWidget):
            raise TypeError("widget 必须是 QWidget")
        old = self._direct_body_widgets.get(card_id)
        if old is not None and old is not widget:
            old.setParent(None)
        self._direct_body_widgets[card_id] = widget
        frame = self.card_frames.get(card_id)
        if frame:
            frame.set_body_widget(widget, owned=False)

    def set_generate_handler(
        self, card_id: str, handler: Callable[[], bool | None]
    ) -> None:
        """为内置卡片接入已有的完整生成链路。

        自定义卡片仍走通用 ``card_generate``；今日/昨日简报等内置卡片可以
        复用经典视图的处理器，避免绕过业务处理后直接展示原始资料。
        """

        if not callable(handler):
            raise TypeError("handler 必须可调用")
        self._generate_handlers[card_id] = handler

    def finish_handled_generation(
        self, card_id: str, *, ok: bool, message: str
    ) -> None:
        self._handled_busy.discard(card_id)
        self._set_card_busy(card_id, False)
        if ok:
            self._set_status(message or "内容已生成。")
        else:
            self._report_error(message or "生成失败，请稍后重试。")

    def card_widget(self, card_id: str) -> CardFrame | None:
        return self.card_frames.get(card_id)

    def show_add_dialog(self) -> None:
        dialog = AddCardDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.description():
            self.request_add_card(
                dialog.description(),
                dialog.card_width(),
                dialog.time_range(),
            )

    def request_add_card(
        self,
        description: str,
        width: str = "standard",
        time_range: str | None = None,
    ) -> bool:
        description = description.strip()
        width = "full" if width == "full" else "standard"
        if not description:
            self._report_error("请先说说你想让这张卡片整理什么。")
            return False
        if self.callbacks.compile_card is None:
            self.cardCreationRequested.emit(description)
            self._set_status("正在理解你的要求…")
            return True
        self._set_status("正在理解你的要求…")
        return self.task_runner.submit(
            "card:create",
            lambda: self.callbacks.compile_card(description),
            lambda result: self.complete_card_creation(
                description,
                result,
                width=width,
                time_range=time_range,
            ),
            lambda message: self._report_error(f"创建卡片失败：{message}"),
        )

    def complete_card_creation(
        self,
        description: str,
        result: Any,
        *,
        width: str = "standard",
        time_range: str | None = None,
    ) -> None:
        """接收 AI 整理后的 CardSpec，并创建卡片。

        ``result`` 可以直接是 CardSpec，也可以是
        ``{"spec": CardSpec, "content": "首次结果"}``。
        """

        spec = _read(result, "spec", result)
        width = "full" if width == "full" else "standard"
        changes = {"width": width}
        if time_range:
            changes["time_range"] = time_range
        if hasattr(spec, "with_updates"):
            spec = spec.with_updates(**changes)
        elif hasattr(spec, "__dataclass_fields__"):
            spec = replace(spec, **changes)
        initial_content = str(_read(result, "content", "") or "")
        try:
            created = self.store.create_card(spec)
            card_id = str(
                _read(created, "card_id", _read(spec, "card_id", "")) or ""
            )
            if initial_content and hasattr(self.store, "add_revision"):
                self.store.add_revision(
                    card_id,
                    initial_content,
                    kind="ai",
                    source_hash=str(_read(result, "source_hash", "") or ""),
                )
            self.refresh()
            self.cardCreated.emit(card_id)
            if initial_content:
                self._set_status("卡片已创建。")
            else:
                self._set_status("卡片已创建；需要时点击“生成”即可运行。")
        except Exception as exc:
            self._report_error(f"卡片保存失败：{exc}")

    def request_generate(self, card_id: str) -> bool:
        if self.is_card_busy(card_id):
            self._set_status("这张卡片正在处理，请等待当前任务完成。")
            return False
        if (
            self.callbacks.generation_gate is not None
            and not self.callbacks.generation_gate(card_id)
        ):
            return False
        handler = self._generate_handlers.get(card_id)
        if handler is not None:
            self._handled_busy.add(card_id)
            self._set_card_busy(card_id, True)
            try:
                accepted = handler()
            except Exception as exc:
                self.finish_handled_generation(
                    card_id, ok=False, message=f"生成失败：{exc}"
                )
                return False
            if accepted is False:
                self._handled_busy.discard(card_id)
                self._set_card_busy(card_id, False)
                return False
            return True
        if self.callbacks.generate_card is None:
            self.cardGenerateRequested.emit(card_id)
            self._set_card_busy(card_id, True)
            return True
        self._set_card_busy(card_id, True)
        submitted = self.task_runner.submit(
            f"card:generate:{card_id}",
            lambda: self.callbacks.generate_card(card_id),
            lambda result: self.complete_generation(card_id, result),
            lambda message: self._finish_with_error(
                card_id, f"生成失败：{message}"
            ),
        )
        if not submitted:
            self._set_card_busy(card_id, False)
        return submitted

    def complete_generation(self, card_id: str, result: Any) -> None:
        if bool(_read(result, "skipped", False)):
            self._set_status(
                str(_read(result, "message", "") or "本次无需更新。")
            )
            self._set_card_busy(card_id, False)
            return
        content = _content_of(_read(result, "content", result))
        if not content:
            self._finish_with_error(card_id, "没有生成可用内容，本次不会保存。")
            return
        try:
            stale = self._result_is_stale(card_id, result)
            revision = self.store.add_revision(
                card_id,
                content,
                kind=str(_read(result, "kind", "ai") or "ai"),
                source_hash=str(_read(result, "source_hash", "") or ""),
                make_current=not stale,
            )
            run_id = str(_read(result, "run_id", "") or "")
            if run_id and hasattr(self.store, "finish_run"):
                self.store.finish_run(
                    run_id,
                    status="stale" if stale else "succeeded",
                    revision_id=_revision_id(revision),
                )
            if stale:
                self._set_status(
                    "生成期间卡片已被修改；旧结果已放入历史候选，没有覆盖当前内容。"
                )
            else:
                incomplete = bool(_read(result, "incomplete", False))
                if incomplete:
                    self._set_status(
                        "内容已保存，但模型触及输出上限；当前结果没有被截断隐藏，"
                        "可再次点击“生成”继续获取完整内容。"
                    )
                else:
                    self._set_status("内容已生成。")
            # 生成完成只更新当前卡片，不重建整个工作台。重建会让所有卡片
            # 重新布局、重新创建文本编辑器，长内容返回时尤其容易出现卡顿。
            if not stale:
                self._update_frame_content(card_id, content)
        except Exception as exc:
            run_id = str(_read(result, "run_id", "") or "")
            if run_id and hasattr(self.store, "finish_run"):
                try:
                    self.store.finish_run(
                        run_id,
                        status="failed",
                        error_code="local_save_failed",
                    )
                except Exception:
                    pass
            self._finish_with_error(card_id, f"生成结果保存失败：{exc}")
        finally:
            self._set_card_busy(card_id, False)

    def show_chat_dialog(self, card_id: str) -> None:
        if card_id == "todos" and self.callbacks.open_todo_capture is not None:
            self.callbacks.open_todo_capture()
            return
        card = self._find_card(card_id)
        if card is None:
            return
        current = _content_of(self._current_revision(card_id))
        if not current:
            self._set_status("这张卡片还没有内容，请先点击“生成”。")
            return
        old = self._chat_dialogs.get(card_id)
        if old is not None:
            old.raise_()
            old.activateWindow()
            return
        dialog = CardChatDialog(
            str(_read(card, "name", "卡片") or "卡片"), current, self
        )
        self._chat_dialogs[card_id] = dialog
        dialog.messageSubmitted.connect(
            lambda _text, cid=card_id: self.request_chat(cid)
        )
        dialog.saveRequested.connect(lambda cid=card_id: self.save_chat_reply(cid))
        dialog.finished.connect(lambda _code, cid=card_id: self._chat_dialogs.pop(cid, None))
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def request_chat(self, card_id: str) -> bool:
        dialog = self._chat_dialogs.get(card_id)
        if dialog is None or self.callbacks.chat_card is None:
            self._report_error("当前版本尚未接入卡片对话服务。")
            return False
        if self.is_card_busy(card_id):
            return False
        dialog.set_busy(True)
        self._set_card_busy(card_id, True)
        messages = [dict(item) for item in dialog.messages]
        submitted = self.task_runner.submit(
            f"card:chat:{card_id}",
            lambda: self.callbacks.chat_card(card_id, messages),
            lambda result: self.complete_chat(card_id, result),
            lambda message: self._finish_chat_with_error(card_id, message),
        )
        if not submitted:
            dialog.set_busy(False)
            self._set_card_busy(card_id, False)
        return submitted

    def complete_chat(self, card_id: str, result: Any) -> None:
        dialog = self._chat_dialogs.get(card_id)
        content = _content_of(_read(result, "content", result))
        if dialog is not None and content:
            dialog.add_reply(content)
            self._set_status("对话已返回；满意的话可以点击“保存到卡片”。")
        elif not content:
            self._finish_chat_with_error(card_id, "模型没有返回可保存的对话内容。")
        self._set_card_busy(card_id, False)

    def _finish_chat_with_error(self, card_id: str, message: str) -> None:
        dialog = self._chat_dialogs.get(card_id)
        if dialog is not None:
            dialog.set_busy(False)
        self._set_card_busy(card_id, False)
        self._report_error(f"对话失败：{message}")

    def save_chat_reply(self, card_id: str) -> bool:
        dialog = self._chat_dialogs.get(card_id)
        if dialog is None or not dialog.latest_reply.strip():
            return False
        try:
            self.store.add_revision(
                card_id,
                dialog.latest_reply,
                kind="chat",
                source_hash="",
            )
            self._update_frame_content(card_id, dialog.latest_reply)
            self._set_status("对话内容已保存为这张卡片的新版本，可在版本管理中回退。")
            return True
        except Exception as exc:
            self._report_error(f"对话内容保存失败：{exc}")
            return False

    def show_revise_dialog(self, card_id: str) -> None:
        card = self._find_card(card_id)
        if card is None:
            return
        current = _content_of(self._current_revision(card_id))
        if not current:
            self.request_generate(card_id)
            return
        dialog = ReviseCardDialog(str(_read(card, "name", "卡片")), self)
        if (
            dialog.exec() == QDialog.DialogCode.Accepted
            and dialog.instruction()
        ):
            self.request_revision(
                card_id, dialog.instruction(), dialog.durable()
            )

    def request_revision(
        self, card_id: str, instruction: str, durable: bool = False
    ) -> bool:
        instruction = instruction.strip()
        if not instruction:
            return False
        if self.is_card_busy(card_id):
            self._set_status("这张卡片正在处理，请等待当前任务完成。")
            return False
        if self.callbacks.revise_card is None:
            self.cardReviseRequested.emit(card_id, instruction, durable)
            self._set_card_busy(card_id, True)
            return True
        self._set_card_busy(card_id, True)
        submitted = self.task_runner.submit(
            f"card:revise:{card_id}",
            lambda: self.callbacks.revise_card(card_id, instruction, durable),
            lambda result: self.complete_revision(card_id, result),
            lambda message: self._finish_with_error(
                card_id, f"修改失败：{message}"
            ),
        )
        if not submitted:
            self._set_card_busy(card_id, False)
        return submitted

    def complete_revision(self, card_id: str, result: Any) -> None:
        content = _content_of(_read(result, "content", result))
        if not content:
            self._finish_with_error(card_id, "没有生成可用修改，本次不会保存。")
            return
        try:
            stale = self._result_is_stale(card_id, result)
            revision = self.store.add_revision(
                card_id,
                content,
                kind=str(_read(result, "kind", "ai") or "ai"),
                source_hash=str(_read(result, "source_hash", "") or ""),
                make_current=not stale,
            )
            durable_preference = str(
                _read(result, "durable_preference", "") or ""
            ).strip()
            if durable_preference and not stale:
                revision_id = str(_read(revision, "revision_id", "") or "")
                self._pending_preferences[card_id] = (
                    revision_id,
                    durable_preference,
                )
            self.refresh()
            if stale:
                self._set_status(
                    "修改期间卡片已发生变化；返回结果已放入历史候选，没有覆盖当前内容。"
                )
            elif durable_preference:
                self._set_status(
                    "已经按长期要求修改；点“有用”后才会记住这条偏好。"
                )
            else:
                self._set_status("已经按你的要求修改。")
        except Exception as exc:
            self._report_error(f"修改结果保存失败：{exc}")
        finally:
            self._set_card_busy(card_id, False)

    def _result_is_stale(self, card_id: str, result: Any) -> bool:
        """规则或正文指针变化时，异步结果不得覆盖用户的新修改。"""
        try:
            expected_rules = int(_read(result, "rules_version", 0) or 0)
        except (TypeError, ValueError):
            expected_rules = 0
        if expected_rules <= 0:
            return False
        card = self.store.get_card(card_id)
        if int(_read(card, "rules_version", 0) or 0) != expected_rules:
            return True
        current = self._current_revision(card_id)
        current_id = _revision_id(current)
        base_revision_id = str(
            _read(result, "base_revision_id", "") or ""
        )
        return current_id != base_revision_id

    def show_manual_edit_dialog(self, card_id: str) -> None:
        card = self._find_card(card_id)
        if card is None:
            return
        dialog = ContentEditDialog(
            str(_read(card, "name", "卡片")),
            _content_of(self._current_revision(card_id)),
            self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.save_manual_edit(card_id, dialog.content())

    def save_manual_edit(self, card_id: str, content: str) -> bool:
        try:
            self.store.add_revision(card_id, content, kind="manual", source_hash="")
            self._update_frame_content(card_id, content)
            self._set_status("手动修改已保存，可随时撤销。")
            return True
        except Exception as exc:
            self._report_error(f"保存失败：{exc}")
            return False

    def show_settings_dialog(self, card_id: str) -> None:
        card = self._find_card(card_id)
        if card is None:
            return
        dialog = CardSettingsDialog(card, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.save_card_settings(card_id, dialog.values())

    def show_rename_dialog(self, card_id: str) -> None:
        card = self._find_card(card_id)
        if card is None:
            return
        current_name = str(_read(card, "name", "卡片") or "卡片")
        name, accepted = QInputDialog.getText(
            self,
            "重命名卡片",
            "卡片名称",
            QLineEdit.EchoMode.Normal,
            current_name,
        )
        if accepted:
            self.rename_card(card_id, name)

    def rename_card(self, card_id: str, name: str) -> bool:
        clean_name = str(name or "").strip()
        if not clean_name:
            self._report_error("卡片名称不能为空。")
            return False
        try:
            self.store.update_card(card_id, name=clean_name)
            self.refresh()
            self._set_status(f"卡片已重命名为“{clean_name}”。")
            return True
        except Exception as exc:
            self._report_error(f"卡片重命名失败：{exc}")
            return False

    def show_time_range_dialog(self, card_id: str) -> None:
        card = self._find_card(card_id)
        if card is None:
            return
        dialog = SourceTimeRangeDialog(
            str(_read(card, "name", "卡片") or "卡片"),
            str(_read(card, "time_range", "today") or "today"),
            self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.save_time_range(card_id, dialog.value())

    def save_time_range(self, card_id: str, time_range: str) -> bool:
        try:
            self.store.update_card(card_id, time_range=time_range)
            self.refresh()
            self._set_status("读取语料的时间已更新；下次生成时生效。")
            return True
        except Exception as exc:
            self._report_error(f"语料时间范围保存失败：{exc}")
            return False

    def save_card_settings(self, card_id: str, values: Mapping[str, Any]) -> bool:
        try:
            self.store.update_card(card_id, **dict(values))
            self.refresh()
            self._set_status("卡片规则已更新；下次生成时生效。")
            return True
        except Exception as exc:
            self._report_error(f"规则保存失败：{exc}")
            return False

    def show_history_dialog(self, card_id: str) -> None:
        card = self._find_card(card_id)
        if card is None or not hasattr(self.store, "list_revisions"):
            return
        try:
            revisions = list(self.store.list_revisions(card_id))
            if card_id == "yesterday_brief":
                # beta.5 之前曾把原始日记错误地导入昨日简报。保留数据库
                # 备份，但不把这些未经复盘的内容作为可恢复版本暴露给用户。
                revisions = [
                    revision
                    for revision in revisions
                    if not _is_unprocessed_yesterday_content(
                        _content_of(revision)
                    )
                ]
        except Exception as exc:
            self._report_error(f"历史版本读取失败：{exc}")
            return
        can_restore = hasattr(self.store, "restore_revision")
        dialog = RevisionHistoryDialog(
            str(_read(card, "name", "卡片")),
            revisions,
            can_restore,
            str(_read(card, "user_prompt", "") or ""),
            self,
        )
        if can_restore:
            dialog.revisionRestoreRequested.connect(
                lambda revision_id: self.restore_revision(card_id, revision_id)
            )
        dialog.promptSaveRequested.connect(
            lambda prompt: self.save_card_prompt(card_id, prompt)
        )
        dialog.exec()

    def save_card_prompt(self, card_id: str, prompt: str) -> bool:
        clean_prompt = str(prompt or "").strip()
        if not clean_prompt:
            self._report_error("生成提示词不能为空。")
            return False
        try:
            self.store.update_card(card_id, user_prompt=clean_prompt)
            self.refresh()
            self._set_status("生成提示词已保存；下次生成时生效。")
            return True
        except Exception as exc:
            self._report_error(f"生成提示词保存失败：{exc}")
            return False

    def show_preferences_dialog(self, card_id: str) -> None:
        card = self._find_card(card_id)
        if card is None or not hasattr(self.store, "list_preferences"):
            return
        try:
            preferences = list(
                self.store.list_preferences(
                    card_id,
                    include_global=False,
                    active_only=True,
                )
            )
        except Exception as exc:
            self._report_error(f"长期要求读取失败：{exc}")
            return
        dialog = PreferencesDialog(
            str(_read(card, "name", "卡片")),
            preferences,
            self,
        )
        dialog.preferenceRevokeRequested.connect(
            lambda preference_id: self.revoke_preference(
                preference_id
            )
        )
        dialog.exec()

    def revoke_preference(self, preference_id: str) -> None:
        if not hasattr(self.store, "revoke_preference"):
            return
        try:
            self.store.revoke_preference(preference_id)
            self._set_status("已撤销这条长期要求；下次生成不再使用。")
        except Exception as exc:
            self._report_error(f"撤销长期要求失败：{exc}")

    def _revoke_preferences_for_revision(
        self, card_id: str, revision_id: str
    ) -> None:
        """撤销随被放弃版本确认的长期偏好，避免继续参与后续生成。"""
        if (
            not revision_id
            or not hasattr(self.store, "list_preferences")
            or not hasattr(self.store, "revoke_preference")
        ):
            return
        preferences = self.store.list_preferences(
            card_id,
            include_global=False,
            active_only=True,
        )
        for preference in preferences:
            if str(_read(preference, "source_revision_id", "") or "") == revision_id:
                self.store.revoke_preference(
                    str(_read(preference, "preference_id", "") or "")
                )

    def restore_revision(self, card_id: str, revision_id: str) -> None:
        if not hasattr(self.store, "restore_revision"):
            return
        try:
            previous = self._current_revision(card_id)
            restored = self.store.restore_revision(card_id, revision_id)
            previous_id = _revision_id(previous)
            if previous_id and _revision_id(restored) != previous_id:
                self._revoke_preferences_for_revision(card_id, previous_id)
            self._pending_preferences.pop(card_id, None)
            self.refresh()
            self.cardRevisionRestored.emit(
                card_id, _content_of(restored)
            )
            self._set_status("已恢复到所选版本。")
        except Exception as exc:
            self._report_error(f"版本恢复失败：{exc}")

    def undo(self, card_id: str) -> None:
        self._revision_pointer_action("undo", card_id, "已撤销上一次修改。")

    def redo(self, card_id: str) -> None:
        self._revision_pointer_action("redo", card_id, "已重做。")

    def restore_initial(self, card_id: str) -> None:
        self._revision_pointer_action(
            "restore_initial", card_id, "已回到本次初稿。"
        )

    def _revision_pointer_action(
        self, method: str, card_id: str, message: str
    ) -> None:
        if not hasattr(self.store, method):
            return
        try:
            previous = self._current_revision(card_id)
            current = getattr(self.store, method)(card_id)
            previous_id = _revision_id(previous)
            if previous_id and _revision_id(current) != previous_id:
                self._revoke_preferences_for_revision(card_id, previous_id)
            self._pending_preferences.pop(card_id, None)
            self.refresh()
            self._set_status(message)
        except Exception as exc:
            self._report_error(f"版本操作失败：{exc}")

    def mark_useful(self, card_id: str) -> None:
        try:
            confirmed = None
            if hasattr(self.store, "confirm_revision"):
                confirmed = self.store.confirm_revision(card_id)
            pending = self._pending_preferences.get(card_id)
            if pending and hasattr(self.store, "add_preference"):
                revision_id, rule_text = pending
                confirmed_id = str(
                    _read(confirmed, "revision_id", "") or ""
                )
                if confirmed_id == revision_id:
                    self.store.add_preference(
                        rule_text,
                        card_id=card_id,
                        scope="card",
                        source_revision_id=revision_id,
                        confirmed=True,
                    )
                    self._pending_preferences.pop(card_id, None)
            self.cardUseful.emit(card_id)
            self._set_status("已记下：这版对你有用。")
        except Exception as exc:
            self._report_error(f"确认失败：{exc}")

    def convert_current_to_todo(self, card_id: str) -> None:
        content = _content_of(self._current_revision(card_id))
        if not content:
            return
        if self.callbacks.convert_to_todo is None:
            self.cardConvertToTodoRequested.emit(card_id, content)
            return
        self.task_runner.submit(
            f"card:todo:{card_id}",
            lambda: self.callbacks.convert_to_todo(card_id, content),
            lambda _result: self._set_status("已转为待办。"),
            lambda message: self._report_error(f"转为待办失败：{message}"),
        )

    def request_short_video(self, card_id: str = "short_video") -> None:
        if self.is_card_busy(card_id):
            self._set_status("这张卡片正在处理，请等待当前任务完成。")
            return
        if self.callbacks.open_short_video is None:
            self.cardShortVideoRequested.emit(card_id)
            return
        self.task_runner.submit(
            f"card:video:{card_id}",
            lambda: self.callbacks.open_short_video(card_id),
            None,
            lambda message: self._report_error(f"短视频创作打开失败：{message}"),
        )

    def set_card_width(self, card_id: str, width: str) -> bool:
        if width not in CARD_WIDTHS:
            self._report_error("卡片宽度只能是标准或通栏。")
            return False
        try:
            self.store.update_layout(card_id, width=width)
            card = self._find_card(card_id)
            if card is not None:
                try:
                    setattr(card, "width", width)
                except (AttributeError, TypeError):
                    if isinstance(card, dict):
                        card["width"] = width
            self.refresh()
            return True
        except Exception as exc:
            self._report_error(f"卡片宽度保存失败：{exc}")
            return False

    def set_card_size(self, card_id: str, height: int, width: str) -> bool:
        """保存底部拖拽条产生的高度和两档宽度。"""
        if width not in CARD_WIDTHS:
            self._report_error("卡片宽度只能是标准或通栏。")
            return False
        height = max(MIN_CARD_HEIGHT, min(MAX_CARD_HEIGHT, int(height)))
        try:
            self.store.update_layout(card_id, width=width, height=height)
            card = self._find_card(card_id)
            if card is not None:
                for field, value in (("width", width), ("height", height)):
                    try:
                        setattr(card, field, value)
                    except (AttributeError, TypeError):
                        if isinstance(card, dict):
                            card[field] = value
            self.refresh()
            return True
        except Exception as exc:
            self._report_error(f"卡片大小保存失败：{exc}")
            return False

    def move_card_by(self, card_id: str, delta: int) -> bool:
        visible = [
            card
            for card in self._cards
            if bool(_read(card, "visible", True))
        ]
        ids = [str(_read(card, "card_id", "")) for card in visible]
        if card_id not in ids:
            return False
        current = ids.index(card_id)
        return self.move_card(card_id, max(0, min(len(ids) - 1, current + delta)))

    def move_card(self, card_id: str, new_position: int) -> bool:
        visible = [
            card
            for card in self._cards
            if bool(_read(card, "visible", True))
        ]
        ids = [str(_read(card, "card_id", "")) for card in visible]
        if card_id not in ids:
            return False
        old_position = ids.index(card_id)
        ids.pop(old_position)
        target = max(0, min(len(ids), int(new_position)))
        ids.insert(target, card_id)
        try:
            for position, value in enumerate(ids):
                self.store.update_layout(value, position=position)
            # 拖放事件发生在 QDrag.exec() 尚未返回时。此时调用 refresh 会
            # 删除并重建正在拖动的 QWidget，Windows 原生拖放结束后就容易
            # 看到“回退”或闪烁。拖动期间只更新内存顺序和网格，松手后再由
            # 下一次正常刷新读取数据库；数据库位置已经在上面一次性保存。
            if self._drag_card_id == card_id:
                self._set_frame_order(ids)
                visible_map = {
                    str(_read(card, "card_id", "")): card
                    for card in self._cards
                    if bool(_read(card, "visible", True))
                }
                hidden = [
                    card
                    for card in self._cards
                    if not bool(_read(card, "visible", True))
                ]
                self._cards = [visible_map[value] for value in ids if value in visible_map] + hidden
                self._reflow(animate=False)
            else:
                self.refresh()
            self.cardOrderChanged.emit(ids)
            return True
        except Exception as exc:
            self._report_error(f"卡片排序保存失败：{exc}")
            return False

    def _update_frame_content(self, card_id: str, content: str) -> None:
        """只刷新一张卡片的正文，避免生成后重建整个工作台。"""

        frame = self.card_frames.get(card_id)
        if frame is None:
            return
        # 今日/昨日简报、待办和已办使用经典视图复用的正文组件；这些组件
        # 由各自的业务链路刷新，不要把生成结果强行写进它们。
        if frame.body_widget() is frame.content_editor:
            frame.set_content(content)
        try:
            card = self.store.get_card(card_id)
            frame.card = card
            frame.updated_label.setText(
                f"更新于 {_format_time(_read(card, 'updated_at', ''))}"
            )
        except Exception:
            pass

    def hide_card(self, card_id: str) -> bool:
        try:
            self.store.hide_card(card_id)
            self.refresh()
            return True
        except Exception as exc:
            self._report_error(f"隐藏卡片失败：{exc}")
            return False

    def restore_card(self, card_id: str) -> bool:
        try:
            self.store.show_card(card_id)
            self.refresh()
            return True
        except Exception as exc:
            self._report_error(f"恢复卡片失败：{exc}")
            return False

    def soft_delete_card(self, card_id: str) -> bool:
        card = self._find_card(card_id)
        if card is None:
            return False
        if _is_default(card):
            self._report_error("默认卡片不能删除，可以选择隐藏。")
            return False
        if self.is_card_busy(card_id):
            self._report_error("卡片正在处理，任务完成前不能删除。")
            return False
        try:
            self.store.soft_delete(card_id)
            self.refresh()
            self._set_status("卡片已移入本地回收站，30天内可以恢复。")
            return True
        except Exception as exc:
            self._report_error(f"删除卡片失败：{exc}")
            return False

    def show_recycle_bin(self) -> None:
        try:
            cards = self._list_cards(include_hidden=True, include_deleted=True)
            deleted = [card for card in cards if _read(card, "deleted_at", None)]
        except Exception as exc:
            self._report_error(f"回收站读取失败：{exc}")
            return
        dialog = RecycleBinDialog(deleted, self)
        dialog.restoreRequested.connect(self.restore_deleted_card)
        dialog.exec()

    def restore_deleted_card(self, card_id: str) -> bool:
        try:
            self.store.restore_deleted(card_id)
            self.refresh()
            self._set_status("卡片已从回收站恢复。")
            return True
        except Exception as exc:
            self._report_error(f"恢复卡片失败：{exc}")
            return False

    def _find_card(self, card_id: str) -> Any | None:
        for card in self._cards:
            if str(_read(card, "card_id", "")) == card_id:
                return card
        try:
            return self.store.get_card(card_id)
        except Exception:
            return None

    def is_card_busy(self, card_id: str) -> bool:
        if card_id in self._handled_busy:
            return True
        suffix = f":{card_id}"
        return any(
            key.startswith("card:") and key.endswith(suffix)
            for key in self.task_runner._tasks
        )

    @Slot(str, bool)
    def _on_busy_changed(self, key: str, busy: bool) -> None:
        parts = key.split(":")
        if len(parts) >= 3 and parts[0] == "card":
            self._set_card_busy(parts[-1], busy)
        self.add_button.setEnabled(not self.task_runner.is_busy("card:create"))

    @Slot(str, str)
    def _on_task_failed(self, _key: str, message: str) -> None:
        if message:
            self.errorRaised.emit(message)

    def _set_card_busy(self, card_id: str, busy: bool) -> None:
        frame = self.card_frames.get(card_id)
        if frame:
            frame.set_busy(busy)

    def _finish_with_error(self, card_id: str, message: str) -> None:
        self._set_card_busy(card_id, False)
        self._report_error(message)

    def _set_status(self, message: str) -> None:
        self.status_label.setText(message)
        self.status_label.setVisible(bool(message))
        self.statusChanged.emit(message)

    def _report_error(self, message: str) -> None:
        self._set_status(message)
        self.errorRaised.emit(message)


__all__ = [
    "AddCardDialog",
    "AsyncTaskRunner",
    "CARD_BOARD_QSS",
    "CARD_MIME_TYPE",
    "CARD_WIDTHS",
    "CardBoard",
    "CardBoardCallbacks",
    "CardCanvas",
    "CardFrame",
    "CardSettingsDialog",
    "ContentEditDialog",
    "PreferencesDialog",
    "RecycleBinDialog",
    "RevisionHistoryDialog",
    "ReviseCardDialog",
]
