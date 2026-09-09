"""Centralized constants for paths, app metadata and defaults."""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "ThumbnailAssistant"
APP_DISPLAY_NAME = "Thumbnail Assistant"

# --- Filesystem locations -------------------------------------------------
# %APPDATA%\ThumbnailAssistant            -> config.json
# %LOCALAPPDATA%\ThumbnailAssistant\logs  -> app.log


def _appdata_dir() -> Path:
    base = os.environ.get("APPDATA")
    if not base:
        base = str(Path.home() / ".config")
    return Path(base) / APP_NAME


def _local_appdata_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = str(Path.home() / ".cache")
    return Path(base) / APP_NAME


CONFIG_DIR = _appdata_dir()
CONFIG_FILE = CONFIG_DIR / "config.json"

LOCAL_DATA_DIR = _local_appdata_dir()
LOG_DIR = LOCAL_DATA_DIR / "logs"
LOG_FILE = LOG_DIR / "app.log"

# --- Defaults --------------------------------------------------------------
DEFAULT_WINDOW_WIDTH = 900
DEFAULT_WINDOW_HEIGHT = 700

# Default prompt text used when the user hasn't configured one (see
# config.py: AppConfig.capture_prompt). Single source of truth, same
# pattern as the old adapter-supplied default_capture_text().
DEFAULT_CAPTURE_PROMPT = "Describe what's in this screenshot."

# All actions are core/framework actions now - there is no adapter concept
# post-rewrite (no more per-site adapters; Gemini is called directly).
DEFAULT_HOTKEYS = {
    # action_name: "modifier+modifier+key" (lowercase)
    "toggle_visibility": "ctrl+h",
    "capture_screenshot_attach": "ctrl+alt+g",
    "toggle_voice_capture": "ctrl+alt+v",
    "move_up": "alt+up",
    "move_down": "alt+down",
    "move_left": "alt+left",
    "move_right": "alt+right",
    "increase_height": "alt+shift+up",
    "decrease_height": "alt+shift+down",
    "decrease_width": "alt+shift+left",
    "increase_width": "alt+shift+right",
    "focus_window": "ctrl+alt+f",
    "increase_opacity": "alt+=",
    "decrease_opacity": "alt+-",
    "open_settings": "ctrl+alt+s",
    "capture_mode_toggle": "capslock+t",
    "insert_configured_prompt": "capslock+p",
    "send_message": "capslock+enter",
    "clear_queue": "capslock+backspace",
}

# Human-readable labels for the settings window.
CORE_ACTION_LABELS = {
    "toggle_visibility": "Show / hide window",
    "capture_screenshot_attach": "Capture screenshot & queue (no send)",
    "toggle_voice_capture": "Start/stop voice capture & send transcript to Gemini",
    "open_settings": "Open settings",
    "move_up": "Move window up",
    "move_down": "Move window down",
    "move_left": "Move window left",
    "move_right": "Move window right",
    "increase_width": "Increase window width",
    "decrease_width": "Decrease window width",
    "increase_height": "Increase window height",
    "decrease_height": "Decrease window height",
    "focus_window": "Focus window (click/type)",
    "increase_opacity": "Increase window opacity",
    "decrease_opacity": "Decrease window opacity",
    "capture_mode_toggle": "Toggle background capture mode (types live, silently, into the queued prompt - no auto-send)",
    "insert_configured_prompt": "Insert configured prompt into queue (no send)",
    "send_message": "Send queued screenshots + prompt to Gemini",
    "clear_queue": "Discard everything queued (without sending)",
}

IS_WINDOWS = sys.platform.startswith("win")
