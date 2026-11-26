from collections.abc import Callable
from datetime import datetime

from PySide6.QtCore import QRect, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QColor,
    QCursor,
    QDesktopServices,
    QIcon,
    QPainter,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .desktop import claude_setup, installed_executable
from .quota import FIVE_HOURS, WEEK, Provider, Snapshot, Window, countdown, reset_badge

STYLESHEET = """
    QWidget#usage { background: #12151e; border: 1px solid #343a4b; border-radius: 13px; }
    QWidget, QLabel { color: #edf0fa; font-size: 13px; }
    QLabel#title { font-size: 19px; font-weight: 700; }
    QLabel#provider { font-size: 14px; font-weight: 700; }
    QLabel#meta { font-size: 11px; color: #adb5c9; }
    QLabel#amount { font-size: 12px; font-weight: 650; }
    QLabel#resetBadge {
        background: #2b243f; color: #e2d4ff; border-radius: 6px; padding: 5px 8px;
        font-size: 11px; font-weight: 650;
    }
    QFrame#providerCard { background: #1b202c; border: 1px solid #2e3444; border-radius: 10px; }
    QScrollArea { border: none; background: transparent; }
    QWidget#quotaCards, QWidget#quotaViewport { background: #12151e; }
    QScrollBar:vertical { background: #12151e; width: 7px; margin: 0; }
    QScrollBar::handle:vertical { background: #535d76; border-radius: 3px; min-height: 24px; }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
    QPushButton {
        background: #252b3a; color: #e6e9f5; border: 1px solid #353e53;
        border-radius: 6px; padding: 4px 8px; font-size: 11px; font-weight: 600;
    }
    QPushButton:hover { background: #343b50; }
    QPushButton:focus { border-color: #bcabff; }
    QPushButton:checked { background: #382f53; }
    QProgressBar { background: #303749; border: none; border-radius: 3px; }
"""


def tray_icon() -> QIcon:
    pixmap = QPixmap(48, 48)
    pixmap.fill(Qt.GlobalColor.transparent)
    with QPainter(pixmap) as painter:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#221c36"))
        painter.drawRoundedRect(2, 2, 44, 44, 11, 11)
        painter.setBrush(QColor("#655682"))
        painter.drawRoundedRect(10, 13, 28, 8, 4, 4)
        painter.drawRoundedRect(10, 27, 28, 8, 4, 4)
        painter.setBrush(QColor("#c4afff"))
        painter.drawRoundedRect(10, 13, 22, 8, 4, 4)
        painter.drawRoundedRect(10, 27, 15, 8, 4, 4)
    return QIcon(pixmap)


def label(text: str = "", style: str = "") -> QLabel:
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setObjectName(style)
    return widget


def button(text: str, callback: Callable[[], object]) -> QPushButton:
    widget = QPushButton(text)
    widget.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
    widget.clicked.connect(callback)
    return widget


def _set_text(widget: QLabel, text: str) -> None:
    if widget.text() != text:
        widget.setText(text)


class LimitRow(QWidget):
    def __init__(self, window: Window):
        super().__init__()
        self.quota = window
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        heading = QHBoxLayout()
        heading.setSpacing(8)
        caption = "5-hour" if window.minutes == FIVE_HOURS else "Weekly"
        remaining = f"{window.remaining:.1f}".rstrip("0").rstrip(".")
        heading.addWidget(label(caption))
        self.timer = label("", "meta")
        heading.addWidget(self.timer)
        heading.addStretch()
        heading.addWidget(label(f"{remaining}% left", "amount"))
        layout.addLayout(heading)

        bar = QProgressBar()
        bar.setAccessibleName(f"{caption} quota remaining")
        bar.setRange(0, 1000)
        bar.setValue(round(window.remaining * 10))
        bar.setTextVisible(False)
        bar.setFixedHeight(6)
        color = (
            "#f196a6"
            if window.remaining <= 5
            else "#f4c777"
            if window.remaining <= 20
            else "#b9a5ff"
        )
        bar.setStyleSheet(
            f"QProgressBar::chunk {{ background: {color}; border-radius: 3px; }}"
        )
        layout.addWidget(bar)
        if window.resets_at is not None:
            self.timer.setToolTip(
                datetime.fromtimestamp(window.resets_at)
                .strftime("%a, %b %-d at %-I:%M %p %Z")
                .strip()
            )

    def tick(self, snapshot: Snapshot, now: int, failed: bool) -> None:
        message = countdown(self.quota.resets_at, now)
        if failed or snapshot.is_stale(now, self.quota):
            message += " · Stale"
        _set_text(self.timer, message)


class ProviderCard(QFrame):
    layout_changed = Signal()

    def __init__(self, provider: Provider, title: str):
        super().__init__()
        self.setObjectName("providerCard")
        self.provider = provider
        self.snapshot: Snapshot | None = None
        self.problem = ""
        self.now = 0
        self.rows: list[LimitRow] = []
        panel = QVBoxLayout(self)
        panel.setContentsMargins(12, 10, 12, 10)
        panel.setSpacing(7)

        header = QHBoxLayout()
        header.addWidget(label(title, "provider"))
        header.addStretch()
        self.groups_button = button("Model groups", self._rebuild)
        self.groups_button.setCheckable(True)
        self.groups_button.hide()
        header.addWidget(self.groups_button)
        panel.addLayout(header)

        self.data = QVBoxLayout()
        self.data.setSpacing(7)
        panel.addLayout(self.data)
        self.badge = label("", "resetBadge")
        self.badge.setWordWrap(True)
        self.badge.hide()
        panel.addWidget(self.badge)
        self.status = label("", "meta")
        self.status.setWordWrap(True)
        panel.addWidget(self.status)

        if provider == "claude":
            actions = QHBoxLayout()
            self.setup_button = button("Copy Claude setup", self._copy_claude_setup)
            self.setup_button.setToolTip(
                "Copy the status-line settings for ~/.claude/settings.json"
            )
            actions.addWidget(self.setup_button)
            actions.addWidget(
                button(
                    "Open Usage",
                    lambda: QDesktopServices.openUrl(
                        QUrl("https://claude.ai/settings/usage")
                    ),
                )
            )
            actions.addStretch()
            panel.addLayout(actions)
        self.tick(0)

    def _copy_claude_setup(self) -> None:
        QApplication.clipboard().setText(claude_setup(installed_executable()))
        self.setup_button.setText("Copied")
        QTimer.singleShot(1400, lambda: self.setup_button.setText("Copy Claude setup"))

    def display(self, snapshot: Snapshot | None, problem: str, now: int) -> None:
        self.problem = problem
        self.now = now
        if snapshot != self.snapshot:
            self.snapshot = snapshot
            self._rebuild()
        else:
            self.tick(now)

    def _rebuild(self) -> None:
        while self.data.count():
            item = self.data.takeAt(0)
            if item is not None and (child := item.widget()) is not None:
                child.hide()
                child.setParent(None)
                child.deleteLater()
        self.rows.clear()
        snapshot = self.snapshot
        windows = snapshot.windows if snapshot else ()
        groups: dict[str, list[Window]] = {}
        for window in windows:
            groups.setdefault(window.group, []).append(window)

        self.groups_button.setVisible(bool(groups) and self.provider == "agy")
        if self.provider == "agy" and groups and not self.groups_button.isChecked():
            if len(groups) > 1:
                self.data.addWidget(
                    label("Lowest remaining across model groups", "meta")
                )
            groups = {
                "": [
                    min(matches, key=lambda window: window.remaining)
                    for minutes in (FIVE_HOURS, WEEK)
                    if (
                        matches := [
                            window for window in windows if window.minutes == minutes
                        ]
                    )
                ]
            }
        for group, values in groups.items():
            if group:
                caption = label(group, "meta")
                caption.setWordWrap(True)
                self.data.addWidget(caption)
                caption.show()
            for window in values:
                row = LimitRow(window)
                self.rows.append(row)
                self.data.addWidget(row)
                row.show()
        self.tick(self.now)
        self.layout_changed.emit()

    def tick(self, now: int) -> None:
        self.now = now
        snapshot = self.snapshot
        if snapshot is None:
            waiting = (
                "Waiting for first update"
                if self.provider == "claude"
                else "Checking quota…"
            )
            _set_text(self.status, self.problem or waiting)
            return

        for row in self.rows:
            row.tick(snapshot, now, failed=bool(self.problem))
        stamp = datetime.fromtimestamp(snapshot.observed_at).strftime(
            "%b %-d, %-I:%M %p"
        )
        info = f"Last updated {stamp}"
        if not snapshot.windows:
            info = f"Quota unavailable · {info}"
        if self.problem:
            info = f"{self.problem}\n{info}"
        _set_text(self.status, info)

        offer = reset_badge(snapshot, now, failed=bool(self.problem))
        _set_text(self.badge, offer or "")
        if bool(offer) == self.badge.isHidden():
            self.badge.setVisible(bool(offer))
            self.layout_changed.emit()


class UsageWindow(QWidget):
    refresh_requested = Signal()

    def __init__(self, *, popup: bool = False):
        flags = (
            Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
            if popup
            else Qt.WindowType.Window
        )
        super().__init__(None, flags)
        self.setObjectName("usage")
        self.setWindowTitle("Agent Quota")
        self.setFixedWidth(398)
        self.setStyleSheet(STYLESHEET)
        self._area: QRect | None = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(13, 12, 13, 13)
        outer.setSpacing(10)

        header = QHBoxLayout()
        glyph = label()
        glyph.setPixmap(tray_icon().pixmap(24, 24))
        header.addWidget(glyph)
        header.addWidget(label("Agent Quota", "title"))
        header.addStretch()
        header.addWidget(button("Refresh", self.refresh_requested.emit))
        if popup:
            close = button("×", self.hide)
            close.setAccessibleName("Close popup")
            header.addWidget(close)
        outer.addLayout(header)

        self.scroller = QScrollArea()
        self.scroller.setWidgetResizable(True)
        self.scroller.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.scroller.setFrameShape(QFrame.Shape.NoFrame)
        self.scroller.viewport().setObjectName("quotaViewport")
        self.contents = QWidget()
        self.contents.setObjectName("quotaCards")
        self.scroller.setWidget(self.contents)
        self.cards_layout = QVBoxLayout(self.contents)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards_layout.setSpacing(8)
        self.fit_timer = QTimer(self)
        self.fit_timer.setSingleShot(True)
        self.fit_timer.timeout.connect(self.refit)
        self.cards: dict[Provider, ProviderCard] = {
            "claude": ProviderCard("claude", "Claude Code"),
            "codex": ProviderCard("codex", "Codex"),
            "agy": ProviderCard("agy", "Antigravity"),
        }
        for card in self.cards.values():
            self.cards_layout.addWidget(card)
            card.layout_changed.connect(self.fit_timer.start)
        outer.addWidget(self.scroller)
        self.escape = QShortcut("Escape", self)
        self.escape.activated.connect(self.close if not popup else self.hide)
        self.refit()

    def refit(self) -> None:
        area = (
            self._area if self._area is not None else self.screen().availableGeometry()
        )
        # Card hints are available even before the scroll area is shown.
        gaps = (len(self.cards) - 1) * self.cards_layout.spacing()
        height = max(
            self.contents.sizeHint().height(),
            sum(card.sizeHint().height() for card in self.cards.values()) + gaps,
        )
        self.contents.setMinimumHeight(height)
        self.scroller.setFixedHeight(min(height, max(100, area.height() - 76)))
        self.adjustSize()
        if self.isVisible():
            self._move_inside(area, self.x(), self.y())

    def _move_inside(self, area: QRect, x: int, y: int) -> None:
        self.move(
            max(area.left(), min(x, area.right() - self.width() + 1)),
            max(area.top(), min(y, area.bottom() - self.height() + 1)),
        )

    def reveal(self, anchor: QRect) -> None:
        point = anchor.center() if anchor.isValid() else QCursor.pos()
        screen = QApplication.screenAt(point) or self.screen()
        area = screen.availableGeometry()
        self._area = area
        if handle := self.windowHandle():
            handle.setScreen(screen)
        self.refit()
        x = point.x() - self.width() // 2
        y = anchor.bottom() + 8 if anchor.isValid() else area.top() + 12
        self._move_inside(area, x, y)
        self.show()
        self.fit_timer.start()
        self.raise_()
        self.activateWindow()
