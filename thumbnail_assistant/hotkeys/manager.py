"""High-level hotkey manager: binds configured hotkey strings to named
application actions.

Every action name is defined in ``constants.DEFAULT_HOTKEYS`` and wired to
a callback by ``Application._wire_hotkey_actions``. ``HotkeyManager`` just
needs a name -> callback mapping (via :meth:`set_action`) and a name ->
"ctrl+alt+m"-style spec mapping (from configuration); it doesn't care what
an action actually does.

Note that callbacks registered here are invoked on a throwaway thread
spawned by ``win32_hotkey.py``, never on the Qt main thread - see
``Application._wire_hotkey_actions``, which wraps each one so it lands back
on the main thread before touching any widget.
"""
from __future__ import annotations

import logging
from typing import Callable, Dict, Optional

from ..config import ConfigManager
from .win32_hotkey import Win32HotkeyListener

logger = logging.getLogger(__name__)


class HotkeyManager:
    def __init__(self, config_manager: ConfigManager) -> None:
        self._config_manager = config_manager
        self._listener = Win32HotkeyListener()
        self._actions: Dict[str, Callable[[], None]] = {}
        self._capture_on_key: Optional[Callable[[int, int, str], None]] = None
        self._capture_on_mode_changed: Optional[Callable[[bool], None]] = None

    def start(self) -> None:
        self._listener.start()
        if self._config_manager.config.hotkeys_enabled:
            self._register_all()
        self._config_manager.on_change(self._on_config_changed)

    def stop(self) -> None:
        self._listener.unregister_all()
        self._listener.stop()

    def set_action(self, name: str, callback: Callable[[], None]) -> None:
        """Register the Python-side callback for a named action, e.g.
        ``set_action("send_message", app_controller.send_message)``. Call
        this for every action *before* calling :meth:`start`."""
        self._actions[name] = callback

    def set_capture_mode_handlers(
        self,
        on_key: Callable[[int, int, str], None],
        on_mode_changed: Optional[Callable[[bool], None]] = None,
    ) -> None:
        """Register the callbacks "background capture mode" (see
        win32_hotkey.py) calls while active. ``on_key(vk, modifiers, text)``
        fires per keystroke; ``on_mode_changed(active)`` fires when the mode
        is toggled, so the UI can show that keys are being captured. There
        is no buffering or auto-send here or in win32_hotkey.py - whatever
        ``on_key`` does is the only place the typed text goes. Call this
        before :meth:`start`, same as :meth:`set_action`."""
        self._capture_on_key = on_key
        self._capture_on_mode_changed = on_mode_changed

    def reload(self) -> None:
        """Re-read config and re-register all hotkeys (used after settings
        are changed)."""
        self._listener.unregister_all()
        if self._config_manager.config.hotkeys_enabled:
            self._register_all()

    def _register_all(self) -> None:
        for name, spec in self._config_manager.config.hotkeys.items():
            if not spec:
                continue
            if name == "capture_mode_toggle":
                # Special-cased: this isn't a normal fire-once action, it
                # flips a mode (see Win32HotkeyListener.set_capture_mode_toggle).
                # Still just an ordinary "name -> spec" entry in
                # config.hotkeys/the settings window, same as everything else.
                if self._capture_on_key is None:
                    logger.warning(
                        "No capture-mode handlers registered for 'capture_mode_toggle'; skipping."
                    )
                    continue
                self._listener.set_capture_mode_toggle(
                    spec, self._capture_on_key, self._capture_on_mode_changed
                )
                continue

            callback = self._actions.get(name)
            if callback is None:
                logger.warning("No action registered for hotkey '%s'; skipping.", name)
                continue
            self._listener.register(name, spec, callback)

    def _on_config_changed(self, _cfg) -> None:
        self.reload()