"""In-memory image + prompt queue.

Holds the state shared between the decoupled actions (capture,
insert-prompt, clear, send). Nothing here touches disk or the network -
it's just a small piece of shared state that
``ui/app_controller.py`` owns and mutates from hotkey callbacks, then reads
(and clears) when ``send_message`` fires.

Thread-safety note: hotkey callbacks arrive on the win32_hotkey.py
listener thread, not the Qt main thread. All mutation here is guarded by
a single lock so concurrent capture/insert/send calls can't interleave
and corrupt the list/string.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class QueuedBatch:
    """A snapshot of what was queued at the moment of send - what actually
    gets bundled into one Gemini API call."""
    images_base64: List[str] = field(default_factory=list)
    prompt_text: str = ""


class ImageQueue:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._images: List[str] = []
        self._prompt_parts: List[str] = []

    def add_image(self, png_base64: str) -> int:
        """Queue a screenshot (base64 PNG). Returns the new queue length."""
        with self._lock:
            self._images.append(png_base64)
            return len(self._images)

    def append_prompt_text(self, text: str) -> None:
        """Append text to the queued prompt - used by both
        insert_configured_prompt (whole string at once) and the
        Capslock+T live-typing handlers (one character at a time)."""
        if not text:
            return
        with self._lock:
            self._prompt_parts.append(text)

    def backspace_prompt(self) -> None:
        """Remove the last character of the queued prompt, so Backspace
        works while live-typing in background capture mode."""
        with self._lock:
            if not self._prompt_parts:
                return
            joined = "".join(self._prompt_parts)
            if not joined:
                return
            self._prompt_parts = [joined[:-1]]

    def peek_prompt(self) -> str:
        with self._lock:
            return "".join(self._prompt_parts)

    def image_count(self) -> int:
        with self._lock:
            return len(self._images)

    def is_empty(self) -> bool:
        with self._lock:
            return not self._images and not "".join(self._prompt_parts).strip()

    def pop_all(self) -> Optional[QueuedBatch]:
        """Atomically snapshot and clear the queue (auto-clear-on-send).
        Returns None if there's nothing to send (no images and no prompt
        text) so callers can skip firing an empty API call."""
        with self._lock:
            if not self._images and not "".join(self._prompt_parts).strip():
                return None
            batch = QueuedBatch(
                images_base64=self._images,
                prompt_text="".join(self._prompt_parts),
            )
            self._images = []
            self._prompt_parts = []
            return batch

    def clear(self) -> None:
        """Explicit manual clear, distinct from the auto-clear pop_all()
        does on send - backs the clear_queue hotkey."""
        with self._lock:
            self._images = []
            self._prompt_parts = []
