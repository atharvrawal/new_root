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
2. **Queue prompt text**, by either:
   - `Capslock+P` — insert the prompt configured in Settings, or
   - `Capslock+T` — background capture mode: every keystroke is typed live into
     the queued prompt and swallowed, so it never reaches the focused app.
     Press `Capslock+T` again to exit. Backspace edits; there is no auto-send.
3. **Send** (`Capslock+Enter`) — bundles every queued image plus the prompt into
   one Gemini call and appends the response to the log.

The status line above the log always shows what is currently queued.
`Capslock+Backspace` throws the queue away without sending.

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
- **US QWERTY only** for background capture mode — the vk-to-character table is
  static, so non-US layouts will produce wrong characters for keys that differ.
- **True exclusive-fullscreen games** bypass the compositor entirely and cannot
  be overlaid by any window, including this one. Borderless windowed works.
- **Held-key auto-repeat is not replayed** in capture mode: holding a key types
  one character, not many.

## Packaging

```
pip install -r requirements-build.txt
pyinstaller --noconfirm --windowed --name ThumbnailAssistant main.py
```

Untested as of this writing — the faster-whisper/ctranslate2 CUDA DLLs need
`collect_dynamic_libs` entries in a `.spec` file to survive freezing (see
`voice.py: _register_nvidia_dll_dirs`, which already handles the frozen case at
runtime). See `todo.md`.
