# Thumbnail Assistant

A frameless, always-on-top, screen-capture-excluded desktop overlay for Windows.
Global hotkeys queue screenshots and prompt text (typed or dictated), send the
whole batch to the Gemini API in one request, and render the response as
markdown in a scrollable log — without ever stealing focus from whatever is on
screen.

## Requirements

- Windows 10 build 19041 (2004) or later — `WDA_EXCLUDEFROMCAPTURE`, used for
  the capture exclusion, does not exist on earlier builds.
- Python 3.10+ (developed and verified on 3.13).
- A Gemini API key: <https://aistudio.google.com/apikey>

## Install and run

```
pip install -r requirements.txt
python main.py
```

On first run the window appears with an empty response log. Press
**Ctrl+Alt+S** to open Settings and paste in your Gemini API key — nothing can
be sent until you do.

## How it works

Capture, prompt, and send are three independent actions. Nothing is sent until
you explicitly send, and sending clears the queue.

1. **Queue a screenshot** (`Ctrl+Alt+G`) — as many times as you like; every
   queued image goes into the same request.
2. **Write the prompt** in the input box at the bottom, or:
   - `Capslock+P` — insert the prompt configured in Settings, or
   - `Capslock+T` — background capture mode: every keystroke is swallowed and
     typed into the input box instead, without the window ever taking focus.
     Press `Capslock+T` again to exit. While it is on, the input box gets a
     white border and a `CAPTURE MODE` badge.

     Editing works as it would if the box had focus, because the keystrokes
     are replayed to it as real key events: arrows and Home/End move the
     caret, Shift selects, Ctrl+A/C/V/X/Z do the usual, and held keys repeat.
     Characters come from your actual keyboard layout, not a US-only table.
3. **Send** — `Capslock+Enter` from anywhere, or `Enter` when the window has
   focus (`Shift+Enter` makes a new line). Bundles every queued screenshot plus
   the prompt into one Gemini call and appends the response.

The input box is the prompt: whatever it holds is what gets sent, whether you
typed it there directly or a hotkey put it there. It grows as you type and
scrolls once it gets tall. The row beneath it carries everything else — how
many screenshots are attached, and live state such as sending, recording or a
failure. Screenshots are a count, never a preview. `Capslock+Backspace` throws
away the queue and clears the box.

Voice capture (`Ctrl+Alt+V`) is separate: it records the mic (mixed with desktop
audio via WASAPI loopback, when available), transcribes locally with
faster-whisper *while you are still talking*, and on the second press sends the
transcript to Gemini as its own request — it does not touch the image queue.

### Default hotkeys

| Action | Default |
| --- | --- |
| Show / hide window | `Ctrl+H` |
| Capture screenshot & queue | `Ctrl+Alt+G` |
| Insert configured prompt | `Capslock+P` |
| Background capture mode (type live) | `Capslock+T` |
| Send queued batch to Gemini | `Capslock+Enter` |
| Discard the queue | `Capslock+Backspace` |
| Voice capture start / stop | `Ctrl+Alt+V` |
| Focus window (to click / scroll) | `Ctrl+Alt+F` |
| Open settings | `Ctrl+Alt+S` |
| Move window | `Alt+arrows` |
| Resize window | `Alt+Shift+arrows` |
| Opacity up / down | `Alt+=` / `Alt+-` |

All are rebindable in Settings, in `modifier+modifier+key` form (`ctrl`, `alt`,
`shift`, `win`, `capslock`). Leave a field blank to disable that action.

Caps Lock is fully repurposed as a modifier — it never toggles caps-lock state
while the app is running, and a bare tap fires whatever standalone action is
bound to `capslock` (nothing, by default).

## Where things live

| | |
| --- | --- |
| Config | `%APPDATA%\ThumbnailAssistant\config.json` |
| Logs | `%LOCALAPPDATA%\ThumbnailAssistant\logs\app.log` |
| Debug screenshots | `%LOCALAPPDATA%\ThumbnailAssistant\debug_screenshots\` |

Every capture is also written to `debug_screenshots\` exactly as sent, which is
the fastest way to tell a bad capture apart from a bad prompt. It is never
cleaned up automatically; delete it whenever.

The config file holds your API key in plain text, same as the log directory
holds your prompts — both are per-user paths, not encrypted.

## Verifying it works

```
python test_basics.py
```

Covers the GUI-free logic: hotkey parsing, the queue, config round-tripping, PNG
header parsing, STT framing. The Qt window, the Win32 chrome, the keyboard hook
and the Gemini call need the real app — run `python main.py`, press
`Ctrl+Alt+G` then `Capslock+Enter`, and check `app.log` if nothing appears.

To see which models your key can actually call:

```
python check_models.py
```

`models.list()` only shows what is *visible* to a key; it says nothing about
what your quota tier is allowed to *use*. This sends a one-token request to
each text/vision model and reports OK / quota-blocked / unavailable.

## Limitations

- **Windows only.** The hotkey hook, capture exclusion, taskbar hiding, opacity
  and desktop-audio loopback are all Win32-specific.
- **Capture exclusion is not DRM.** It removes the window from the DWM
  compositor's output, which covers the Windows Graphics Capture API,
  `BitBlt`/`PrintWindow`, and the conferencing tools built on them. It does not
  hide the window from a phone camera, a capture card, or a kernel-level
  screen reader.
- **A low-level keyboard hook** is how hotkeys stay pass-through. Anti-cheat and
  EDR software sometimes flag or block that.
- **True exclusive-fullscreen games** bypass the compositor entirely and cannot
  be overlaid by any window, including this one. Borderless windowed works.

## Packaging

```
pip install -r requirements-build.txt
pyinstaller --noconfirm --windowed --name ThumbnailAssistant main.py
```

Untested as of this writing — the faster-whisper/ctranslate2 CUDA DLLs need
`collect_dynamic_libs` entries in a `.spec` file to survive freezing (see
`voice.py: _register_nvidia_dll_dirs`, which already handles the frozen case at
runtime). See `todo.md`.
