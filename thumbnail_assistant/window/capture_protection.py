"""Screen-capture exclusion for this application's own window, using the
official Win32 mechanism intended for exactly this purpose:
``SetWindowDisplayAffinity`` with ``WDA_EXCLUDEFROMCAPTURE``.

Reference: https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-setwindowdisplayaffinity

What this does and does not do
-------------------------------
``WDA_EXCLUDEFROMCAPTURE`` tells the Desktop Window Manager to omit this
specific window's surface from the output of supported Windows capture
APIs (e.g. the Windows Graphics Capture API, ``BitBlt``/``PrintWindow``
against the DWM-composited surface, and consumers built on those such as
screen sharing in many conferencing apps). The window continues to render
completely normally on the user's own monitor(s).

This module deliberately does nothing else: no DRM, no anti-recording, no
enforcement against capture methods outside the Windows compositor (see the
module-level docstring in the README for the full list of things this does
NOT protect against). It only ever touches the single HWND it is given -
never other windows, never system-wide state.

Design notes
------------
* Only this window is ever affected: we require an explicit ``hwnd`` on
  every call and never enumerate or touch other windows.
* Failure is always non-fatal. Every expected failure mode (unsupported
  OS, DWM unavailable, invalid hwnd, API returning FALSE, or simply not
  running on Windows) is caught, logged once with enough context to
  diagnose, and swallowed so the caller can continue running normally.
* Thread-safety: these functions hold no shared mutable state and only
  make a single stateless Win32 call per invocation, so they are safe to
  call from any thread without additional locking.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import sys

from .. import constants

logger = logging.getLogger(__name__)

# -- Win32 constants (winuser.h) --------------------------------------------
WDA_NONE = 0x00000000
WDA_EXCLUDEFROMCAPTURE = 0x00000011  # requires Windows 10 2004 (build 19041)+

_MIN_BUILD_FOR_EXCLUDE_FROM_CAPTURE = 19041

# Lazily-initialized WinDLL handle with use_last_error=True so GetLastError()
# reflects calls made through *this* handle specifically.
_user32 = None


def _get_user32():
    global _user32
    if _user32 is None:
        _user32 = ctypes.WinDLL("user32", use_last_error=True)
        _user32.SetWindowDisplayAffinity.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.DWORD]
        _user32.SetWindowDisplayAffinity.restype = ctypes.wintypes.BOOL
        _user32.IsWindow.argtypes = [ctypes.wintypes.HWND]
        _user32.IsWindow.restype = ctypes.wintypes.BOOL
    return _user32


def _windows_build_number() -> int:
    try:
        return sys.getwindowsversion().build  # type: ignore[attr-defined]
    except Exception:
        return 0


def _supports_exclude_from_capture() -> bool:
    return _windows_build_number() >= _MIN_BUILD_FOR_EXCLUDE_FROM_CAPTURE


def _set_affinity(hwnd: int, affinity: int, *, context: str) -> bool:
    """Shared implementation for enable/disable. Returns True on success,
    False on any failure - never raises."""
    if not constants.IS_WINDOWS:
        logger.debug("Capture protection skipped: not running on Windows.")
        return False

    if not hwnd:
        logger.warning("Capture protection (%s) skipped: no HWND available.", context)
        return False

    try:
        user32 = _get_user32()
    except Exception:
        logger.exception("Capture protection (%s): failed to load user32.dll.", context)
        return False

    try:
        if not user32.IsWindow(hwnd):
            logger.warning(
                "Capture protection (%s) skipped: HWND %s is not a valid window.",
                context,
                hwnd,
            )
            return False
    except Exception:
        logger.exception("Capture protection (%s): IsWindow check failed.", context)
        return False

    if affinity == WDA_EXCLUDEFROMCAPTURE and not _supports_exclude_from_capture():
        logger.warning(
            "Capture protection unavailable on this version of Windows "
            "(build %s; requires build %s or later).",
            _windows_build_number(),
            _MIN_BUILD_FOR_EXCLUDE_FROM_CAPTURE,
        )
        return False

    try:
        success = bool(user32.SetWindowDisplayAffinity(hwnd, affinity))
    except OSError as exc:
        # ctypes surfaces some failures (e.g. calling convention/arg issues)
        # as OSError; treat identically to an API-level failure.
        logger.error("SetWindowDisplayAffinity failed: %s", exc)
        return False
    except Exception:
        logger.exception("Unexpected error calling SetWindowDisplayAffinity.")
        return False

    if not success:
        error_code = ctypes.get_last_error()
        reason = _describe_error(error_code)
        logger.error("SetWindowDisplayAffinity failed: %s", reason)
        return False

    return True


def _describe_error(error_code: int) -> str:
    """Best-effort human-readable reason for a failed call, including a
    specific hint for the well-known "DWM unavailable" case."""
    # ERROR_ACCESS_DENIED is what SetWindowDisplayAffinity tends to surface
    # when the Desktop Window Manager is not running/available (e.g. some
    # remote session or reduced-functionality configurations).
    ERROR_ACCESS_DENIED = 5
    if error_code == ERROR_ACCESS_DENIED:
        return "Desktop Window Manager is unavailable."
    try:
        message = ctypes.FormatError(error_code)
    except Exception:
        message = "unknown error"
    return f"Win32 error {error_code} ({message})"


# -- Public API ---------------------------------------------------------
def enable_capture_protection(hwnd: int) -> bool:
    """Exclude ``hwnd`` from supported Windows screen-capture APIs via
    ``SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)``.

    Always safe to call: any failure (unsupported OS, missing/invalid hwnd,
    DWM unavailable, non-Windows platform, or the API itself failing) is
    logged and swallowed. Returns True if protection was successfully
    enabled, False otherwise.
    """
    ok = _set_affinity(hwnd, WDA_EXCLUDEFROMCAPTURE, context="enable")
    if ok:
        logger.info("Capture protection enabled.")
    return ok


def disable_capture_protection(hwnd: int) -> bool:
    """Restore normal capture behavior for ``hwnd`` via
    ``SetWindowDisplayAffinity(hwnd, WDA_NONE)``. Provided for completeness/
    future use (e.g. a future user-facing toggle); not called anywhere by
    default since capture protection is always-on per current requirements.
    """
    ok = _set_affinity(hwnd, WDA_NONE, context="disable")
    if ok:
        logger.info("Capture protection disabled.")
    return ok
