"""Low-level global hotkey plumbing using a WH_KEYBOARD_LL keyboard hook.

Why a keyboard hook instead of the ``RegisterHotKey`` API (used previously)?

``RegisterHotKey`` claims a key combination system-wide: when it fires, Windows
delivers ``WM_HOTKEY`` to *this* app and the focused app (browser, game,
whatever) never sees that keystroke at all - there is no way to both react to
it here and have it still reach the focused window. That is a hard OS-level
limitation of the API, not a bug in how it was being used.

This app's hotkeys are frequently the same combination the focused app also
wants (e.g. Ctrl+T in a browser), and the desired behavior is for *both* to
happen: this app reacts, and the focused app still receives the keystroke
normally. A low-level keyboard hook (``WH_KEYBOARD_LL``) can do this, because
the hook callback observes every keystroke system-wide but explicitly chooses
whether to swallow it (return a nonzero value) or let it continue
(``CallNextHookEx``) to the focused application. Every hook callback in this
module always calls ``CallNextHookEx`` - it never blocks a keystroke.

Trade-offs accepted with this change (vs. the previous ``RegisterHotKey``
approach):

* Keyboard hooks are sometimes flagged or blocked by anti-cheat systems in
  games, and can read as keylogging-like behavior to antivirus/EDR software.
  ``RegisterHotKey`` doesn't have either issue.
* Slightly more CPU overhead, since every keystroke system-wide now runs
  through this process's hook callback (kept minimal - see
  ``_low_level_keyboard_proc``).

This module intentionally has no dependency on the rest of the app - it only
knows how to turn a "ctrl+alt+m"-style string into a (modifiers, vk_code)
pair and call a Python callback when that combination is pressed.

Threading requirement
----------------------
``SetWindowsHookExW``/``UnhookWindowsHookEx`` for a ``WH_KEYBOARD_LL`` hook
must be installed and torn down from a thread that runs a Win32 message
loop (``GetMessageW``) for the hook's lifetime - Windows dispatches the hook
callback via that thread's message pump internally, even though no visible
window is involved. A dedicated ``HotkeyListenerThread`` owns the hook and
pumps messages for as long as it's installed; ``stop()`` posts a thread
message to break out of that loop and unhook cleanly.

Because matching a keystroke against registered hotkeys is just a Python
dict lookup (not an OS-level claim like ``RegisterHotKey`` was),
``register()``/``unregister()`` only need to mutate a lock-protected dict -
no thread-affinity handoff is required for those calls; only the hook
install/uninstall itself is thread-affine, and that only happens once each
in ``start()``/``stop()``.

Win32 signature correctness
----------------------------
Every Win32 API used here has explicit ``argtypes``/``restype`` declared
below. This matters on 64-bit Python: handles (``HHOOK``, ``HMODULE``) and
message-loop wparam/lparam values are pointer-sized (64 bits), but ctypes
defaults undeclared return/argument types to a 32-bit ``c_int``. Leaving any
of these undeclared silently truncates 64-bit values. Both ``user32`` and
``kernel32`` are loaded with ``use_last_error=True`` so
``ctypes.get_last_error()`` reliably reflects the most recent failed call
from that DLL, and every failure path below logs the real Win32 error code
and message via ``FormatMessageW`` rather than a generic message.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import logging
import threading
from typing import Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# `use_last_error=True` makes ctypes capture the real GetLastError() value
# immediately after each call through this DLL handle, before anything else
# (including Python itself) has a chance to clobber it.
user32 = ctypes.WinDLL("user32", use_last_error=True) if hasattr(ctypes, "WinDLL") else None
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if hasattr(ctypes, "WinDLL") else None

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_QUIT = 0x0012

WH_KEYBOARD_LL = 13

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12    # Alt
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_CAPITAL = 0x14  # Caps Lock
VK_UP = 0x26
VK_DOWN = 0x28
VK_LEFT = 0x25
VK_RIGHT = 0x27
VK_OEM_PLUS = 0xBB   # "="
VK_OEM_MINUS = 0xBD  # "-"  
VK_OEM_3 = 0xC0  # "`" (grave/tilde key)
VK_S = 0x53
VK_P = 0x50
VK_V = 0x56
VK_RETURN = 0x0D
VK_BACK = 0x08  # Backspace, used to edit live during background capture mode

# Keys in this set are swallowed (never passed to CallNextHookEx) when they
# match a registered hotkey - i.e. only when actually used as a hotkey.
# These are the keys where leaking the keystroke through to the focused app
# is actively harmful (an arrow key moving the game's camera, Enter
# submitting a form) rather than merely redundant. Everything else passes
# through unconditionally, per the module's pass-through philosophy - see
# _handle_keydown, which also swallows any Capslock-modified combo
# regardless of what's in this set.
_SWALLOWABLE_VKS = {
    VK_CAPITAL,
    VK_UP, VK_DOWN, VK_LEFT, VK_RIGHT,
    VK_OEM_PLUS, VK_OEM_MINUS, VK_OEM_3,
    VK_S, VK_RETURN, VK_P, VK_V
}

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_CAPSLOCK = 0x0010
# Kept only because parse_hotkey() (below) still ORs this into its returned
# modifiers for backward compatibility with anything inspecting that value;
# it has no meaning to the keyboard hook (repeat suppression is handled
# ourselves - see _keys_down in Win32HotkeyListener) and is masked back out
# before matching. Historically this was a RegisterHotKey flag bit.
MOD_NOREPEAT = 0x4000

# LRESULT has no built-in ctypes alias; it is a pointer-sized signed
# integer on both x86 and x64, which c_ssize_t models correctly on either.
LRESULT = ctypes.c_ssize_t

# -- HOOKPROC type definition (module level - the layout never changes
# between listener instances; only the bound callback does). ``WINFUNCTYPE``
# only exists on Windows builds of ctypes, so this is guarded the same way
# ``user32``/``kernel32`` are above, to keep this module importable on
# non-Windows platforms (e.g. for linting/CI). -------------------------------
if hasattr(ctypes, "WINFUNCTYPE"):
    HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("vkCode", wintypes.DWORD),
            ("scanCode", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_void_p),
        ]
else:
    HOOKPROC = None
    KBDLLHOOKSTRUCT = None


def _configure_win32_signatures() -> None:
    """Declare argtypes/restype for every Win32 API this module calls.

    This is what makes 64-bit handles, pointers, and LRESULT values survive
    the ctypes boundary intact instead of being silently coerced to/from a
    32-bit ``c_int``.
    """
    if user32 is None or kernel32 is None:
        return  # Non-Windows platform; nothing to configure.

    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE

    kernel32.GetCurrentThreadId.argtypes = []
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    kernel32.FormatMessageW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    kernel32.FormatMessageW.restype = wintypes.DWORD

    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
    user32.SetWindowsHookExW.restype = wintypes.HHOOK

    user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
    user32.UnhookWindowsHookEx.restype = wintypes.BOOL

    user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
    user32.CallNextHookEx.restype = LRESULT

    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short

    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    user32.GetMessageW.restype = ctypes.c_int  # BOOL, but can legitimately be -1 on error

    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.TranslateMessage.restype = wintypes.BOOL

    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = LRESULT

    user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostThreadMessageW.restype = wintypes.BOOL


_configure_win32_signatures()


# -- Win32 error diagnostics ------------------------------------------------
_FORMAT_MESSAGE_FROM_SYSTEM = 0x00001000
_FORMAT_MESSAGE_IGNORE_INSERTS = 0x00000200


def _format_win32_error(code: int) -> str:
    """Render a Win32 error code to its system-provided message text via
    ``FormatMessageW`` - the same API ``GetLastError()`` results are meant
    to be decoded with."""
    if not code:
        return "no error code was set"
    if kernel32 is None:
        return f"error code {code}"

    buf = ctypes.create_unicode_buffer(512)
    chars_written = kernel32.FormatMessageW(
        _FORMAT_MESSAGE_FROM_SYSTEM | _FORMAT_MESSAGE_IGNORE_INSERTS,
        None,
        code,
        0,
        buf,
        len(buf),
        None,
    )
    if chars_written == 0:
        return f"error code {code} (no system message available)"
    return buf.value.strip()


def _describe_last_error() -> str:
    code = ctypes.get_last_error()
    return f"Win32 error {code}: {_format_win32_error(code)}"


def _log_win32_failure(api_name: str, context: str) -> None:
    logger.error("%s failed while %s - %s", api_name, context, _describe_last_error())


_MODIFIER_MAP = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "super": MOD_WIN,
    "capslock": MOD_CAPSLOCK,
    "caps": MOD_CAPSLOCK,
}

# Official Win32 virtual-key codes (winuser.h) for every key we support.
# Letters MUST be keyed in lowercase here because parse_hotkey() lowercases
# every token before lookup - VK_A..VK_Z (0x41-0x5A) are the correct codes
# for A-Z regardless of letter case in the *input string*; only the dict
# key's case matters for the lookup to succeed.
_VK_MAP: Dict[str, int] = {chr(c): c for c in range(0x30, 0x3A)}  # '0'-'9' (no case)
_VK_MAP.update({chr(c).lower(): c for c in range(0x41, 0x5B)})  # 'a'-'z' -> VK_A-VK_Z
_VK_MAP.update({
    # Function keys
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    # Whitespace / editing keys
    "space": 0x20, "tab": 0x09, "backspace": 0x08,
    "enter": 0x0D, "return": 0x0D,
    "esc": 0x1B, "escape": 0x1B,
    "delete": 0x2E, "del": 0x2E,
    "insert": 0x2D, "ins": 0x2D,
    "capslock": 0x14, "caps": 0x14, "caps_lock": 0x14,
    # Navigation / arrow keys
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pgup": 0x21,
    "pagedown": 0x22, "pgdn": 0x22,
    # Punctuation (US layout VK_OEM_* codes)
    "`": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, "\\": 0xDC,
    ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
})


class HotkeyParseError(ValueError):
    pass


# -- capture-mode character translation --------------------------------
# Used only by "background capture mode" (see Win32HotkeyListener's
# _capture_* methods below) to turn raw vk codes back into text, since
# nothing is focused to receive real WM_CHAR translation while it's
# active. US QWERTY layout only - if you're on a different physical
# keyboard layout, characters typed during capture mode will come out
# wrong for whichever keys differ from US QWERTY. Extending this to
# other layouts would need a real Win32 keyboard-layout API
# (ToUnicodeEx), not just a bigger static table.
_VK_CHAR_UNSHIFTED: Dict[int, str] = {}
_VK_CHAR_SHIFTED: Dict[int, str] = {}

for _c in range(0x30, 0x3A):  # '0'-'9'
    _VK_CHAR_UNSHIFTED[_c] = chr(_c)
_VK_CHAR_SHIFTED.update({
    0x30: ")", 0x31: "!", 0x32: "@", 0x33: "#", 0x34: "$",
    0x35: "%", 0x36: "^", 0x37: "&", 0x38: "*", 0x39: "(",
})

for _c in range(0x41, 0x5B):  # 'A'-'Z' vk codes
    _VK_CHAR_UNSHIFTED[_c] = chr(_c + 32)  # lowercase
    _VK_CHAR_SHIFTED[_c] = chr(_c)          # uppercase

_VK_CHAR_UNSHIFTED[0x20] = " "  # space

# vk -> (unshifted, shifted), US layout
_OEM_CHAR_PAIRS = {
    0xC0: ("`", "~"), 0xBD: ("-", "_"), 0xBB: ("=", "+"),
    0xDB: ("[", "{"), 0xDD: ("]", "}"), 0xDC: ("\\", "|"),
    0xBA: (";", ":"), 0xDE: ("'", "\""), 0xBC: (",", "<"),
    0xBE: (".", ">"), 0xBF: ("/", "?"),
}
for _vk, (_un, _sh) in _OEM_CHAR_PAIRS.items():
    _VK_CHAR_UNSHIFTED[_vk] = _un
    _VK_CHAR_SHIFTED[_vk] = _sh


def _vk_to_char(vk: int, shifted: bool) -> Optional[str]:
    table = _VK_CHAR_SHIFTED if shifted else _VK_CHAR_UNSHIFTED
    return table.get(vk)


def parse_hotkey(spec: str) -> tuple[int, int]:
    """Parse a string like ``"ctrl+alt+m"`` into ``(modifiers, vk_code)``.

    Kept byte-for-byte compatible with the previous RegisterHotKey-based
    implementation (including OR'ing in MOD_NOREPEAT) since settings_window.py
    calls this directly for validation; Win32HotkeyListener.register() below
    masks MOD_NOREPEAT back out before using the result, since it has no
    meaning to the keyboard hook.
    """
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    if not parts:
        raise HotkeyParseError(f"Empty hotkey spec: {spec!r}")

    key_part = parts[-1]
    modifier_parts = parts[:-1]

    modifiers = 0
    for mod in modifier_parts:
        if mod not in _MODIFIER_MAP:
            raise HotkeyParseError(f"Unknown modifier {mod!r} in {spec!r}")
        modifiers |= _MODIFIER_MAP[mod]

    if key_part not in _VK_MAP:
        raise HotkeyParseError(f"Unknown/unsupported key {key_part!r} in {spec!r}")

    return modifiers | MOD_NOREPEAT, _VK_MAP[key_part]


class Win32HotkeyListener:
    """Owns a system-wide ``WH_KEYBOARD_LL`` keyboard hook on a dedicated
    background thread. Registers/unregisters named hotkeys (matched against
    every keystroke by this hook) and invokes a callback (on its own,
    per-invocation worker thread - see ``_invoke_callback``) when one fires.

    Every keystroke is always passed through to whatever application has
    focus (``CallNextHookEx`` is called unconditionally) - registering a
    hotkey here never prevents the focused app from also seeing that
    keystroke normally.

    Callbacks are dispatched on their own short-lived thread, not the hook
    thread itself, so a slow callback can never stall the hook (Windows
    silently removes low-level hooks that block for too long) or delay
    keyboard input system-wide.
    """

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._thread_id: Optional[int] = None
        self._hook_handle: Optional[int] = None
        # name -> (modifiers, vk_code, callback)
        self._hotkeys: Dict[str, Tuple[int, int, Callable[[], None]]] = {}
        self._keys_down: set = set()  # vk codes currently held, for repeat suppression
        self._swallowed_keyups_pending: set = set()
        self._capslock_used_as_modifier: bool = False

        # -- background capture mode ("type live, no buffer, no auto-send") --
        self._capture_mode: bool = False
        self._capture_toggle_mods: Optional[int] = None
        self._capture_toggle_vk: Optional[int] = None
        self._capture_on_char: Optional[Callable[[str], None]] = None
        self._capture_on_backspace: Optional[Callable[[], None]] = None
        self._lock = threading.RLock()
        self._ready = threading.Event()
        self._hookproc_ref = None  # keep ctypes callback alive

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="HotkeyListenerThread", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout=5):
            logger.error("Hotkey listener thread failed to initialize in time.")

    def stop(self) -> None:
        if self._thread_id is not None:
            if not user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0):
                _log_win32_failure("PostThreadMessageW", "posting WM_QUIT during shutdown")
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None
        self._thread_id = None
        self._hook_handle = None

    # -- registration ------------------------------------------------------
    def register(self, name: str, hotkey_spec: str, callback: Callable[[], None]) -> bool:
        """Register (or replace) a named hotkey. Returns True on success.

        Unlike the previous RegisterHotKey-based version, this can't fail
        because "another application already registered it" - there's no
        such thing as an OS-level claim on a combination anymore, only a
        local dict entry - so this only fails if the spec itself doesn't
        parse.
        """
        try:
            modifiers, vk = parse_hotkey(hotkey_spec)
        except HotkeyParseError as exc:
            logger.error("Could not register hotkey %r (%s): %s", name, hotkey_spec, exc)
            return False

        modifiers &= ~MOD_NOREPEAT  # meaningless to the hook; not used for matching

        with self._lock:
            self._hotkeys[name] = (modifiers, vk, callback)
        logger.info("Registered global hotkey '%s' -> %s (pass-through)", name, hotkey_spec)
        return True

    def unregister(self, name: str) -> None:
        with self._lock:
            if self._hotkeys.pop(name, None) is not None:
                logger.info("Unregistered global hotkey '%s'", name)

    def unregister_all(self) -> None:
        with self._lock:
            self._hotkeys.clear()

    # -- background capture mode --------------------------------------------
    def set_capture_mode_toggle(
        self,
        hotkey_spec: str,
        on_char: Callable[[str], None],
        on_backspace: Callable[[], None],
    ) -> bool:
        """Register the hotkey that toggles "background capture mode" -
        while active, every keystroke is swallowed (never reaches the
        focused app) and forwarded LIVE, one keystroke at a time: regular
        characters go through ``on_char``, Backspace goes through
        ``on_backspace``. There is no internal buffer and no action taken
        on toggle-OFF beyond stopping that forwarding - whatever ``on_char``
        does with each character (e.g. typing it into a page's compose box)
        is the only place the text ever lives.

        Separate from register() (rather than an ordinary hotkey) because
        its behavior is fundamentally different - it doesn't fire once, it
        flips a mode that changes how every subsequent keystroke is
        handled - so it's checked explicitly in
        _low_level_keyboard_proc() before the normal per-hotkey matching
        path, and it's the one combo that always keeps working even while
        capture mode is already on (otherwise there'd be no way to turn it
        back off).
        """
        try:
            modifiers, vk = parse_hotkey(hotkey_spec)
        except HotkeyParseError as exc:
            logger.error("Could not register capture-mode toggle (%s): %s", hotkey_spec, exc)
            return False

        modifiers &= ~MOD_NOREPEAT
        with self._lock:
            self._capture_toggle_mods = modifiers
            self._capture_toggle_vk = vk
            self._capture_on_char = on_char
            self._capture_on_backspace = on_backspace
        logger.info("Registered capture-mode toggle -> %s", hotkey_spec)
        return True

    def _toggle_capture_mode(self) -> None:
        self._capture_mode = not self._capture_mode
        if self._capture_mode:
            logger.info("Capture mode ON - keystrokes are being typed live, silently.")
        else:
            logger.info("Capture mode OFF - back to normal hotkey behavior.")

    def _handle_capture_keydown(self, vk: int, modifiers: int) -> None:
        """Forwards one keydown live - no buffer, nothing held back for
        toggle-off. Held-key auto-repeat is not replayed here (repeats are
        suppressed for all keys at the _low_level_keyboard_proc level,
        same as every other hotkey in this module) - holding Backspace or
        a letter down only registers once, not repeatedly. Known
        limitation, not a bug."""
        if vk == VK_BACK:
            if self._capture_on_backspace is not None:
                threading.Thread(
                    target=self._invoke_capture_backspace, name="CaptureModeBackspace", daemon=True
                ).start()
            return
        if vk == VK_RETURN:
            char = "\n"
        else:
            char = _vk_to_char(vk, shifted=bool(modifiers & MOD_SHIFT))
        if char is not None and self._capture_on_char is not None:
            threading.Thread(
                target=self._invoke_capture_char, args=(char,), name="CaptureModeChar", daemon=True
            ).start()

    def _invoke_capture_char(self, char: str) -> None:
        try:
            self._capture_on_char(char)
        except Exception:
            logger.exception("Capture-mode on_char callback raised an exception.")

    def _invoke_capture_backspace(self) -> None:
        try:
            self._capture_on_backspace()
        except Exception:
            logger.exception("Capture-mode on_backspace callback raised an exception.")

    # -- internals ---------------------------------------------------------
    def _current_modifier_state(self) -> int:
        """Snapshot which of Ctrl/Alt/Shift/Win are currently held, via
        GetAsyncKeyState - checked at the moment the "final" key of a
        combo goes down, same semantics RegisterHotKey used to provide."""
        modifiers = 0
        if user32.GetAsyncKeyState(VK_CONTROL) & 0x8000:
            modifiers |= MOD_CONTROL
        if user32.GetAsyncKeyState(VK_MENU) & 0x8000:
            modifiers |= MOD_ALT
        if user32.GetAsyncKeyState(VK_SHIFT) & 0x8000:
            modifiers |= MOD_SHIFT
        if (user32.GetAsyncKeyState(VK_LWIN) & 0x8000) or (user32.GetAsyncKeyState(VK_RWIN) & 0x8000):
            modifiers |= MOD_WIN
        if VK_CAPITAL in self._keys_down:
            modifiers |= MOD_CAPSLOCK
        return modifiers

    def _handle_keydown(self, vk: int) -> bool:
        """Fire every hotkey this keydown matches. Returns True if the
        keystroke should be swallowed (never forwarded to the focused
        app) - unmatched presses always pass through normally."""
        modifiers = self._current_modifier_state()
        with self._lock:
            matches = [
                (name, cb)
                for name, (mods, key_vk, cb) in self._hotkeys.items()
                if key_vk == vk and mods == modifiers
            ]
        for name, callback in matches:
            threading.Thread(
                target=self._invoke_callback, args=(name, callback), name=f"Hotkey-{name}", daemon=True
            ).start()
        # Capslock-modified combos are always swallowed regardless of which
        # key they end on: Capslock itself never reaches the focused app
        # (it's fully repurposed - see _low_level_keyboard_proc), so letting
        # the other half through would deliver a bare, unmodified keystroke
        # the user never intended to type. Without this, only the keys that
        # happen to be in _SWALLOWABLE_VKS behaved correctly, so rebinding
        # to e.g. "capslock+g" typed a stray "g" into whatever had focus.
        should_swallow = bool(matches) and (
            vk in _SWALLOWABLE_VKS or bool(modifiers & MOD_CAPSLOCK)
        )
        if should_swallow:
            self._swallowed_keyups_pending.add(vk)
        return should_swallow

    def _invoke_callback(self, name: str, callback: Callable[[], None]) -> None:
        try:
            callback()
        except Exception:
            logger.exception("Hotkey callback for '%s' raised an exception.", name)

    def _low_level_keyboard_proc(self, nCode, wParam, lParam):
        swallow = False
        if nCode >= 0:
            try:
                kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                vk = kb.vkCode

                if wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    is_repeat = vk in self._keys_down

                    if vk == VK_CAPITAL:
                        # Capslock is fully repurposed: it never actually
                        # toggles caps-lock state, and its own standalone
                        # action fires on release (see _handle_capslock_release),
                        # not press - that's what lets it double as a modifier
                        # for combos like "capslock+right" without also firing
                        # the standalone action every time.
                        if not is_repeat:
                            self._capslock_used_as_modifier = False
                        self._keys_down.add(vk)
                        swallow = True
                    else:
                        if not is_repeat and VK_CAPITAL in self._keys_down:
                            # A different key went down while Capslock is
                            # currently held - this is a modifier combo, not
                            # a standalone Capslock tap.
                            self._capslock_used_as_modifier = True
                        self._keys_down.add(vk)
                        if not is_repeat:
                            current_mods = self._current_modifier_state()
                            if (
                                self._capture_toggle_vk is not None
                                and vk == self._capture_toggle_vk
                                and current_mods == self._capture_toggle_mods
                            ):
                                # Checked before the capture-mode branch
                                # below so this combo always works to turn
                                # capture mode back OFF too, not just on.
                                self._toggle_capture_mode()
                                swallow = True
                                self._swallowed_keyups_pending.add(vk)
                            elif self._capture_mode:
                                # Swallow-everything mode: every other key
                                # becomes buffered text instead of a normal
                                # hotkey action.
                                self._handle_capture_keydown(vk, current_mods)
                                swallow = True
                                self._swallowed_keyups_pending.add(vk)
                            elif self._handle_keydown(vk):
                                swallow = True
                        elif self._capture_mode:
                            # Held-key auto-repeat: not replayed into the
                            # buffer (see _handle_capture_keydown's
                            # docstring), but still must never leak through
                            # to the focused app while capture mode is on.
                            swallow = True

                elif wParam in (WM_KEYUP, WM_SYSKEYUP):
                    self._keys_down.discard(vk)
                    if vk == VK_CAPITAL:
                        swallow = True
                        if not self._capslock_used_as_modifier:
                            self._handle_capslock_release()
                    elif vk in self._swallowed_keyups_pending:
                        self._swallowed_keyups_pending.discard(vk)
                        swallow = True
            except Exception:
                logger.exception("Error in low-level keyboard hook callback.")

        if swallow:
            return 1  # non-zero return value swallows the keystroke
        return user32.CallNextHookEx(None, nCode, wParam, lParam)
    
    def _handle_capslock_release(self) -> None:
        """Fires the standalone Capslock-alone hotkey (if one is registered)
        on release rather than press. Only called when Capslock wasn't used
        as a modifier during this hold (see _capslock_used_as_modifier) -
        this is what lets "capslock+right" et al. coexist with a standalone
        "capslock" binding without the standalone one also firing.

        Also suppressed entirely while capture mode is active: a bare
        Capslock tap during capture mode (not part of the capslock+t
        toggle-off combo, which is handled separately) shouldn't fire
        show/hide or any other standalone action mid-dictation."""
        if self._capture_mode:
            return
        with self._lock:
            matches = [
                (name, cb)
                for name, (mods, key_vk, cb) in self._hotkeys.items()
                if key_vk == VK_CAPITAL and mods == 0
            ]
        for name, callback in matches:
            threading.Thread(
                target=self._invoke_callback, args=(name, callback), name=f"Hotkey-{name}", daemon=True
            ).start()

    def _run(self) -> None:
        if user32 is None or kernel32 is None:
            logger.error("Win32 hotkey support is unavailable on this platform.")
            self._ready.set()
            return

        self._thread_id = kernel32.GetCurrentThreadId()
        self._hookproc_ref = HOOKPROC(self._low_level_keyboard_proc)

        ctypes.set_last_error(0)
        hinstance = kernel32.GetModuleHandleW(None)
        if not hinstance:
            _log_win32_failure("GetModuleHandleW", "resolving the current module handle")
            self._ready.set()
            return

        ctypes.set_last_error(0)
        self._hook_handle = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._hookproc_ref, hinstance, 0
        )
        if not self._hook_handle:
            _log_win32_failure("SetWindowsHookExW", "installing the low-level keyboard hook")
            self._ready.set()
            return

        logger.debug("Low-level keyboard hook installed (handle=%s).", self._hook_handle)
        self._ready.set()

        # Pumps messages purely to keep this thread responsive to the hook
        # dispatch mechanism and to WM_QUIT (posted by stop()) - no window
        # is created or needed for a WH_KEYBOARD_LL hook.
        msg = wintypes.MSG()
        while True:
            ctypes.set_last_error(0)
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret == 0:
                break  # WM_QUIT
            if ret == -1:
                _log_win32_failure("GetMessageW", "running the hotkey message loop")
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        if self._hook_handle:
            if not user32.UnhookWindowsHookEx(self._hook_handle):
                _log_win32_failure("UnhookWindowsHookEx", "removing the low-level keyboard hook")
            self._hook_handle = None

        logger.info("Hotkey listener message loop exited.")