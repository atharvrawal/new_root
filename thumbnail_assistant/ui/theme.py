"""One palette, shared by the window and the message widgets so they can't
drift apart.

Strictly greyscale: black, dark greys, white text. No hues anywhere - state
is carried by brightness and by wording, never by colour. Keep it that way
when adding anything here.

The greys are spread wide on purpose. This window spends much of its life
semi-transparent (the opacity hotkeys go down to alpha 40/255), and
closely-spaced greys collapse into each other once faded.
"""
from __future__ import annotations

# -- palette (greyscale only) ----------------------------------------------
BG = "#1a1a1a"           # window ground
BG_RAISED = "#222222"    # status bar
BG_CODE = "#000000"      # code slab - black, so code reads as inset
BORDER = "#333333"
BORDER_STRONG = "#4a4a4a"

TEXT = "#ffffff"         # body copy
TEXT_DIM = "#b0b0b0"     # secondary text that must still be readable faded
TEXT_FAINT = "#808080"   # decoration only - never put meaning here

# -- fonts -----------------------------------------------------------------
# Families are passed to Qt as a fallback list; the first installed one wins.
UI_FAMILIES = ["Segoe UI Variable Text", "Segoe UI", "Inter", "system-ui", "Arial"]
MONO_FAMILIES = ["Cascadia Mono", "Consolas", "JetBrains Mono", "Courier New", "monospace"]

UI_CSS = '"Segoe UI Variable Text", "Segoe UI", Inter, system-ui, Arial, sans-serif'
MONO_CSS = '"Cascadia Mono", Consolas, "JetBrains Mono", "Courier New", monospace'

BODY_PT = 10.5           # point sizes - Qt document formatting works in points
CODE_PT = 10.0
H1_PT = 14.0
H2_PT = 12.5
H3_PT = 11.5

LINE_HEIGHT_PCT = 145    # body leading; code stays at 100 (set in message_widget)
