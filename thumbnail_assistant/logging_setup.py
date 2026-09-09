"""Application-wide logging configuration.

Uses a rotating file handler so log files never grow unbounded, plus an
optional console handler for interactive/dev usage. All modules should
obtain loggers via ``logging.getLogger(__name__)`` after ``configure_logging``
has been called once, at process start.
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from . import constants

_CONFIGURED = False


def configure_logging(console: bool = True, level: int = logging.INFO) -> None:
    """Idempotently configure the root logger for the application.

    Parameters
    ----------
    console:
        If True, also emit logs to stdout. Disable for a windowed
        (no-console) PyInstaller build where stdout may not exist.
    level:
        Root logging level.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    constants.LOG_DIR.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        constants.LOG_FILE,
        maxBytes=2 * 1024 * 1024,  # 2 MB per file
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    if console:
        try:
            stream_handler = logging.StreamHandler(sys.stdout)
            stream_handler.setFormatter(fmt)
            root.addHandler(stream_handler)
        except Exception:
            # No console available (e.g. windowed exe) - safe to ignore.
            pass

    # Quiet down noisy third-party loggers. google-genai logs every HTTP
    # request at INFO through httpx, and faster-whisper logs per-segment
    # detail - both would bury this app's own lines in app.log.
    for noisy in ("httpx", "httpcore", "google_genai", "faster_whisper", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def install_global_exception_hook() -> None:
    """Route uncaught exceptions on the main thread into the log file
    instead of letting them silently vanish in a windowed executable."""
    logger = logging.getLogger("uncaught")

    def handle(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logger.critical("Unhandled exception", exc_info=(exc_type, exc_value, exc_tb))

    sys.excepthook = handle
