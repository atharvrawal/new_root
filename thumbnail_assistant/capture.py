"""Screenshot capture for the capture-and-send feature.

Deliberately minimal: this module's only job is "grab a screenshot, return
it as a base64-encoded PNG". It knows nothing about the queue, the UI, or
Gemini - see ``ui/app_controller.py`` (capture_and_attach) for what happens
to the image afterwards.

Library choice: ``mss`` vs. Pillow's ``ImageGrab``
----------------------------------------------------
``mss`` is a small, dependency-free screenshot library (pure ctypes on
Windows) that's already fast enough for this use case and returns raw
pixel data directly - no need to pull in Pillow just to encode a PNG,
since ``mss.tools.to_png`` does that itself in the standard library
(``zlib``/``struct``). Keeps the dependency list as small as the rest of
this project.

Note: this takes a screenshot of the monitor, not of any specific window -
same as pressing Print Screen. It captures whatever is actually on your
screen at the moment the hotkey is pressed, same as the manual workflow
this app replaces.
"""
from __future__ import annotations

import base64
import logging
from typing import Optional

import mss
import mss.tools

logger = logging.getLogger(__name__)

# Index into mss's monitor list. Index 0 is the special "all monitors
# combined" virtual screen; index 1 is the primary monitor. Primary is the
# sane default for a single-monitor screenshot-to-thumbnail workflow - if
# you run a multi-monitor setup and want the whole virtual desktop
# instead, change this to 0.
_MONITOR_INDEX = 1

def capture_screen_png_base64(monitor_index: Optional[int] = None) -> Optional[str]:
    """Capture a screenshot and return it as a base64-encoded PNG string,
    or ``None`` on any failure (never raises - callers should treat a
    ``None`` return as "skip this capture, log already written")."""
    try:
        with mss.mss() as sct:
            monitors = sct.monitors
            index = monitor_index if monitor_index is not None else _MONITOR_INDEX
            index = index if 0 <= index < len(monitors) else 0
            shot = sct.grab(monitors[index])
            png_bytes = mss.tools.to_png(shot.rgb, shot.size)
            if not png_bytes:
                logger.warning("Screenshot capture produced no PNG data.")
                return None
            return base64.b64encode(png_bytes).decode("ascii")
    except Exception:
        logger.exception("Screenshot capture failed.")
        return None
