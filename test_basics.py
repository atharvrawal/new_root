"""Self-check for the pure logic in this app: hotkey parsing, the queue,
config round-tripping, PNG header parsing, and STT message formatting.

Deliberately no framework and no fixtures - run it directly:

    python test_basics.py

Everything here is GUI-free, thread-free and network-free, so it runs in
under a second. The Qt/Win32/Gemini paths are not covered; those need the
real app running (see README.md's "Verifying it works" section).
"""
import struct
import sys

from thumbnail_assistant import constants, voice
from thumbnail_assistant.config import AppConfig
from thumbnail_assistant.hotkeys.win32_hotkey import (
    _vk_to_text,
    MOD_ALT,
    MOD_CAPSLOCK,
    MOD_CONTROL,
    MOD_NOREPEAT,
    MOD_SHIFT,
    HotkeyParseError,
    parse_hotkey,
)
from thumbnail_assistant.queue_store import ImageQueue
from thumbnail_assistant.ui.app_controller import _png_dimensions


def test_hotkey_parsing():
    mods, vk = parse_hotkey("ctrl+alt+g")
    assert mods & MOD_CONTROL and mods & MOD_ALT
    assert vk == 0x47  # VK_G

    # Capslock is a first-class modifier here, not just another key.
    mods, vk = parse_hotkey("capslock+shift+up")
    assert mods & MOD_CAPSLOCK and mods & MOD_SHIFT
    assert vk == 0x26  # VK_UP

    # Standalone capslock: no modifiers beyond the compatibility bit.
    mods, vk = parse_hotkey("capslock")
    assert mods & ~MOD_NOREPEAT == 0
    assert vk == 0x14

    # Case and stray whitespace must not matter.
    assert parse_hotkey(" CTRL + ALT + S ") == parse_hotkey("ctrl+alt+s")

    for bad in ("", "ctrl+", "meta+q", "ctrl+alt+notakey"):
        try:
            parse_hotkey(bad)
        except HotkeyParseError:
            pass
        else:
            raise AssertionError(f"expected {bad!r} to be rejected")


def test_every_default_hotkey_parses():
    """A default that doesn't parse would silently fail to register at
    startup, logging a warning nobody reads."""
    for name, spec in constants.DEFAULT_HOTKEYS.items():
        parse_hotkey(spec)  # raises if malformed
        assert name in constants.CORE_ACTION_LABELS, f"{name} has no settings-window label"
    assert set(constants.CORE_ACTION_LABELS) == set(constants.DEFAULT_HOTKEYS)


def test_capture_mode_translation():
    """Background capture mode turns raw virtual keys back into text via
    the user's real keyboard layout (ToUnicodeEx). Windows-only."""
    if not constants.IS_WINDOWS:
        return
    VK_A, VK_1, VK_BACK, VK_LEFT, VK_SHIFT_KEY = 0x41, 0x31, 0x08, 0x25, 0x10
    scan = 0  # ToUnicodeEx resolves the scan code itself when given 0

    assert _vk_to_text(VK_A, scan, 0) == "a"
    assert _vk_to_text(VK_A, scan, MOD_SHIFT) == "A"
    assert _vk_to_text(VK_1, scan, 0) == "1"

    # Ctrl+A must NOT produce text: ToUnicodeEx maps it to the control
    # character , which would otherwise be inserted into the prompt.
    # It is passed on as a key code so it can act as select-all instead.
    assert _vk_to_text(VK_A, scan, MOD_CONTROL) == ""

    # Editing keys and modifiers type nothing.
    assert _vk_to_text(VK_LEFT, scan, 0) == ""
    assert _vk_to_text(VK_SHIFT_KEY, scan, 0) == ""
    assert _vk_to_text(VK_BACK, scan, 0) in ("", "")


def test_queue():
    """The queue holds screenshots only - prompt text lives in the
    composer widget now (ui/composer.py)."""
    q = ImageQueue()
    assert q.image_count() == 0
    assert q.pop_all() == []

    q.add_image("aaa")
    assert q.add_image("bbb") == 2
    assert q.image_count() == 2

    assert q.pop_all() == ["aaa", "bbb"]
    # pop_all auto-clears, so an immediate second send has nothing to send.
    assert q.image_count() == 0
    assert q.pop_all() == []

    q.add_image("ccc")
    q.clear()
    assert q.image_count() == 0


def test_config_roundtrip():
    cfg = AppConfig(
        window_width=1234,
        gemini_api_key="secret",
        monitor_index=0,
        hotkeys={"toggle_visibility": "capslock"},
    )
    back = AppConfig.from_dict(cfg.to_dict())
    assert back.window_width == 1234
    assert back.gemini_api_key == "secret"
    # monitor_index 0 ("all monitors") must survive - it's falsy, so a
    # truthiness check here would silently turn it back into None.
    assert back.monitor_index == 0
    # An explicitly-cleared hotkey stays cleared; unset ones get defaults.
    assert back.hotkeys["toggle_visibility"] == "capslock"
    assert back.hotkeys["send_message"] == constants.DEFAULT_HOTKEYS["send_message"]

    # Junk on disk falls back to defaults rather than raising.
    defaults = AppConfig()
    empty = AppConfig.from_dict({})
    assert empty.window_width == defaults.window_width
    assert empty.gemini_model == defaults.gemini_model


def test_png_dimensions():
    header = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + struct.pack(">II", 1920, 1080)
    assert _png_dimensions(header) == (1920, 1080)
    assert _png_dimensions(b"not a png") is None
    assert _png_dimensions(b"") is None


def test_stt_message():
    assert "hi there" in voice.build_stt_message("hi there")
    assert voice.build_stt_message("x", "Q: {transcript}") == "Q: x"
    # A custom template missing the placeholder must append rather than
    # silently drop the transcript.
    assert "x" in voice.build_stt_message("x", "no placeholder here")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    sys.exit(main())
