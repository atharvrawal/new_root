"""Orchestrator that owns the image queue, the Gemini client, and the
response window.

Every public method here is a hotkey action, wired up in ``app.py``'s
``_wire_hotkey_actions``. Those arrive on a throwaway thread from the
keyboard hook, so they are marshaled onto the Qt main thread first - see
:meth:`AppController.dispatch`. Anything added here that touches
``self._window`` must go through that same door.
"""
from __future__ import annotations

import base64
import logging
import struct
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, Qt, QThread, Signal

from .. import capture, constants, voice
from ..config import ConfigManager
from ..gemini import client as gemini_client
from ..queue_store import ImageQueue
from .response_window import ResponseWindow

logger = logging.getLogger(__name__)

# Every captured screenshot is written here as a plain PNG, so you can
# open File Explorer and look at exactly the bytes that got sent to
# Gemini - much faster to diagnose a bad capture (wrong monitor, blank/
# black due to a capture-protection interaction, wrong crop, etc.) than
# guessing from Gemini's text response alone. Not cleaned up
# automatically; it's a handful of small PNGs, safe to delete anytime.
_DEBUG_SCREENSHOT_DIR = constants.LOCAL_DATA_DIR / "debug_screenshots"


def _png_dimensions(png_bytes: bytes) -> Optional[tuple[int, int]]:
    """Parse width/height straight out of the PNG header (IHDR is always
    the first chunk mss.tools.to_png writes) - no Pillow dependency
    needed just to sanity-check a capture."""
    try:
        if png_bytes[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        width, height = struct.unpack(">II", png_bytes[16:24])
        return width, height
    except Exception:
        return None


class _SendWorker(QObject):
    """Runs one blocking Gemini API call off the Qt main thread, then
    emits the result back onto it. One-shot: create, start, discard."""
    finished = Signal(str, bool)  # (text_or_error, is_error)

    def __init__(self, images_base64, prompt_text, api_key, model) -> None:
        super().__init__()
        self._images_base64 = images_base64
        self._prompt_text = prompt_text
        self._api_key = api_key
        self._model = model

    def run(self) -> None:
        response = gemini_client.send(
            self._images_base64, self._prompt_text, self._api_key, self._model
        )
        if response.ok:
            self.finished.emit(response.text, False)
        else:
            self.finished.emit(response.error or "Unknown error.", True)


class AppController(QObject):
    # Every hotkey callback arrives on a throwaway thread spawned by
    # win32_hotkey.py, never on the Qt main thread. Touching a QWidget
    # (or calling winId(), or constructing a QThread) from there is
    # undefined behavior - it works until it doesn't, then crashes or
    # hangs with no traceback. This signal is the single door every
    # hotkey action goes through: emit from any thread, run on the main
    # one. See dispatch() and app.py's _wire_hotkey_actions.
    _run_on_main = Signal(object)

    def __init__(self, config_manager: ConfigManager) -> None:
        super().__init__()
        self._run_on_main.connect(self._invoke_on_main, Qt.ConnectionType.QueuedConnection)
        self._config_manager = config_manager
        self._queue = ImageQueue()
        self._window = ResponseWindow()
        # Enter (or the send button) in the composer runs exactly the same
        # path as the send hotkey - one send implementation, two triggers.
        self._window.composer.submitted.connect(self.send_message)
        self._voice_recorder = voice.VoiceRecorder()
        self._mic_device_provider: Optional[Callable[[], Optional[int]]] = None
        self._voice_prompt_provider: Optional[Callable[[], Optional[str]]] = None
        self._voice_lock = threading.Lock()

        # Keep worker/thread refs alive until they finish (Qt won't do
        # this for us if they go out of scope mid-flight) - see
        # _dispatch_send for why both must be kept, not just the thread.
        # Keyed by worker id() so _on_send_finished (identified via
        # self.sender(), the QObject that actually emitted the signal)
        # can look up its matching thread without relying on a lambda's
        # captured closure - see _dispatch_send for why that matters.
        self._active: dict[int, tuple[QThread, "_SendWorker"]] = {}

    # -- main-thread marshaling ------------------------------------------
    def dispatch(self, fn: Callable[[], None]) -> None:
        """Queue ``fn`` to run on the Qt main thread. Safe to call from
        any thread; returns immediately without waiting for it."""
        self._run_on_main.emit(fn)

    def _invoke_on_main(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:
            logger.exception("Hotkey action raised on the main thread.")

    # -- window lifecycle, mirrors old BrowserManager.start()/create_window
    def create_window(self, width: int, height: int, x: Optional[int], y: Optional[int]) -> None:
        self._window.resize(width, height)
        if x is not None and y is not None:
            self._window.move(x, y)

    def start(self, start_minimized: bool = False) -> None:
        """Blocking call in the old API (webview.start()); here Qt's own
        event loop (app.exec()) is what actually blocks, so this just
        shows the window - app.py's run() is expected to call
        QApplication.exec() itself after this returns. apply_chrome()
        does the actual show() internally (see response_window.py for
        why show-then-style-mutate ordering matters)."""
        self._window.apply_chrome()
        if start_minimized:
            self._window.toggle_visibility()

    def set_closing_callback(self, callback: Callable[[], None]) -> None:
        self._window.set_closing_callback(callback)

    def set_geometry_changed_callback(self, callback: Callable[[int, int, int, int], None]) -> None:
        self._window.set_geometry_changed_callback(callback)

    def set_opacity_changed_callback(self, callback: Callable[[int], None]) -> None:
        self._window.set_opacity_changed_callback(callback)

    def set_mic_device_provider(self, provider: Callable[[], Optional[int]]) -> None:
        self._mic_device_provider = provider

    def set_voice_prompt_provider(self, provider: Callable[[], Optional[str]]) -> None:
        self._voice_prompt_provider = provider

    @property
    def _opacity(self):
        return self._window._opacity

    @_opacity.setter
    def _opacity(self, value):
        self._window._opacity = value

    # -- window move/resize/opacity/visibility, delegate straight through
    def toggle_visibility(self) -> None:
        self._window.toggle_visibility()

    def focus_window(self) -> None:
        self._window.focus_window()

    def move_up(self) -> None:
        self._window.move_up()

    def move_down(self) -> None:
        self._window.move_down()

    def move_left(self) -> None:
        self._window.move_left()

    def move_right(self) -> None:
        self._window.move_right()

    def increase_width(self) -> None:
        self._window.increase_width()

    def decrease_width(self) -> None:
        self._window.decrease_width()

    def increase_height(self) -> None:
        self._window.increase_height()

    def decrease_height(self) -> None:
        self._window.decrease_height()

    def increase_opacity(self) -> None:
        self._window.increase_opacity()

    def decrease_opacity(self) -> None:
        self._window.decrease_opacity()

    # -- decoupled capture/insert/send primitives -----------------------
    def capture_and_attach(self, monitor_index: Optional[int] = None) -> None:
        """Screenshot -> queue only, no send. Pairs with
        insert_configured_prompt/send_message, same independence as
        before - just backed by ImageQueue instead of a DOM upload."""
        logger.info("capture_and_attach: taking screenshot (monitor_index=%s)...", monitor_index)
        image_b64 = capture.capture_screen_png_base64(monitor_index)
        if image_b64 is None:
            logger.warning("capture_and_attach: screenshot failed; aborting.")
            self._window.set_queue_status("Last screenshot FAILED - check logs.")
            return

        raw_png = base64.b64decode(image_b64)
        dims = _png_dimensions(raw_png)
        debug_path = self._save_debug_screenshot(raw_png)
        logger.info(
            "capture_and_attach: got %d bytes, dimensions=%s, saved to %s",
            len(raw_png),
            dims,
            debug_path,
        )

        count = self._queue.add_image(image_b64)
        logger.info("capture_and_attach: queued screenshot (%d image(s) now queued).", count)
        self._refresh_queue_status()

    def _save_debug_screenshot(self, raw_png: bytes) -> Optional[Path]:
        """Write the just-captured PNG to disk unmodified, so it can be
        opened and visually inspected - the fastest way to tell "bad
        capture" from "bad prompt" from "model being fussy" apart."""
        try:
            _DEBUG_SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
            path = _DEBUG_SCREENSHOT_DIR / f"capture_{time.strftime('%Y%m%d_%H%M%S')}.png"
            path.write_bytes(raw_png)
            return path
        except Exception:
            logger.exception("Failed to save debug screenshot (non-fatal).")
            return None

    def insert_text(self, text: str) -> None:
        """Used both for insert_configured_prompt's whole-string insert
        and (via on_capture_char) capture mode's per-character live
        typing. Both land in the composer, which is where the queued
        prompt now lives."""
        if not text:
            return
        logger.info("insert_text: appending %d char(s) to the composer.", len(text))
        self._window.composer.append(text)

    def backspace(self) -> None:
        self._window.composer.backspace()

    def clear_queue(self) -> None:
        """Discard queued screenshots and prompt text without sending.
        Without this the only way out of a queue you no longer want is to
        send it, since pop_all() is what clears it."""
        logger.info(
            "clear_queue: discarding %d image(s) and %d prompt char(s).",
            self._queue.image_count(),
            len(self._window.composer.text()),
        )
        self._queue.clear()
        self._window.composer.clear()
        self._refresh_queue_status()
        self._window.set_queue_status("Queue cleared.")

    def send_message(self) -> None:
        """Bundle whatever's queued (auto-clears the queue) and fire the
        Gemini call off the main thread."""
        prompt = self._window.composer.text()
        logger.info(
            "send_message: fired. Queue has %d image(s), prompt is %d char(s).",
            self._queue.image_count(),
            len(prompt),
        )
        if not self._queue.image_count() and not prompt.strip():
            logger.warning("send_message: nothing queued; ignoring.")
            self._window.set_queue_status("Nothing to send - type a prompt or capture a screenshot first.")
            return
        images = self._queue.pop_all()
        cfg = self._config_manager.config
        if not cfg.gemini_api_key:
            logger.warning("send_message: no Gemini API key configured.")
            self._window.append_response(
                "No Gemini API key configured. Open Settings (Ctrl+Alt+S) and add one.",
                is_error=True,
            )
            self._window.set_queue_status("Send failed - no API key configured.")
            return
        # Echo what was sent, then clear the composer - same as the web
        # chat UIs, and it's the only record of the prompt once it's gone.
        self._window.append_prompt_echo(prompt, image_count=len(images))
        self._window.composer.clear()
        self._refresh_queue_status()
        self._window.set_queue_status(
            f"Sending {len(images)} image(s) + prompt to Gemini..."
        )
        self._dispatch_send(images, prompt, cfg.gemini_api_key, cfg.gemini_model)

    def _refresh_queue_status(self) -> None:
        """The composer shows the prompt itself now, so the status line
        only has to report the screenshot count."""
        count = self._queue.image_count()
        self._window.composer.set_image_count(count)
        self._window.set_queue_status(
            f"{count} screenshot(s) attached." if count else "Ready."
        )

    def _dispatch_send(self, images_base64, prompt_text, api_key, model) -> None:
        logger.info(
            "_dispatch_send: starting worker thread (%d image(s), prompt length %d).",
            len(images_base64),
            len(prompt_text),
        )
        thread = QThread()
        worker = _SendWorker(images_base64, prompt_text, api_key, model)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        # Connect DIRECTLY to the bound method, not a lambda. This
        # matters: Qt determines a connection's thread affinity from the
        # RECEIVER object of the connected callable. self is an
        # AppController, which is now a QObject living on the main
        # thread (see __init__), so Qt's AutoConnection correctly
        # resolves this to a queued connection - the slot runs on the
        # main thread's event loop, which is required since it touches
        # QWidgets (append_response). A bare lambda has no QObject
        # affiliation, so Qt can't infer this and the slot would run
        # directly on the emitting (worker) thread instead - a real
        # cross-thread GUI bug that doesn't crash on every machine, but
        # will eventually.
        worker.finished.connect(self._on_send_finished)
        # Keep BOTH the thread and worker alive by holding real references
        # until they finish - Qt/PySide does not guarantee a QObject moved
        # to a QThread survives on Python's own refcounting once this
        # method returns, and a worker getting garbage-collected before
        # the thread's started signal fires means run() silently never
        # executes (no error, no response, nothing - exactly the "capslock
        # sequence does nothing" symptom).
        self._active[id(worker)] = (thread, worker)
        thread.start()

    def _on_send_finished(self, text: str, is_error: bool) -> None:
        worker = self.sender()
        logger.info("_on_send_finished: received Gemini result (is_error=%s).", is_error)
        self._window.append_response(text, is_error=is_error)
        self._window.set_queue_status("Queue empty.")
        pair = self._active.pop(id(worker), None)
        if pair is None:
            logger.warning("_on_send_finished: could not find matching thread for worker; skipping cleanup.")
            return
        thread, _worker = pair
        thread.quit()
        thread.wait()

    # -- capture-mode handlers, wired by hotkeys.manager.set_capture_mode_handlers.
    # Live-typed keystrokes land in the same queued prompt that
    # insert_configured_prompt writes to, so they reuse the same two
    # methods (and get the same status-line refresh).
    def on_capture_char(self, char: str) -> None:
        self.insert_text(char)

    def on_capture_backspace(self) -> None:
        self.backspace()

    # -- voice capture, same start/stop/transcribe/send shape as before -
    # difference: instead of sending into a DOM compose box via
    # send_text(), it sends the transcript straight to Gemini as its own
    # one-shot request (bypassing the image queue, matching the old
    # behavior of not touching whatever else was mid-compose).
    # Deliberately NOT marshaled onto the Qt main thread (unlike every
    # other hotkey action): device enumeration and stream setup take
    # long enough to visibly stall the UI. It touches no QWidget - only
    # the status-line updates below are dispatched.
    def toggle_voice_capture(self) -> None:
        with self._voice_lock:
            if self._voice_recorder.is_recording:
                threading.Thread(
                    target=self._finish_voice_capture,
                    name="VoiceCaptureFinishThread",
                    daemon=True,
                ).start()
                return

            device_index = None
            if self._mic_device_provider is not None:
                try:
                    device_index = self._mic_device_provider()
                except Exception:
                    logger.exception("mic_device_provider raised; using default input device.")

            desktop_device_index = voice.get_stereo_mix_input_device()
            if self._voice_recorder.start(device_index, desktop_device_index):
                self._set_status("Recording... press the voice hotkey again to stop and send.")
            else:
                logger.warning("toggle_voice_capture: failed to start recording.")
                self._set_status("Voice capture FAILED to start - check logs.")

    def _finish_voice_capture(self) -> None:
        self._set_status("Transcribing...")
        transcript = self._voice_recorder.stop()
        if not transcript:
            logger.warning("toggle_voice_capture: no transcribable audio; nothing sent.")
            self._set_status("No transcribable audio captured - nothing sent.")
            return
        template = None
        if self._voice_prompt_provider is not None:
            try:
                template = self._voice_prompt_provider()
            except Exception:
                logger.exception("voice_prompt_provider raised; using default template.")
        message = voice.build_stt_message(transcript, template)
        cfg = self._config_manager.config
        if not cfg.gemini_api_key:
            logger.warning("_finish_voice_capture: no Gemini API key configured.")
            self.dispatch(
                lambda: self._window.append_response(
                    "No Gemini API key configured. Open Settings and add one.", is_error=True
                )
            )
            return
        self._set_status("Sending transcript to Gemini...")
        # _dispatch_send builds a QThread, so it must run on the main
        # thread - this method runs on VoiceCaptureFinishThread.
        self.dispatch(
            lambda: self._dispatch_send([], message, cfg.gemini_api_key, cfg.gemini_model)
        )

    def _set_status(self, text: str) -> None:
        """Update the status line from any thread."""
        self.dispatch(lambda: self._window.set_queue_status(text))
