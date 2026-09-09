"""Typed application configuration with JSON persistence.

Configuration lives at ``%APPDATA%\\ThumbnailAssistant\\config.json``.
Loading is defensive: missing keys fall back to defaults and a corrupt file
is backed up rather than crashing the application.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from . import constants

logger = logging.getLogger(__name__)


@dataclass
class AppConfig:
    start_minimized: bool = False
    remember_window_position: bool = True
    window_width: int = constants.DEFAULT_WINDOW_WIDTH
    window_height: int = constants.DEFAULT_WINDOW_HEIGHT
    window_x: Optional[int] = None
    window_y: Optional[int] = None
    hotkeys: Dict[str, str] = field(default_factory=lambda: dict(constants.DEFAULT_HOTKEYS))
    hotkeys_enabled: bool = True
    window_opacity: int = 255

    # Selected microphone for the voice-capture feature (see voice.py).
    # None means "system default input device".
    mic_device_index: Optional[int] = None

    # mss monitor index for screenshot capture (see capture.py). None means
    # "use capture.py's own built-in default (_MONITOR_INDEX)".
    monitor_index: Optional[int] = None

    # User-customizable prompts, editable from the settings window. None
    # means "use constants.DEFAULT_CAPTURE_PROMPT / voice.py's own
    # default template" - kept as None rather than duplicating the
    # default text here, so there's a single source of truth.
    capture_prompt: Optional[str] = None
    voice_prompt_template: Optional[str] = None

    # Gemini API key, used directly by gemini/client.py. Replaces the old
    # adapter/webview-session-based auth entirely.
    gemini_api_key: Optional[str] = None
    # Model name, editable in case Gemini's available models change.
    gemini_model: str = "gemini-3.6-flash"

    # -- (de)serialization ---------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AppConfig":
        defaults = cls()
        merged_hotkeys = dict(constants.DEFAULT_HOTKEYS)
        merged_hotkeys.update(data.get("hotkeys", {}) or {})
        return cls(
            start_minimized=bool(data.get("start_minimized", defaults.start_minimized)),
            remember_window_position=bool(
                data.get("remember_window_position", defaults.remember_window_position)
            ),
            window_width=int(data.get("window_width", defaults.window_width)),
            window_height=int(data.get("window_height", defaults.window_height)),
            window_x=data.get("window_x", defaults.window_x),
            window_y=data.get("window_y", defaults.window_y),
            hotkeys=merged_hotkeys,
            hotkeys_enabled=bool(data.get("hotkeys_enabled", defaults.hotkeys_enabled)),
            window_opacity=int(data.get("window_opacity", defaults.window_opacity)),
            mic_device_index=(
                int(data["mic_device_index"])
                if data.get("mic_device_index") is not None
                else defaults.mic_device_index
            ),
            monitor_index=(
                int(data["monitor_index"])
                if data.get("monitor_index") is not None
                else defaults.monitor_index
            ),
            capture_prompt=data.get("capture_prompt", defaults.capture_prompt) or None,
            voice_prompt_template=data.get("voice_prompt_template", defaults.voice_prompt_template) or None,
            gemini_api_key=data.get("gemini_api_key", defaults.gemini_api_key) or None,
            gemini_model=str(data.get("gemini_model", defaults.gemini_model)) or defaults.gemini_model,
        )


class ConfigManager:
    """Loads/saves :class:`AppConfig` from/to disk and notifies listeners
    when the configuration changes (e.g. after the settings window saves).
    Unchanged from the pre-rewrite version - AppConfig's shape is the only
    thing that changed."""

    def __init__(self, path: Path = constants.CONFIG_FILE) -> None:
        self._path = path
        self._config = self._load()
        self._listeners = []

    @property
    def config(self) -> AppConfig:
        return self._config

    def _load(self) -> AppConfig:
        if not self._path.exists():
            logger.info("No config file found at %s; using defaults.", self._path)
            cfg = AppConfig()
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._write(cfg)
            return cfg

        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            cfg = AppConfig.from_dict(raw)
            logger.debug("Loaded configuration from %s", self._path)
            return cfg
        except Exception:
            logger.exception(
                "Failed to parse config file at %s; backing up and using defaults.",
                self._path,
            )
            try:
                backup = self._path.with_suffix(".json.bak")
                self._path.replace(backup)
            except Exception:
                logger.exception("Could not back up corrupt config file.")
            cfg = AppConfig()
            self._write(cfg)
            return cfg

    def _write(self, cfg: AppConfig) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(".json.tmp")
        try:
            tmp_path.write_text(json.dumps(cfg.to_dict(), indent=2), encoding="utf-8")
            tmp_path.replace(self._path)
        except Exception:
            logger.exception("Failed to persist configuration to %s", self._path)

    def save(self, cfg: Optional[AppConfig] = None) -> None:
        if cfg is not None:
            self._config = cfg
        self._write(self._config)
        logger.info("Configuration saved.")
        for listener in list(self._listeners):
            try:
                listener(self._config)
            except Exception:
                logger.exception("Config change listener raised an exception.")

    def update(self, **kwargs: Any) -> None:
        """Convenience helper: mutate a few fields and persist immediately."""
        current = self._config.to_dict()
        current.update(kwargs)
        self.save(AppConfig.from_dict(current))

    def update_quiet(self, **kwargs: Any) -> None:
        """Like :meth:`update`, but does not notify on_change listeners.
        Use for high-frequency, non-hotkey-affecting changes (e.g. window
        geometry from move/resize hotkeys)."""
        current = self._config.to_dict()
        current.update(kwargs)
        self._config = AppConfig.from_dict(current)
        self._write(self._config)

    def on_change(self, callback) -> None:
        self._listeners.append(callback)
