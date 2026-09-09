"""A single rendered response, appended to the scroll area in
response_window.py.

Why the document is restyled by hand
------------------------------------
``QTextBrowser.setMarkdown()`` renders markdown straight into the internal
document and never consults ``setDefaultStyleSheet()`` - that only applies
to HTML parsed via ``setHtml()``. So CSS is not available to us, and code
blocks come out as undifferentiated body text, which is the single worst
thing that can happen to a window whose main job is displaying code.

Qt does, however, tag markdown fences with ``BlockCodeFence`` and
``BlockCodeLanguage`` block properties. :func:`_restyle` walks the finished
document and applies real formatting off the back of those - monospace,
inset background, tight leading for code; proportional font and generous
leading for prose. That is the whole trick, and it needs no HTML
round-trip and no markdown dependency.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextFormat,
    QTextTable,
    QTextTableFormat,
)
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QTextBrowser, QVBoxLayout, QWidget

from . import theme

_BROWSER_STYLE = f"""
QTextBrowser {{
    background: transparent;
    border: none;
    color: {theme.TEXT};
    selection-background-color: #ffffff;
    selection-color: #000000;
}}
"""

_HEADER_STYLE = f"""
QLabel {{
    color: {theme.TEXT_FAINT};
    font-family: {theme.UI_CSS};
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 1px;
}}
"""

_ERROR_TAG_STYLE = f"""
QLabel {{
    color: {theme.TEXT};
    font-family: {theme.UI_CSS};
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 1px;
}}
"""


# setLineHeight wants the height-type as a plain int, not the enum member.
_PROPORTIONAL = QTextBlockFormat.LineHeightTypes.ProportionalHeight.value


def _has_fence(block) -> bool:
    return block.blockFormat().hasProperty(QTextFormat.Property.BlockCodeFence)


def _restyle(doc) -> None:
    """Walk every block and apply real typography. Runs once, after
    setMarkdown() has built the document."""
    cursor = QTextCursor(doc)
    cursor.beginEditBlock()

    block = doc.begin()
    while block.isValid():
        in_code = _has_fence(block)
        cursor.setPosition(block.position())
        cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)

        bf = QTextBlockFormat(block.blockFormat())
        cf = QTextCharFormat()

        if in_code:
            # Consecutive fenced lines are separate blocks. Padding goes on
            # the first and last of a run only, so the background reads as
            # one continuous slab instead of striped rows.
            first = not _has_fence(block.previous()) or not block.previous().isValid()
            last = not _has_fence(block.next()) or not block.next().isValid()

            bf.setBackground(QColor(theme.BG_CODE))
            bf.setLeftMargin(14)
            bf.setRightMargin(14)
            bf.setTopMargin(10 if first else 0)
            bf.setBottomMargin(10 if last else 0)
            # Exactly 100%: anything above it leaves an unpainted gap
            # between consecutive blocks, which stripes the slab.
            bf.setLineHeight(100, _PROPORTIONAL)
            bf.setNonBreakableLines(True)

            cf.setFontFamilies(theme.MONO_FAMILIES)
            cf.setFontFixedPitch(True)
            cf.setFontPointSize(theme.CODE_PT)
            cf.setForeground(QColor(theme.TEXT))
        else:
            level = bf.headingLevel()
            bf.setLineHeight(theme.LINE_HEIGHT_PCT, _PROPORTIONAL)
            bf.setTopMargin(10 if level else 4)
            bf.setBottomMargin(4)

            cf.setFontFamilies(theme.UI_FAMILIES)
            if level == 1:
                cf.setFontPointSize(theme.H1_PT)
                cf.setFontWeight(QFont.Weight.DemiBold)
            elif level == 2:
                cf.setFontPointSize(theme.H2_PT)
                cf.setFontWeight(QFont.Weight.DemiBold)
            elif level >= 3:
                cf.setFontPointSize(theme.H3_PT)
                cf.setFontWeight(QFont.Weight.DemiBold)
            else:
                cf.setFontPointSize(theme.BODY_PT)
            cf.setForeground(QColor(theme.TEXT))

        cursor.setBlockFormat(bf)
        cursor.mergeCharFormat(cf)
        # List markers ("1.", bullets) are painted from the block char
        # format rather than from any fragment, so without this they stay
        # at the default size and look shrunken next to their own text.
        cursor.mergeBlockCharFormat(cf)

        # Inline `code` keeps whatever fixed-pitch run Qt gave it, but at
        # the body font size and without the block treatment - re-apply the
        # mono family per-fragment so the merge above didn't flatten it.
        if not in_code:
            for fragment in _fragments(block):
                if fragment.charFormat().fontFixedPitch():
                    inline = QTextCharFormat()
                    inline.setFontFamilies(theme.MONO_FAMILIES)
                    inline.setFontFixedPitch(True)
                    inline.setFontPointSize(theme.CODE_PT)
                    inline.setBackground(QColor(theme.BG_CODE))
                    inline.setForeground(QColor(theme.TEXT))
                    c = QTextCursor(doc)
                    c.setPosition(fragment.position())
                    c.setPosition(fragment.position() + fragment.length(), QTextCursor.MoveMode.KeepAnchor)
                    c.mergeCharFormat(inline)

        block = block.next()

    _restyle_tables(doc)
    cursor.endEditBlock()


def _restyle_tables(doc) -> None:
    """Qt draws markdown tables with a default light border, which on a
    dark ground reads as a harsh white grid that fights the text. Repaint
    them in the theme's border colour and give the cells real padding."""
    for frame in doc.rootFrame().childFrames():
        if not isinstance(frame, QTextTable):
            continue
        fmt = frame.format().toTableFormat()
        fmt.setBorder(1)
        fmt.setBorderBrush(QColor(theme.BORDER))
        fmt.setBorderStyle(QTextTableFormat.BorderStyle.BorderStyle_Solid)
        fmt.setBorderCollapse(True)
        fmt.setCellPadding(7)
        fmt.setCellSpacing(0)
        frame.setFormat(fmt)


def _fragments(block):
    it = block.begin()
    while not it.atEnd():
        fragment = it.fragment()
        if fragment.isValid():
            yield fragment
        it += 1


class MessageWidget(QWidget):
    """One markdown-rendered Gemini response, self-sizing to its content."""

    def __init__(
        self,
        markdown_text: str,
        *,
        index: int = 0,
        timestamp: str = "",
        is_error: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"background-color: {theme.BG};")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 18)
        layout.setSpacing(8)

        layout.addWidget(self._build_header(index, timestamp, is_error))

        browser = QTextBrowser()
        browser.setStyleSheet(_BROWSER_STYLE)
        browser.setOpenExternalLinks(True)
        browser.setFrameShape(QFrame.Shape.NoFrame)
        browser.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        browser.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        browser.setSizeAdjustPolicy(QTextBrowser.SizeAdjustPolicy.AdjustToContents)
        browser.document().setDocumentMargin(0)

        browser.setMarkdown(markdown_text)
        _restyle(browser.document())

        browser.document().setTextWidth(browser.viewport().width() or 600)
        self._fit(browser)
        browser.document().documentLayout().documentSizeChanged.connect(
            lambda _size: self._fit(browser)
        )

        layout.addWidget(browser)
        self._browser = browser

    def _build_header(self, index: int, timestamp: str, is_error: bool) -> QWidget:
        """A small caption above each response. Without it, consecutive
        answers run together into one wall of text and it's genuinely hard
        to see where the newest one starts."""
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(8)

        label = QLabel(f"#{index:02d}" + (f"   {timestamp}" if timestamp else ""))
        label.setStyleSheet(_HEADER_STYLE)
        row_layout.addWidget(label)

        if is_error:
            tag = QLabel("ERROR")
            tag.setStyleSheet(_ERROR_TAG_STYLE)
            row_layout.addWidget(tag)

        row_layout.addStretch(1)

        rule = QFrame()
        rule.setFrameShape(QFrame.Shape.HLine)
        rule.setStyleSheet(
            f"background-color: {theme.BORDER};"
            " max-height: 1px; min-height: 1px; border: none;"
        )
        rule.setMinimumWidth(40)
        row_layout.addWidget(rule, 1)
        return row

    @staticmethod
    def _fit(browser: QTextBrowser) -> None:
        height = int(browser.document().size().height())
        browser.setFixedHeight(max(height + 4, 20))

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        self._browser.document().setTextWidth(self._browser.viewport().width())
