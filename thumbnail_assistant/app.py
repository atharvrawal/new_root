"""Top-level Application orchestrator.

Wires together configuration, logging, the response window (via
AppController), global hotkeys, and the settings window. Owns the
process lifecycle: closing the main window terminates the entire
application (no tray, no background residency), per the product
requirement - unchanged from before.

Post-rewrite there is no adapter concept: every action is a core
framework action (see constants.py), and AppController talks to Gemini
directly instead of hosting a per-site webview.
"""
from __future__ import annotations

import logging
import sys

from PySide6.QtWidgets import QApplication

from . import constants
from .config import ConfigManager
from .hotkeys.manager import HotkeyManager
from .logging_setup import configure_logging, install_global_exception_hook
from .settings.settings_window import SettingsWindow
from .ui.app_controller import AppController

logger = logging.getLogger(__name__)


class Application:
    def __init__(self) -> None:
        configure_logging(console=not _is_frozen_windowed())
        install_global_exception_hook()

        logger.info("=" * 60)
        logger.info("Starting %s v%s", constants.APP_DISPLAY_NAME, _version())

        self.qt_app = QApplication.instance() or QApplication(sys.argv)

        self.config_manager = ConfigManager()
        self._ensure_default_hotkeys()

        self.app_controller = AppController(self.config_manager)
        self.hotkey_manager = HotkeyManager(self.config_manager)
        self.settings_window = SettingsWindow(
            self.config_manager,
            action_labels=dict(constants.CORE_ACTION_LABELS),
            on_saved=self.hotkey_manager.reload,
            window_title=f"{constants.APP_DISPLAY_NAME} - Settings",
            default_capture_prompt=constants.DEFAULT_CAPTURE_PROMPT,
        )

    def _ensure_default_hotkeys(self) -> None:
        """Seed any missing hotkey entries with their defaults and persist
        once if anything was added. Never overwrites a hotkey the user
        has already configured (including one they've intentionally
        cleared to "")."""
        cfg = self.config_manager.config
        changed = False
        for name, spec in constants.DEFAULT_HOTKEYS.items():
            if name not in cfg.hotkeys:
                cfg.hotkeys[name] = spec
                changed = True
        if changed:
            logger.info("Seeded default hotkeys.")
            self.config_manager.save(cfg)

    def _wire_hotkey_actions(self) -> None:
        ac = self.app_controller

        def on_main(name: str, callback) -> None:
            """Register a hotkey action, marshaled onto the Qt main
            thread. Hotkey callbacks arrive on a throwaway thread from
            win32_hotkey.py; every action below eventually touches a
            QWidget (even the move/resize ones, via winId()), which is
            only legal on the main thread."""
            self.hotkey_manager.set_action(name, lambda: ac.dispatch(callback))

        on_main("toggle_visibility", ac.toggle_visibility)
        on_main(
            "capture_screenshot_attach",
            lambda: ac.capture_and_attach(self.config_manager.config.monitor_index),
        )
        on_main(
            "insert_configured_prompt",
            lambda: ac.insert_text(
                self.config_manager.config.capture_prompt or constants.DEFAULT_CAPTURE_PROMPT
            ),
        )
        on_main("send_message", ac.send_message)
        on_main("clear_queue", ac.clear_queue)
        on_main("open_settings", self.settings_window.open)

        # Not marshaled: voice capture blocks on device enumeration and
        # stream setup, and touches no QWidget of its own (it dispatches
        # its own status updates) - see AppController.toggle_voice_capture.
        self.hotkey_manager.set_action("toggle_voice_capture", ac.toggle_voice_capture)

        # Background capture mode: every keystroke while active is typed
        # live into the queued prompt (AppController.on_capture_char) - no
        # buffering, no auto-send when toggled off.
        self.hotkey_manager.set_capture_mode_handlers(
            on_char=lambda ch: ac.dispatch(lambda: ac.on_capture_char(ch)),
            on_backspace=lambda: ac.dispatch(ac.on_capture_backspace),
        )

        on_main("move_up", ac.move_up)
        on_main("move_down", ac.move_down)
        on_main("move_left", ac.move_left)
        on_main("move_right", ac.move_right)
        on_main("increase_width", ac.increase_width)
        on_main("decrease_width", ac.decrease_width)
        on_main("increase_height", ac.increase_height)
        on_main("decrease_height", ac.decrease_height)
        on_main("focus_window", ac.focus_window)
        on_main("increase_opacity", ac.increase_opacity)
        on_main("decrease_opacity", ac.decrease_opacity)

    def _on_geometry_changed(self, x: int, y: int, width: int, height: int) -> None:
        cfg = self.config_manager.config
        updates = {"window_width": width, "window_height": height}
        if cfg.remember_window_position:
            updates["window_x"] = x
            updates["window_y"] = y
        self.config_manager.update_quiet(**updates)

    def _on_opacity_changed(self, alpha: int) -> None:
        self.config_manager.update_quiet(window_opacity=alpha)

    def _on_main_window_closing(self) -> None:
        logger.info("Shutting down hotkey listener and exiting process.")
        try:
            self.hotkey_manager.stop()
        except Exception:
            logger.exception("Error stopping hotkey manager during shutdown.")
        self.qt_app.quit()

    def run(self) -> int:
        cfg = self.config_manager.config

        try:
            self._wire_hotkey_actions()
            self.app_controller._opacity = cfg.window_opacity
            self.app_controller.create_window(
                width=cfg.window_width,
                height=cfg.window_height,
                x=cfg.window_x if cfg.remember_window_position else None,
                y=cfg.window_y if cfg.remember_window_position else None,
            )
            self.app_controller.set_closing_callback(self._on_main_window_closing)
            self.app_controller.set_geometry_changed_callback(self._on_geometry_changed)
            self.app_controller.set_opacity_changed_callback(self._on_opacity_changed)
            self.app_controller.set_mic_device_provider(
                lambda: self.config_manager.config.mic_device_index
            )
            self.app_controller.set_voice_prompt_provider(
                lambda: self.config_manager.config.voice_prompt_template
            )

            self.hotkey_manager.start()

            logger.info("Launching response window (start_minimized=%s)", cfg.start_minimized)
            self.app_controller.start(start_minimized=cfg.start_minimized)

            # Blocking call - runs until the window is closed (see
            # _on_main_window_closing, wired via ResponseWindow.closeEvent).
            return self.qt_app.exec()

        except Exception:
            logger.exception("Fatal error while running the application.")
            return 1
        finally:
            try:
                self.hotkey_manager.stop()
            except Exception:
                pass
            logger.info("%s exited cleanly.", constants.APP_DISPLAY_NAME)


def _is_frozen_windowed() -> bool:
    return getattr(sys, "frozen", False) and not sys.stdout


def _version() -> str:
    from . import __version__
    return __version__


def main() -> int:
    app = Application()
    return app.run()
