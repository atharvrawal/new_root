"""The bottom input box - the chat composer.

This widget is the single source of truth for the queued prompt text.
Before, prompt text lived invisibly inside ``ImageQueue`` and the status
bar showed a truncated preview of it; now it lives here, where you can
actually see and edit it. ``ImageQueue`` was reduced to holding images
only, so there is exactly one copy of the text and nothing to keep in
sync.

Everything that used to append text to the queue - the configured-prompt
hotkey, and background capture mode's live per-keystroke typing - now
appends here instead, via ``AppController.insert_text``. Those arrive on
the Qt main thread already (see ``AppController.dispatch``), so this
widget is only ever touched from the one thread that may touch it.

Sizing behaves like the web chat UIs it's modelled on: the box grows with
the text up to ``_MAX_HEIGHT``, then stops growing and scrolls internally.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QKeyEvent, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import theme

_MIN_HEIGHT = 46
_MAX_HEIGHT = 220

_EDIT_STYLE = f"""
QTextEdit {{
    background: transparent;
    border: none;
    color: {theme.TEXT};
    font-family: {theme.UI_CSS};
    font-size: 14px;
    padding: 0;
    selection-background-color: #ffffff;
    selection-color: #000000;
}}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 0; }}
QScrollBar::handle:vertical {{
    background: {theme.BORDER_STRONG}; border-radius: 4px; min-height: 24px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
"""

_SEND_STYLE = f"""
QPushButton {{
    background-color: {theme.TEXT};
    color: #000000;
    border: none;
    border-radius: 15px;
    font-family: {theme.UI_CSS};
    font-size: 15px;
    font-weight: 700;
    min-width: 30px;
    max-width: 30px;
    min-height: 30px;
    max-height: 30px;
}}
QPushButton:hover {{ background-color: {theme.TEXT_DIM}; }}
QPushButton:disabled {{ background-color: {theme.BORDER_STRONG}; color: {theme.TEXT_FAINT}; }}
"""

_HINT_STYLE = f"""
QLabel {{
    color: {theme.TEXT_FAINT};
    font-family: {theme.UI_CSS};
    font-size: 11px;
    padding: 0 4px;
}}
"""

# Transient state (sending, failures) sits on the same row as the
# key hints but in full white, so it reads as the live thing and the hints
# recede. This row replaced the old status bar at the top of the window.
# Capture mode gets a white border and a filled badge. It has to be
# unmistakable: with every key swallowed and no window focused, "capture
# mode is on" and "the app is frozen" look identical otherwise.
_BOX_STYLE_IDLE = (
    f"#composerBox {{ background-color: {theme.BG_RAISED};"
    f" border: 1px solid {theme.BORDER}; border-radius: 14px; }}"
)
_BOX_STYLE_CAPTURING = (
    f"#composerBox {{ background-color: {theme.BG_RAISED};"
    f" border: 2px solid {theme.TEXT}; border-radius: 14px; }}"
)

_BADGE_STYLE = f"""
QLabel {{
    background-color: {theme.TEXT};
    color: #000000;
    font-family: {theme.UI_CSS};
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 1px;
    padding: 2px 8px;
    border-radius: 7px;
}}
"""

_STATUS_STYLE = f"""
QLabel {{
    color: {theme.TEXT};
    font-family: {theme.UI_CSS};
    font-size: 11px;
    font-weight: 600;
    padding: 0 4px;
}}
"""


class _Edit(QTextEdit):
    """The text area itself. Enter sends, Shift+Enter makes a newline -
    the convention every web chat UI uses."""

    submitted = Signal()
    changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setStyleSheet(_EDIT_STYLE)
        self.setPlaceholderText("Type a prompt, or press your insert-prompt hotkey...")
        self.setAcceptRichText(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QTextEdit.Shape.NoFrame)
        self.document().setDocumentMargin(0)
        self.textChanged.connect(self._autosize)
        self.textChanged.connect(self.changed)
        self._autosize()

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        enter = event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        if enter and not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.submitted.emit()
            return
        super().keyPressEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        # Height depends on how the text wraps, which depends on width - so
        # a height computed before the first layout pass is wrong (it reads
        # as a single line however much text is in there). Recompute
        # whenever the width actually changes.
        super().resizeEvent(event)
        self._autosize()

    def _autosize(self) -> None:
        """Grow to fit, then stop and scroll - never let the composer eat
        the whole window on a long prompt."""
        self.document().setTextWidth(self.viewport().width())
        wanted = int(self.document().size().height()) + 4
        capped = max(_MIN_HEIGHT - 20, min(wanted, _MAX_HEIGHT - 20))
        self.setFixedHeight(capped)
        self.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
            if wanted > _MAX_HEIGHT - 20
            else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )


class Composer(QWidget):
    """Rounded input surface pinned to the bottom of the window."""

    submitted = Signal()

    def __init__(self) -> None:
        super().__init__()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 8, 16, 12)
        outer.setSpacing(6)

        # The rounded box. Styled on a named child so the radius applies to
        # this frame only and not to every descendant widget.
        self._box = QWidget()
        self._box.setObjectName("composerBox")
        self._box.setStyleSheet(_BOX_STYLE_IDLE)
        box_layout = QHBoxLayout(self._box)
        box_layout.setContentsMargins(14, 10, 10, 10)
        box_layout.setSpacing(10)

        self._edit = _Edit()
        self._edit.submitted.connect(self.submitted)
        self._edit.changed.connect(self._sync_send_enabled)
        box_layout.addWidget(self._edit, 1)

        self._send = QPushButton("↑")  # up arrow, same as the web UIs
        self._send.setStyleSheet(_SEND_STYLE)
        self._send.setCursor(Qt.CursorShape.PointingHandCursor)
        self._send.setToolTip("Send")
        self._send.clicked.connect(self.submitted)
        box_layout.addWidget(self._send, 0, Qt.AlignmentFlag.AlignBottom)

        outer.addWidget(self._box)

        # Footer: live status on the left, static key hints on the right.
        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(10)

        self._badge = QLabel("CAPTURE MODE")
        self._badge.setStyleSheet(_BADGE_STYLE)
        self._badge.hide()
        footer.addWidget(self._badge, 0)

        self._status = QLabel("")
        self._status.setStyleSheet(_STATUS_STYLE)
        footer.addWidget(self._status, 0)
        footer.addStretch(1)

        self._hint = QLabel("Enter to send  ·  Shift+Enter for a new line")
        self._hint.setStyleSheet(_HINT_STYLE)
        footer.addWidget(self._hint, 0)

        outer.addLayout(footer)

        self._image_count = 0
        self._status_text = ""
        self._refresh_status()
        self._sync_send_enabled()

    # -- text, the queued prompt itself ---------------------------------
    def text(self) -> str:
        return self._edit.toPlainText()

    def set_text(self, value: str) -> None:
        self._edit.setPlainText(value)

    def append(self, value: str) -> None:
        """Append at the end and keep the caret visible - used by the
        insert-prompt hotkey and by capture mode's live typing."""
        cursor = self._edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(value)
        self._edit.setTextCursor(cursor)
        self._edit.ensureCursorVisible()

    def backspace(self) -> None:
        cursor = self._edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.deletePreviousChar()
        self._edit.setTextCursor(cursor)
        self._edit.ensureCursorVisible()

    def clear(self) -> None:
        self._edit.clear()

    def focus_input(self) -> None:
        self._edit.setFocus()

    # -- background capture mode ----------------------------------------
    def post_key(self, qt_key: int, modifiers: Qt.KeyboardModifier, text: str) -> None:
        """Replay one captured keystroke into the text area.

        Posted rather than applied by hand so Qt's own editing does the
        work - selection, word jumps, Ctrl+V/A/Z, Home/End all behave
        normally. postEvent delivers straight to this widget, so it needs
        no focus and steals none.
        """
        app = QApplication.instance()
        if app is None:
            return
        # Enter is the send gesture, not a newline, matching what typing
        # into the box directly does (_Edit.keyPressEvent).
        if qt_key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
            modifiers & Qt.KeyboardModifier.ShiftModifier
        ):
            self.submitted.emit()
            return
        app.postEvent(
            self._edit, QKeyEvent(QEvent.Type.KeyPress, qt_key, modifiers, text)
        )

    def set_capture_mode(self, active: bool) -> None:
        self._box.setStyleSheet(_BOX_STYLE_CAPTURING if active else _BOX_STYLE_IDLE)
        self._badge.setVisible(active)
        self._edit.setPlaceholderText(
            "Capture mode on - every keystroke is typed here. Press the toggle again to stop."
            if active
            else "Type a prompt, or press your insert-prompt hotkey..."
        )

    # -- attached-image count (a number, never a preview) ----------------
    def set_image_count(self, count: int) -> None:
        self._image_count = count
        self._refresh_status()
        self._sync_send_enabled()

    def set_status(self, text: str) -> None:
        """Live state - sending, a failure. Idle wording is
        dropped rather than shown, so the row is empty when nothing is
        happening instead of saying so."""
        self._status_text = "" if text.strip().lower().startswith("ready") else text.strip()
        self._refresh_status()

    def _refresh_status(self) -> None:
        bits = []
        if self._image_count:
            bits.append(f"{self._image_count} screenshot{'s' if self._image_count != 1 else ''} attached")
        if self._status_text:
            bits.append(self._status_text)
        self._status.setText("   ·   ".join(bits))

    def _sync_send_enabled(self) -> None:
        self._send.setEnabled(bool(self._image_count) or bool(self.text().strip()))
