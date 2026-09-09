"""The single application window - a simple AI-chat-style log.

Every Gemini response is appended as a MessageWidget in a scrollable
column (scrollable history - not a single pane that gets overwritten).
The frameless/borderless/capture-excluded/opacity-toggle/no-taskbar chrome
is applied at the Win32 level to this QMainWindow's HWND - see
:meth:`apply_chrome` and ``window/window_utils.py``.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..window import capture_protection, window_utils
from . import theme
from .message_widget import MessageWidget

logger = logging.getLogger(__name__)

_BACKGROUND_STYLE = f"background-color: {theme.BG};"

_STATUS_BASE = f"""
QLabel {{
    font-family: {theme.UI_CSS};
    font-size: 12px;
    padding: 8px 14px;
    border-bottom: 1px solid {theme.BORDER};
}}
"""
# Idle vs. active vs. failed are told apart by brightness and weight, not
# by hue - dim grey for "nothing queued", full white and bold for anything
# in flight or failed. Brightness survives the opacity hotkeys; so does the
# wording, which is what actually names the failure.
_STATUS_IDLE = _STATUS_BASE + f"QLabel {{ background-color: {theme.BG_RAISED}; color: {theme.TEXT_FAINT}; }}"
_STATUS_ACTIVE = _STATUS_BASE + f"QLabel {{ background-color: {theme.BG_RAISED}; color: {theme.TEXT}; font-weight: 600; }}"
_STATUS_ERROR = _STATUS_BASE + f"QLabel {{ background-color: {theme.BORDER}; color: {theme.TEXT}; font-weight: 700; }}"

_SCROLLBAR_STYLE = f"""
QScrollArea {{ border: none; background-color: {theme.BG}; }}
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {theme.BORDER_STRONG};
    border-radius: 5px;
    min-height: 32px;
}}
QScrollBar::handle:vertical:hover {{ background: {theme.TEXT_FAINT}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
"""

_EMPTY_HINT_STYLE = f"""
QLabel {{
    color: {theme.TEXT_FAINT};
    font-family: {theme.UI_CSS};
    font-size: 13px;
    padding: 28px 20px;
}}
"""


class ResponseWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setStyleSheet(_BACKGROUND_STYLE)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet(_SCROLLBAR_STYLE)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._content = QWidget()
        self._content.setStyleSheet(_BACKGROUND_STYLE)
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(0)

        # Shown until the first response lands, so a freshly-launched
        # window says what to press instead of sitting there blank.
        self._empty_hint = QLabel(
            "No responses yet.\n\n"
            "Queue a screenshot, insert your prompt, then send - "
            "the bar above always shows what is currently queued."
        )
        self._empty_hint.setStyleSheet(_EMPTY_HINT_STYLE)
        self._empty_hint.setWordWrap(True)
        self._empty_hint.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._content_layout.addWidget(self._empty_hint)

        self._content_layout.addStretch(1)  # keeps messages pinned to top until scroll needed

        self._scroll.setWidget(self._content)

        # Status bar (queue contents) sits OUTSIDE the scroll area, pinned
        # to the top, so it's always visible regardless of scroll
        # position - mirrors the settings window's "Save/Cancel always
        # reachable" pattern, just for queue visibility instead.
        self._status_label = QLabel("Queue empty.")
        self._status_label.setStyleSheet(_STATUS_IDLE)
        self._status_label.setWordWrap(True)
        self._message_count = 0

        outer = QWidget()
        outer.setStyleSheet(_BACKGROUND_STYLE)
        outer_layout = QVBoxLayout(outer)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)
        outer_layout.addWidget(self._status_label)
        outer_layout.addWidget(self._scroll, 1)

        self.setCentralWidget(outer)

        self._closing_callback: Optional[Callable[[], None]] = None
        self._geometry_changed_callback: Optional[Callable[[int, int, int, int], None]] = None
        self._opacity_changed_callback: Optional[Callable[[int], None]] = None
        self._opacity = 255

        self._pending_scroll_to_bottom = False
        self._scroll.verticalScrollBar().rangeChanged.connect(self._on_scroll_range_changed)

    def _on_scroll_range_changed(self, _minimum: int, maximum: int) -> None:
        if not self._pending_scroll_to_bottom:
            return
        self._pending_scroll_to_bottom = False
        self._scroll.verticalScrollBar().setValue(maximum)

    # -- lifecycle -----------------------------------------------------
    def apply_chrome(self) -> None:
        """Show the window and apply the Win32-level treatment on top of
        it. hide_from_taskbar_and_alttab() does its own raw
        ShowWindow(SW_HIDE)->ShowWindow(SW_SHOWNOACTIVATE) cycle (needed
        so DWM picks up the WS_EX_TOOLWINDOW style change) - if that
        happens before Qt has actually pumped events to realize/paint
        the window, Qt's internal visibility bookkeeping desyncs from
        the real Win32 state and the window never appears. So: show()
        first, pump events so Qt fully realizes the native window,
        *then* mutate styles, then explicitly re-show so Qt resyncs its
        own state with whatever the raw Win32 calls left behind."""
        self.show()
        app = QApplication.instance()
        if app is not None:
            for _ in range(5):
                app.processEvents()

        hwnd = int(self.winId())
        capture_protection.enable_capture_protection(hwnd)
        window_utils.hide_from_taskbar_and_alttab(hwnd)
        window_utils.set_topmost(hwnd, True)
        window_utils.set_window_opacity(hwnd, self._opacity)
        window_utils.remove_native_border(hwnd)

        # Re-assert visibility so Qt's own state matches reality after
        # the raw hide/show cycle above.
        self.setVisible(True)
        window_utils.show_no_activate(hwnd)
        if app is not None:
            app.processEvents()

    def hwnd(self) -> int:
        return int(self.winId())

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if self._closing_callback:
            try:
                self._closing_callback()
            except Exception:
                logger.exception("Error in closing callback.")
        super().closeEvent(event)

    def set_closing_callback(self, callback: Callable[[], None]) -> None:
        self._closing_callback = callback

    def set_geometry_changed_callback(self, callback: Callable[[int, int, int, int], None]) -> None:
        self._geometry_changed_callback = callback

    def set_opacity_changed_callback(self, callback: Callable[[int], None]) -> None:
        self._opacity_changed_callback = callback

    # -- chat history ----------------------------------------------------
    def append_response(self, markdown_text: str, *, is_error: bool = False) -> None:
        """Append a new response block at the bottom and scroll to it.
        Must be called on the Qt main thread - callers dispatching from a
        worker thread should use a Qt signal to marshal onto it."""
        if self._empty_hint is not None:
            # setParent(None) detaches it now; deleteLater() alone only
            # queues a DeferredDelete event, which plain processEvents()
            # does not dispatch - so the hint would linger above the first
            # real response.
            self._content_layout.removeWidget(self._empty_hint)
            self._empty_hint.setParent(None)
            self._empty_hint.deleteLater()
            self._empty_hint = None

        self._message_count += 1
        widget = MessageWidget(
            markdown_text,
            index=self._message_count,
            timestamp=time.strftime("%H:%M:%S"),
            is_error=is_error,
        )
        # Insert before the trailing stretch so new messages append at
        # the bottom, not above the stretch spacer.
        count = self._content_layout.count()
        self._content_layout.insertWidget(count - 1, widget)

        # Arm the one-shot auto-scroll: _on_scroll_range_changed (connected
        # once, in __init__) jumps to the bottom on the next range change,
        # then disarms so the user's own scrolling isn't yanked back down
        # every time a message widget re-lays out.
        self._pending_scroll_to_bottom = True

    def set_queue_status(self, text: str) -> None:
        """Update the small status line pinned above the response log
        showing what's currently queued (image count + prompt preview).
        Purely visual - the queue itself lives in queue_store.py.

        The bar restyles itself from the text: idle, busy/queued, or
        failed - by brightness and weight only, no colour."""
        self._status_label.setText(text)
        lowered = text.lower()
        if "fail" in lowered or "no api key" in lowered:
            self._status_label.setStyleSheet(_STATUS_ERROR)
        elif "queue empty" in lowered or "cleared" in lowered or "nothing" in lowered:
            self._status_label.setStyleSheet(_STATUS_IDLE)
        else:
            self._status_label.setStyleSheet(_STATUS_ACTIVE)

    # -- window move/resize/opacity, driven by hotkeys ------------------
    def toggle_visibility(self) -> None:
        hwnd = self.hwnd()
        if window_utils.is_window_visible(hwnd):
            window_utils.hide_window(hwnd)
        else:
            window_utils.show_no_activate(hwnd)

    def focus_window(self) -> None:
        window_utils.show_and_activate(self.hwnd())

    def _move_by(self, dx: int, dy: int) -> None:
        window_utils.move_window_by(self.hwnd(), dx, dy)
        self._report_geometry()

    def _resize_by(self, dw: int, dh: int) -> None:
        window_utils.resize_window_by(self.hwnd(), dw, dh)
        self._report_geometry()

    def move_up(self) -> None:
        self._move_by(0, -20)

    def move_down(self) -> None:
        self._move_by(0, 20)

    def move_left(self) -> None:
        self._move_by(-20, 0)

    def move_right(self) -> None:
        self._move_by(20, 0)

    def increase_width(self) -> None:
        self._resize_by(20, 0)

    def decrease_width(self) -> None:
        self._resize_by(-20, 0)

    def increase_height(self) -> None:
        self._resize_by(0, 20)

    def decrease_height(self) -> None:
        self._resize_by(0, -20)

    def _report_geometry(self) -> None:
        if not self._geometry_changed_callback:
            return
        rect = window_utils.get_window_rect(self.hwnd())
        if rect is None:
            return
        x, y, w, h = rect
        self._geometry_changed_callback(x, y, w, h)

    def increase_opacity(self) -> None:
        self._opacity = min(255, self._opacity + 15)
        window_utils.set_window_opacity(self.hwnd(), self._opacity)
        if self._opacity_changed_callback:
            self._opacity_changed_callback(self._opacity)

    def decrease_opacity(self) -> None:
        self._opacity = max(40, self._opacity - 15)
        window_utils.set_window_opacity(self.hwnd(), self._opacity)
        if self._opacity_changed_callback:
            self._opacity_changed_callback(self._opacity)
