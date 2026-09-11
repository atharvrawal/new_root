# todo

Status: **the app runs and works end to end.** Verified this session on Windows
11 / Python 3.13 — window appears, is excluded from capture and Alt+Tab, all
hotkeys register, a screenshot + prompt round-trips through Gemini
(`gemini-3.6-flash`) and renders in the log.

What follows is what is left, roughly in the order worth doing.

---

## Fixed (verified)

Kept here so the next person doesn't re-diagnose them.

- **Hotkey callbacks were touching Qt widgets from a worker thread.** Every
  hotkey arrives on a throwaway thread spawned by the keyboard hook, and
  `send_message` / `capture_and_attach` were constructing `QWidget`s and
  `QThread`s directly on it. Undefined behavior — works until it doesn't, then
  crashes or hangs with no traceback. All actions now marshal onto the Qt main
  thread via `AppController.dispatch` (`app.py: _wire_hotkey_actions`).
- **`capslock+<key>` leaked the keystroke into the focused app** for any key
  outside a hardcoded list, so rebinding to e.g. `capslock+g` typed a stray "g"
  into whatever had focus. Swallowing is now decided by whether the match used
  Capslock, in one place (`win32_hotkey.py: _handle_keydown`).
- **Saving settings reverted the window's size and position**, because the
  dialog wrote back the `AppConfig` snapshot taken when it opened, discarding
  anything the move/resize hotkeys had persisted since.
- **The auto-scroll handler was reconnected on every response**, accumulating
  connections and yanking the scrollbar down whenever any message re-laid out.
- `remove_native_border` was defined twice, verbatim, in `window_utils.py`.
- Default screenshot monitor was index 2 — nonexistent on a single-monitor
  machine, silently falling back to the combined virtual screen. Now 1
  (primary), matching the documented intent.
- **No way to discard a queue without sending it.** Added `clear_queue`
  (`Capslock+Backspace`), which `ImageQueue.clear()` already existed for.
- Live-typed characters in background capture mode did not update the status
  line, so you were typing blind. Deleted `ui/capture_input.py` and routed
  through the existing `insert_text`/`backspace`, which already do.
- Removed dead `find_hwnd_by_title` and fixed docstrings across seven modules
  still describing the pre-rewrite pywebview/adapter architecture and pointing
  at files that no longer exist.
- **UI rework for readability.** `setMarkdown()` ignores `setDefaultStyleSheet`,
  so code blocks rendered as undifferentiated body text — the worst possible
  outcome for a window whose main job is showing code. `message_widget.py` now
  walks the document and styles Qt's `BlockCodeFence` blocks directly:
  monospace, inset slab, tight leading. Also fixed markdown tables rendering
  with a harsh white grid, list markers rendering undersized, and added
  per-response headers (`#01  14:32`) so consecutive answers don't run
  together. Palette moved to `ui/theme.py` and pushed to high contrast
  deliberately — this window is often faded via the opacity hotkeys, where
  tasteful mid-greys become unreadable. Verified by rendering to PNG and by
  compositing at alpha 100/255 over light and dark backdrops.
- Added `README.md`, `requirements-build.txt`, `.gitignore` and
  `test_basics.py` — the first two were referenced by `main.py` and
  `requirements.txt` but never existed.

---

## 1. Packaging (the one real gap)

`main.py` advertises a PyInstaller executable but it has never been built.
With voice capture gone there are no lazily-loaded native DLLs left, so
`pyinstaller --windowed main.py` should just work — but nobody has checked.

- [ ] Build it and run the exe on a clean machine.
- [ ] Confirm logging still works windowed (`_is_frozen_windowed` disables the
      console handler; the file handler must still land in `%LOCALAPPDATA%`).

## 2. No timeout on the Gemini call

`gemini/client.py` calls `generate_content` with whatever the SDK's default
timeout is. If the network stalls, the worker `QThread` blocks indefinitely and
the status line stays on "Sending..." with no way to cancel.

- [ ] Pass an explicit timeout via `http_options`, and surface a cancel or at
      least a timed-out error bubble.

## 3. API key is stored in plaintext

`%APPDATA%\ThumbnailAssistant\config.json` holds the key in the clear, and
`config.json.bak` keeps a copy whenever the file fails to parse.

- [ ] Either encrypt it with DPAPI (`CryptProtectData`, user-scoped, no new
      dependency) or read it from an environment variable and document that the
      file is only as protected as the user profile.

## 4. Smaller things

- [ ] **Two UI toolkits**: the main window is PySide6, the settings dialog is
      tkinter on its own thread with its own mainloop. It works, but it's an
      odd seam and the dialog looks nothing like the app.
- [ ] **Monitor picker is a raw index field.** Enumerate monitors in Settings
      instead of asking the user to guess a number.
- [ ] **Responses arrive all at once**, not streamed. `client.send` is one
      blocking call; `generate_content_stream` plus incremental
      `MessageWidget` updates would feel much faster on long answers.
- [ ] **No git repo.** `git init` — `.gitignore` is already in place.
- [ ] `debug_screenshots/` grows forever. Fine day to day; prune on startup if
      it ever matters.

## Not planned

- Voice capture / any audio recording — removed; not coming back.
- Conversation history — every send is deliberately one-shot and independent.
- A tray icon or background residency — closing the window exits the process,
  by design.
