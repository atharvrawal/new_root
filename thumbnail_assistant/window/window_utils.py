"""Win32 helpers for manipulating window visibility without stealing focus
from whatever application currently has it (e.g. a game).

Qt's own ``show()`` activates the window and steals focus. To satisfy the
"never steal focus unless explicitly requested" requirement we operate on
the underlying HWND directly via ``ShowWindow`` with ``SW_SHOWNOACTIVATE``
for the "peek" case, and only use a focus-stealing show for the
``focus_window`` hotkey, where the keypress is itself the explicit request
to bring the window to the foreground.

This is also why window visibility is read back via ``IsWindowVisible``
rather than Qt's ``isVisible()``: the raw ShowWindow calls below leave Qt's
own bookkeeping out of sync, so Win32 is the only reliable source of truth
for whether the window is actually on screen.
"""
from __future__ import annotations

import ctypes
import logging

logger = logging.getLogger(__name__)

user32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None  # type: ignore[attr-defined]

SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
SW_SHOWMINIMIZED = 2
SW_SHOWNORMAL = 1
SW_RESTORE = 9

HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE_NOZORDER = SWP_NOACTIVATE | SWP_NOZORDER

# Guardrails for keyboard-driven move/resize (see move_window_by /
# resize_window_by below).
MIN_WINDOW_WIDTH = 400
MIN_WINDOW_HEIGHT = 300

# Minimum amount of the window that must remain reachable on-screen when
# clamping a move; this deliberately mirrors typical Windows "keep window
# accessible" behavior rather than allowing a fully off-screen window.
_MIN_VISIBLE_MARGIN = 40

GWL_EXSTYLE = -20
WS_EX_APPWINDOW = 0x00040000
WS_EX_TOOLWINDOW = 0x00000080

WS_EX_LAYERED = 0x00080000
LWA_ALPHA = 0x00000002

MIN_OPACITY = 10   # out of 255 - keep it from ever becoming invisible/unreachable
MAX_OPACITY = 255

# DWM window-attribute constants (Windows 11 22H2+) used solely to suppress
# the thin native border DWM draws around frameless windows. Not available
# on older Windows 10 builds - failures are logged and swallowed, matching
# the tolerant style of the rest of this module.
DWMWA_BORDER_COLOR = 34
DWMWA_COLOR_NONE = 0xFFFFFFFE
dwmapi = ctypes.windll.dwmapi if hasattr(ctypes, "windll") else None  # type: ignore[attr-defined]

class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


if user32 is not None:
    user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(RECT)]
    user32.GetWindowRect.restype = ctypes.c_bool
    user32.SetWindowPos.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    user32.SetWindowPos.restype = ctypes.c_bool
    user32.MonitorFromWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    user32.MonitorFromWindow.restype = ctypes.c_void_p
    user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.GetWindowLongPtrW.restype = ctypes.c_long
    user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
    user32.SetWindowLongPtrW.restype = ctypes.c_long
    user32.SetLayeredWindowAttributes.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_ubyte, ctypes.c_uint,
    ]
    user32.SetLayeredWindowAttributes.restype = ctypes.c_bool


def show_no_activate(hwnd: int) -> None:
    if not hwnd:
        return
    user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)


def show_and_activate(hwnd: int) -> None:
    if not hwnd:
        return
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)


def hide_window(hwnd: int) -> None:
    if not hwnd:
        return
    user32.ShowWindow(hwnd, SW_HIDE)


def minimize_window(hwnd: int) -> None:
    if not hwnd:
        return
    user32.ShowWindow(hwnd, SW_SHOWMINIMIZED)


def is_window_visible(hwnd: int) -> bool:
    if not hwnd:
        return False
    return bool(user32.IsWindowVisible(hwnd))

def hide_from_taskbar_and_alttab(hwnd: int) -> bool:
    """Exclude hwnd from the taskbar and Alt+Tab by swapping WS_EX_APPWINDOW
    for WS_EX_TOOLWINDOW. The window keeps rendering normally; it's just no
    longer enumerated as a switchable top-level app window. Toggling this
    briefly hides+reshows the window (required for DWM to pick up the
    style change) using SW_SHOWNOACTIVATE so focus is never stolen."""
    if user32 is None or not hwnd or not user32.IsWindow(hwnd):
        logger.warning("hide_from_taskbar_and_alttab: invalid hwnd %s; ignoring.", hwnd)
        return False

    try:
        ex_style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        new_style = (ex_style & ~WS_EX_APPWINDOW) | WS_EX_TOOLWINDOW
        user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, new_style)

        # DWM only re-evaluates taskbar/alt-tab membership on a visibility
        # transition, so briefly cycle it without activating.
        user32.ShowWindow(hwnd, SW_HIDE)
        user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        return True
    except Exception:
        logger.exception("Failed to set WS_EX_TOOLWINDOW on hwnd %s.", hwnd)
        return False
    
def remove_native_border(hwnd: int) -> bool:
    """Suppress the thin native border DWM draws around frameless windows
    (visible as a whitish outline) via DWMWA_BORDER_COLOR = none. Only
    affects that DWM-drawn accent border; has no effect on the window's
    own background/content. No-ops quietly on Windows versions that don't
    support this attribute (pre-Windows 11 22H2)."""
    if dwmapi is None or not hwnd or (user32 is not None and not user32.IsWindow(hwnd)):
        logger.warning("remove_native_border: invalid hwnd %s; ignoring.", hwnd)
        return False

    try:
        dwmapi.DwmSetWindowAttribute.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.c_uint,
        ]
        dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long

        color = ctypes.c_int(DWMWA_COLOR_NONE)
        result = dwmapi.DwmSetWindowAttribute(
            hwnd,
            DWMWA_BORDER_COLOR,
            ctypes.byref(color),
            ctypes.sizeof(color),
        )
        if result != 0:
            logger.debug(
                "DwmSetWindowAttribute(DWMWA_BORDER_COLOR) returned %s for hwnd %s "
                "(likely unsupported on this Windows version); border left as-is.",
                result, hwnd,
            )
            return False
        return True
    except Exception:
        logger.exception("Failed to remove native border on hwnd %s.", hwnd)
        return False


# -- geometry (move/resize by hotkey) ---------------------------------------
# These helpers back the "control the frameless window entirely via global
# hotkeys" feature. All Win32 specifics (SetWindowPos, RECT, monitor bounds)
# stay in this module; callers (ResponseWindow) only ever deal in plain
# ints, so the UI layer never touches Win32 directly.


def get_window_rect(hwnd: int):
    """Return (x, y, width, height) for hwnd, or None if it can't be read."""
    if not hwnd or user32 is None:
        return None
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        logger.warning("GetWindowRect failed for hwnd %s.", hwnd)
        return None
    return (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)

def set_topmost(hwnd: int, topmost: bool = True) -> None:
    """Pin (or unpin) hwnd as an always-on-top window, without moving,
    resizing, or activating it. Lets the window render above a fullscreen
    game (borderless/windowed fullscreen only - true exclusive-fullscreen
    bypasses the compositor and can't be overlaid by any window)."""
    if user32 is None or not hwnd or not user32.IsWindow(hwnd):
        logger.warning("set_topmost: invalid hwnd %s; ignoring.", hwnd)
        return
    insert_after = HWND_TOPMOST if topmost else HWND_NOTOPMOST
    ok = user32.SetWindowPos(
        hwnd, insert_after, 0, 0, 0, 0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
    )
    if not ok:
        logger.warning("SetWindowPos (topmost=%s) failed for hwnd %s.", topmost, hwnd)


def set_window_opacity(hwnd: int, alpha: int) -> int:
    """Set whole-window opacity via the layered-window mechanism
    (WS_EX_LAYERED + SetLayeredWindowAttributes). alpha is 0-255; clamped
    to [MIN_OPACITY, MAX_OPACITY] so the window can never fade to fully
    invisible-and-unreachable. Returns the clamped alpha actually applied,
    or the unmodified input on failure (never raises)."""
    if user32 is None or not hwnd or not user32.IsWindow(hwnd):
        logger.warning("set_window_opacity: invalid hwnd %s; ignoring.", hwnd)
        return alpha

    clamped = max(MIN_OPACITY, min(alpha, MAX_OPACITY))

    try:
        ex_style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        if not (ex_style & WS_EX_LAYERED):
            user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex_style | WS_EX_LAYERED)

        ok = user32.SetLayeredWindowAttributes(hwnd, 0, clamped, LWA_ALPHA)
        if not ok:
            logger.warning("SetLayeredWindowAttributes failed for hwnd %s.", hwnd)
    except Exception:
        logger.exception("Failed to set opacity on hwnd %s.", hwnd)

    return clamped


def _work_area_for_window(hwnd: int):
    """Best-effort monitor work-area (x, y, width, height) for the monitor
    hwnd currently sits on, used to clamp moves so the window can't be
    dragged fully off-screen. Falls back to None on any failure, in which
    case callers should skip clamping rather than fail the whole action."""
    try:
        MONITOR_DEFAULTTONEAREST = 2
        monitor = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        if not monitor:
            return None

        class MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_ulong),
                ("rcMonitor", RECT),
                ("rcWork", RECT),
                ("dwFlags", ctypes.c_ulong),
            ]

        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None
        work = info.rcWork
        return (work.left, work.top, work.right - work.left, work.bottom - work.top)
    except Exception:
        logger.debug("Could not resolve monitor work area for clamping.", exc_info=True)
        return None


def _clamp_position(x: int, y: int, width: int, height: int, hwnd: int):
    """Clamp (x, y) so at least _MIN_VISIBLE_MARGIN px of the window stays
    within the monitor's work area on every side. Skips clamping (returns
    the input unchanged) if the work area can't be determined."""
    work_area = _work_area_for_window(hwnd)
    if work_area is None:
        return x, y

    wa_x, wa_y, wa_w, wa_h = work_area
    min_x = wa_x - width + _MIN_VISIBLE_MARGIN
    max_x = wa_x + wa_w - _MIN_VISIBLE_MARGIN
    min_y = wa_y - height + _MIN_VISIBLE_MARGIN
    max_y = wa_y + wa_h - _MIN_VISIBLE_MARGIN

    clamped_x = max(min_x, min(x, max_x))
    clamped_y = max(min_y, min(y, max_y))
    return clamped_x, clamped_y


def move_window_by(hwnd: int, dx: int, dy: int) -> None:
    """Move ``hwnd`` by (dx, dy) pixels, preserving its current size,
    Z-order, and focus (SWP_NOACTIVATE + SWP_NOZORDER + SWP_NOSIZE). Movement
    is clamped so the window can't be pushed fully off any monitor. Invalid
    or missing HWNDs are logged and ignored rather than raised."""
    if user32 is None:
        return
    if not hwnd or not user32.IsWindow(hwnd):
        logger.warning("move_window_by: invalid hwnd %s; ignoring.", hwnd)
        return

    rect = get_window_rect(hwnd)
    if rect is None:
        return
    x, y, width, height = rect

    new_x, new_y = _clamp_position(x + dx, y + dy, width, height, hwnd)

    ok = user32.SetWindowPos(
        hwnd, 0, new_x, new_y, 0, 0, SWP_NOSIZE | SWP_NOACTIVATE_NOZORDER
    )
    if not ok:
        logger.warning("SetWindowPos (move) failed for hwnd %s.", hwnd)


def resize_window_by(hwnd: int, dwidth: int, dheight: int) -> None:
    """Resize ``hwnd`` by (dwidth, dheight) pixels, keeping the current
    top-left corner fixed and expanding/shrinking toward the right/bottom.
    Clamped to MIN_WINDOW_WIDTH/MIN_WINDOW_HEIGHT. Preserves Z-order and
    never steals focus. Invalid or missing HWNDs are logged and ignored."""
    if user32 is None:
        return
    if not hwnd or not user32.IsWindow(hwnd):
        logger.warning("resize_window_by: invalid hwnd %s; ignoring.", hwnd)
        return

    rect = get_window_rect(hwnd)
    if rect is None:
        return
    x, y, width, height = rect

    new_width = max(MIN_WINDOW_WIDTH, width + dwidth)
    new_height = max(MIN_WINDOW_HEIGHT, height + dheight)

    ok = user32.SetWindowPos(
        hwnd, 0, x, y, new_width, new_height, SWP_NOMOVE | SWP_NOACTIVATE_NOZORDER
    )
    if not ok:
        logger.warning("SetWindowPos (resize) failed for hwnd %s.", hwnd)
