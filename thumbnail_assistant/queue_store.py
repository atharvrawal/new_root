"""In-memory screenshot queue.

Holds the screenshots captured between sends. Nothing here touches disk or
the network - it's a small piece of shared state that
``ui/app_controller.py`` owns and mutates from hotkey callbacks, then reads
(and clears) when ``send_message`` fires.

Prompt text used to live here too, invisibly, with the status bar showing a
truncated preview. It now lives in the composer widget
(``ui/composer.py``), where it is visible and editable, so this class holds
images only - one copy of each thing, nothing to keep in sync.

Thread-safety note: hotkey callbacks originate on the win32_hotkey.py
listener thread. They are marshaled onto the Qt main thread before reaching
this class (see ``AppController.dispatch``), but the lock is kept anyway -
it is cheap, and it means this class is still correct if something ever
calls it directly from another thread.
"""
from __future__ import annotations

import threading
from typing import List


class ImageQueue:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._images: List[str] = []

    def add_image(self, png_base64: str) -> int:
        """Queue a screenshot (base64 PNG). Returns the new queue length."""
        with self._lock:
            self._images.append(png_base64)
            return len(self._images)

    def image_count(self) -> int:
        with self._lock:
            return len(self._images)

    def pop_all(self) -> List[str]:
        """Atomically snapshot and clear the queue (auto-clear-on-send).
        Returns an empty list if nothing was queued."""
        with self._lock:
            images = self._images
            self._images = []
            return images

    def clear(self) -> None:
        """Explicit manual clear, distinct from the auto-clear pop_all()
        does on send - backs the clear_queue hotkey."""
        with self._lock:
            self._images = []
