"""Settings window for configuring hotkeys, startup behavior, and the
Gemini API key/model.

Still tkinter (stdlib), unchanged reasoning from before: this is a small
config dialog, not the main UI, so it doesn't need PySide6's rendering
power and runs happily on its own thread with its own Tk mainloop
alongside the PySide6 main window/event loop.

Layout notes
------------
The content area lives inside a scrollable canvas; Save/Cancel/status are
deliberately kept OUTSIDE that scrollable area, pinned to the bottom, so
they're always reachable regardless of window size or content height.
"""
from __future__ import annotations

import logging
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable, Dict, Optional

from ..config import AppConfig, ConfigManager
from ..hotkeys.win32_hotkey import HotkeyParseError, parse_hotkey

logger = logging.getLogger(__name__)

_DEFAULT_WIDTH = 620
_DEFAULT_HEIGHT = 680
_MIN_WIDTH = 460
_MIN_HEIGHT = 360


class SettingsWindow:
    def __init__(
        self,
        config_manager: ConfigManager,
        action_labels: Dict[str, str],
        on_saved: Optional[Callable[[], None]] = None,
        window_title: str = "Thumbnail Assistant - Settings",
        default_capture_prompt: str = "",
    ):
        self._config_manager = config_manager
        self._action_labels = dict(action_labels)
        self._on_saved = on_saved
        self._window_title = window_title
        self._default_capture_prompt = default_capture_prompt
        self._thread: Optional[threading.Thread] = None

    def open(self) -> None:
        if self._thread and self._thread.is_alive():
            logger.debug("Settings window already open.")
            return
        self._thread = threading.Thread(target=self._run, name="SettingsWindowThread", daemon=True)
        self._thread.start()

    # -- UI ---------------------------------------------------------------
    def _run(self) -> None:
        cfg = self._config_manager.config
        root = tk.Tk()
        root.title(self._window_title)

        root.minsize(_MIN_WIDTH, _MIN_HEIGHT)
        root.resizable(True, True)

        root.update_idletasks()
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        width = min(_DEFAULT_WIDTH, screen_w - 80)
        height = min(_DEFAULT_HEIGHT, screen_h - 80)
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 3)
        root.geometry(f"{width}x{height}+{x}+{y}")

        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TLabelframe", padding=(10, 8))
        style.configure("TLabelframe.Label", font=("Segoe UI", 9, "bold"))
        style.configure("Hint.TLabel", foreground="#666666")
        style.configure("Status.TLabel", foreground="#a33333")

        padding = {"padx": 10, "pady": 6}

        content_area = ttk.Frame(root)
        content_area.pack(side="top", fill="both", expand=True)

        bg_color = style.lookup("TFrame", "background") or root.cget("bg")
        canvas = tk.Canvas(content_area, highlightthickness=0, bg=bg_color)
        vscroll = ttk.Scrollbar(content_area, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vscroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        vscroll.pack(side="right", fill="y")

        main_frame = ttk.Frame(canvas)
        main_frame_window = canvas.create_window((0, 0), window=main_frame, anchor="nw")

        def _on_main_frame_configure(_event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_configure(event) -> None:
            canvas.itemconfig(main_frame_window, width=event.width)

        main_frame.bind("<Configure>", _on_main_frame_configure)
        canvas.bind("<Configure>", _on_canvas_configure)

        def _on_mousewheel(event) -> None:
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        # -- Startup behavior ------------------------------------------
        startup_frame = ttk.LabelFrame(main_frame, text="Startup")
        startup_frame.pack(fill="x", **padding)

        start_minimized_var = tk.BooleanVar(value=cfg.start_minimized)
        ttk.Checkbutton(
            startup_frame,
            text="Start minimized",
            variable=start_minimized_var,
        ).pack(anchor="w", padx=8, pady=4)

        remember_pos_var = tk.BooleanVar(value=cfg.remember_window_position)
        ttk.Checkbutton(
            startup_frame,
            text="Remember window position",
            variable=remember_pos_var,
        ).pack(anchor="w", padx=8, pady=4)

        # -- Gemini API ---------------------------------------------------
        gemini_frame = ttk.LabelFrame(main_frame, text="Gemini API")
        gemini_frame.pack(fill="x", **padding)
        gemini_frame.columnconfigure(1, weight=1)

        ttk.Label(gemini_frame, text="API key:").grid(row=0, column=0, sticky="w", padx=8, pady=4)
        api_key_var = tk.StringVar(value=cfg.gemini_api_key or "")
        ttk.Entry(gemini_frame, textvariable=api_key_var, show="*", width=40).grid(
            row=0, column=1, sticky="ew", padx=8, pady=4
        )

        ttk.Label(gemini_frame, text="Model:").grid(row=1, column=0, sticky="w", padx=8, pady=4)
        model_var = tk.StringVar(value=cfg.gemini_model)
        ttk.Entry(gemini_frame, textvariable=model_var, width=30).grid(
            row=1, column=1, sticky="w", padx=8, pady=4
        )

        # -- Prompts ------------------------------------------------------
        prompts_frame = ttk.LabelFrame(main_frame, text="Prompts")
        prompts_frame.pack(fill="x", **padding)
        prompts_frame.columnconfigure(1, weight=1)

        ttk.Label(prompts_frame, text="Screenshot prompt:").grid(
            row=0, column=0, sticky="nw", padx=8, pady=4
        )
        capture_prompt_text = tk.Text(prompts_frame, height=4, wrap="word")
        capture_prompt_text.grid(row=0, column=1, sticky="ew", padx=8, pady=4)
        capture_prompt_text.insert("1.0", cfg.capture_prompt or "")
        ttk.Label(
            prompts_frame,
            text=f"Blank = built-in default: \"{self._default_capture_prompt}\"",
            style="Hint.TLabel",
            wraplength=360,
        ).grid(row=1, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 8))

        ttk.Label(prompts_frame, text="Screenshot monitor index:").grid(
            row=2, column=0, sticky="w", padx=8, pady=4
        )
        monitor_index_var = tk.StringVar(value=str(cfg.monitor_index) if cfg.monitor_index is not None else "")
        ttk.Entry(prompts_frame, textvariable=monitor_index_var, width=10).grid(
            row=2, column=1, sticky="w", padx=8, pady=4
        )
        ttk.Label(
            prompts_frame,
            text="0 = all monitors combined, 1 = primary, 2/3/... = other monitors. Blank = default.",
            style="Hint.TLabel",
            wraplength=360,
        ).grid(row=3, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 4))

        # -- Hotkeys ------------------------------------------------------
        hotkeys_frame = ttk.LabelFrame(main_frame, text="Global Hotkeys")
        hotkeys_frame.pack(fill="x", **padding)
        hotkeys_frame.columnconfigure(1, weight=1)

        hotkeys_enabled_var = tk.BooleanVar(value=cfg.hotkeys_enabled)
        ttk.Checkbutton(
            hotkeys_frame,
            text="Enable global hotkeys",
            variable=hotkeys_enabled_var,
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=8, pady=(4, 8))

        entries: dict[str, tk.StringVar] = {}
        row = 1
        for action_name, label in self._action_labels.items():
            ttk.Label(hotkeys_frame, text=label + ":", wraplength=260).grid(
                row=row, column=0, sticky="w", padx=8, pady=4
            )
            var = tk.StringVar(value=cfg.hotkeys.get(action_name, ""))
            entries[action_name] = var
            entry = ttk.Entry(hotkeys_frame, textvariable=var, width=22)
            entry.grid(row=row, column=1, sticky="w", padx=8, pady=4)
            row += 1

        ttk.Label(
            hotkeys_frame,
            text='Format: modifier+modifier+key, e.g. "ctrl+alt+m" (leave blank to disable)',
            style="Hint.TLabel",
            wraplength=460,
        ).grid(row=row, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 6))

        # -- Bottom bar: Save / Cancel / status ------------------------------
        ttk.Separator(root, orient="horizontal").pack(side="top", fill="x")

        bottom_frame = ttk.Frame(root)
        bottom_frame.pack(side="bottom", fill="x")

        status_var = tk.StringVar(value="")
        status_label = ttk.Label(
            bottom_frame, textvariable=status_var, style="Status.TLabel", wraplength=560
        )
        status_label.pack(fill="x", padx=10, pady=(8, 0))

        button_frame = ttk.Frame(bottom_frame)
        button_frame.pack(fill="x", padx=10, pady=8)

        def validate_hotkeys() -> bool:
            for action_name, var in entries.items():
                spec = var.get().strip()
                if not spec:
                    continue
                try:
                    parse_hotkey(spec)
                except HotkeyParseError as exc:
                    label = self._action_labels.get(action_name, action_name)
                    status_var.set(f"Invalid hotkey for '{label}': {exc}")
                    return False
            return True

        def on_save():
            if not validate_hotkeys():
                return
            merged_hotkeys = dict(cfg.hotkeys)
            merged_hotkeys.update({name: var.get().strip() for name, var in entries.items()})

            capture_prompt = capture_prompt_text.get("1.0", "end").strip() or None
            monitor_index_raw = monitor_index_var.get().strip()
            if monitor_index_raw and not monitor_index_raw.lstrip("-").isdigit():
                status_var.set("Monitor index must be a whole number.")
                return
            monitor_index = int(monitor_index_raw) if monitor_index_raw else None

            model_value = model_var.get().strip() or cfg.gemini_model

            # Re-read rather than reusing the `cfg` captured when the
            # window opened: move/resize/opacity hotkeys persist through
            # ConfigManager.update_quiet(), which swaps in a whole new
            # AppConfig object, so anything the user did to the window
            # while this dialog sat open is only in the *current* one.
            # Saving the stale copy would silently revert it.
            live = self._config_manager.config

            new_cfg = AppConfig(
                start_minimized=start_minimized_var.get(),
                remember_window_position=remember_pos_var.get(),
                window_width=live.window_width,
                window_height=live.window_height,
                window_x=live.window_x,
                window_y=live.window_y,
                hotkeys=merged_hotkeys,
                hotkeys_enabled=hotkeys_enabled_var.get(),
                window_opacity=live.window_opacity,
                capture_prompt=capture_prompt,
                monitor_index=monitor_index,
                gemini_api_key=api_key_var.get().strip() or None,
                gemini_model=model_value,
            )
            self._config_manager.save(new_cfg)
            logger.info("Settings saved from settings window.")
            if self._on_saved:
                try:
                    self._on_saved()
                except Exception:
                    logger.exception("on_saved callback raised.")
            messagebox.showinfo("Settings", "Settings saved.", parent=root)
            root.destroy()

        def on_cancel():
            root.destroy()

        ttk.Button(button_frame, text="Save", command=on_save).pack(side="right", padx=4)
        ttk.Button(button_frame, text="Cancel", command=on_cancel).pack(side="right")

        root.mainloop()
