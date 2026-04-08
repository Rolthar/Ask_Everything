"""settings.py — tkinter settings window.

Provides a simple form for editing the values stored in .env / settings.json.
Changes are written back to the .env file and take effect on the next app start
(some settings, like theme and font size, require restart).
"""

import logging
import os
import pathlib
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from typing import Optional

import config
import cache as cache_mod
import history as history_mod

logger = logging.getLogger(__name__)

_ENV_FILE = pathlib.Path(__file__).resolve().parent / ".env"


def _read_env() -> dict[str, str]:
    """Parse the current .env file into a dict."""
    result: dict[str, str] = {}
    if _ENV_FILE.exists():
        for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip()
    return result


def _write_env(values: dict[str, str]) -> None:
    """Merge *values* into the .env file, preserving comments and ordering."""
    existing_lines: list[str] = []
    if _ENV_FILE.exists():
        existing_lines = _ENV_FILE.read_text(encoding="utf-8").splitlines()

    updated_keys: set[str] = set()
    new_lines: list[str] = []
    for line in existing_lines:
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            new_lines.append(line)
            continue
        k = stripped.split("=", 1)[0].strip()
        if k in values:
            new_lines.append(f"{k}={values[k]}")
            updated_keys.add(k)
        else:
            new_lines.append(line)

    for k, v in values.items():
        if k not in updated_keys:
            new_lines.append(f"{k}={v}")

    # Write the updated .env file.  Credentials are stored as plaintext in the
    # .env file — this is the intended, industry-standard behaviour for local
    # development configuration.  The file should not be committed to source
    # control (it is excluded via .gitignore).
    _ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


class SettingsWindow:
    def __init__(self, parent: Optional[tk.Tk] = None):
        self._parent = parent
        self._win: Optional[tk.Toplevel | tk.Tk] = None

    def show(self) -> None:
        if self._win and self._win.winfo_exists():
            self._win.lift()
            return
        self._build()

    def _build(self) -> None:
        if self._parent:
            win = tk.Toplevel(self._parent)
        else:
            win = tk.Tk()
        self._win = win
        win.title("Ask Everything — Settings")
        win.resizable(False, False)
        win.grab_set()

        env = _read_env()
        pad = dict(padx=12, pady=6)

        notebook = ttk.Notebook(win)
        notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # ── Tab: API ──────────────────────────────────────────────────────
        api_tab = ttk.Frame(notebook)
        notebook.add(api_tab, text="API")

        ttk.Label(api_tab, text="Anthropic API key:").grid(
            row=0, column=0, sticky=tk.W, **pad
        )
        self._api_key_var = tk.StringVar(
            value=env.get("ANTHROPIC_API_KEY", "")
        )
        api_entry = ttk.Entry(api_tab, textvariable=self._api_key_var,
                              width=50, show="•")
        api_entry.grid(row=0, column=1, sticky=tk.EW, **pad)

        show_var = tk.BooleanVar(value=False)

        def _toggle_show():
            api_entry.config(show="" if show_var.get() else "•")

        ttk.Checkbutton(
            api_tab, text="Show", variable=show_var, command=_toggle_show
        ).grid(row=0, column=2, **pad)

        # ── Tab: Search ───────────────────────────────────────────────────
        search_tab = ttk.Frame(notebook)
        notebook.add(search_tab, text="Search")

        ttk.Label(search_tab, text="Hotkey:").grid(
            row=0, column=0, sticky=tk.W, **pad
        )
        self._hotkey_var = tk.StringVar(
            value=env.get("HOTKEY", config.HOTKEY)
        )
        ttk.Entry(search_tab, textvariable=self._hotkey_var, width=24).grid(
            row=0, column=1, sticky=tk.W, **pad
        )

        ttk.Label(search_tab, text="Max results (20–200):").grid(
            row=1, column=0, sticky=tk.W, **pad
        )
        self._max_results_var = tk.IntVar(
            value=int(env.get("MAX_RESULTS", str(config.MAX_RESULTS)))
        )
        ttk.Scale(
            search_tab,
            from_=20, to=200,
            variable=self._max_results_var,
            orient=tk.HORIZONTAL,
            length=200,
        ).grid(row=1, column=1, sticky=tk.W, **pad)
        ttk.Label(search_tab, textvariable=self._max_results_var).grid(
            row=1, column=2, **pad
        )

        ttk.Label(search_tab, text="Default sort column:").grid(
            row=2, column=0, sticky=tk.W, **pad
        )
        self._sort_col_var = tk.StringVar(
            value=env.get("DEFAULT_SORT_COLUMN", config.DEFAULT_SORT_COLUMN)
        )
        ttk.Combobox(
            search_tab,
            textvariable=self._sort_col_var,
            values=["name", "path", "size", "date_modified"],
            state="readonly",
            width=16,
        ).grid(row=2, column=1, sticky=tk.W, **pad)

        ttk.Label(search_tab, text="Default sort direction:").grid(
            row=3, column=0, sticky=tk.W, **pad
        )
        self._sort_dir_var = tk.StringVar(
            value=env.get("DEFAULT_SORT_DIRECTION", config.DEFAULT_SORT_DIRECTION)
        )
        ttk.Combobox(
            search_tab,
            textvariable=self._sort_dir_var,
            values=["asc", "desc"],
            state="readonly",
            width=8,
        ).grid(row=3, column=1, sticky=tk.W, **pad)

        ttk.Label(search_tab, text="Result validation:").grid(
            row=4, column=0, sticky=tk.W, **pad
        )
        self._validation_var = tk.BooleanVar(
            value=env.get("RESULT_VALIDATION", "true").lower() == "true"
        )
        ttk.Checkbutton(
            search_tab, variable=self._validation_var,
            text="Check path existence before displaying results"
        ).grid(row=4, column=1, columnspan=2, sticky=tk.W, **pad)

        ttk.Label(search_tab, text="Everything.exe path:").grid(
            row=5, column=0, sticky=tk.W, **pad
        )
        self._ev_path_var = tk.StringVar(
            value=env.get("EVERYTHING_PATH", config.EVERYTHING_PATH)
        )
        ttk.Entry(search_tab, textvariable=self._ev_path_var, width=40).grid(
            row=5, column=1, sticky=tk.EW, **pad
        )
        ttk.Button(
            search_tab, text="Browse…",
            command=self._browse_everything_path,
        ).grid(row=5, column=2, **pad)

        # ── Tab: Cache ────────────────────────────────────────────────────
        cache_tab = ttk.Frame(notebook)
        notebook.add(cache_tab, text="Cache")

        self._cache_enabled_var = tk.BooleanVar(
            value=env.get("CACHE_ENABLED", "true").lower() == "true"
        )
        ttk.Checkbutton(
            cache_tab, text="Enable persistent cache",
            variable=self._cache_enabled_var,
        ).grid(row=0, column=0, columnspan=2, sticky=tk.W, **pad)

        ttk.Label(cache_tab, text="Cache TTL (seconds):").grid(
            row=1, column=0, sticky=tk.W, **pad
        )
        self._cache_ttl_var = tk.IntVar(
            value=int(env.get("CACHE_TTL", str(config.CACHE_TTL)))
        )
        ttk.Entry(cache_tab, textvariable=self._cache_ttl_var, width=10).grid(
            row=1, column=1, sticky=tk.W, **pad
        )

        ttk.Button(
            cache_tab, text="Clear cache now",
            command=self._clear_cache,
        ).grid(row=2, column=0, sticky=tk.W, **pad)

        # ── Tab: UI ───────────────────────────────────────────────────────
        ui_tab = ttk.Frame(notebook)
        notebook.add(ui_tab, text="Appearance")

        ttk.Label(ui_tab, text="Theme:").grid(
            row=0, column=0, sticky=tk.W, **pad
        )
        self._theme_var = tk.StringVar(
            value=env.get("THEME", config.THEME)
        )
        ttk.Combobox(
            ui_tab,
            textvariable=self._theme_var,
            values=["auto", "light", "dark"],
            state="readonly",
            width=10,
        ).grid(row=0, column=1, sticky=tk.W, **pad)

        ttk.Label(ui_tab, text="Font size:").grid(
            row=1, column=0, sticky=tk.W, **pad
        )
        self._font_size_var = tk.IntVar(
            value=int(env.get("FONT_SIZE", str(config.FONT_SIZE)))
        )
        ttk.Scale(
            ui_tab, from_=8, to=24,
            variable=self._font_size_var,
            orient=tk.HORIZONTAL,
            length=160,
        ).grid(row=1, column=1, sticky=tk.W, **pad)
        ttk.Label(ui_tab, textvariable=self._font_size_var).grid(
            row=1, column=2, **pad
        )

        ttk.Label(ui_tab, text="Window opacity (0.85–1.0):").grid(
            row=2, column=0, sticky=tk.W, **pad
        )
        self._opacity_var = tk.DoubleVar(
            value=float(env.get("WINDOW_OPACITY", str(config.WINDOW_OPACITY)))
        )
        ttk.Scale(
            ui_tab, from_=0.85, to=1.0,
            variable=self._opacity_var,
            orient=tk.HORIZONTAL,
            length=160,
        ).grid(row=2, column=1, sticky=tk.W, **pad)

        # ── Tab: History ──────────────────────────────────────────────────
        hist_tab = ttk.Frame(notebook)
        notebook.add(hist_tab, text="History")

        ttk.Button(
            hist_tab, text="Clear all history",
            command=self._clear_history,
        ).grid(row=0, column=0, sticky=tk.W, **pad)

        ttk.Button(
            hist_tab, text="Export history to CSV…",
            command=self._export_history,
        ).grid(row=0, column=1, sticky=tk.W, **pad)

        # ── Buttons ───────────────────────────────────────────────────────
        btn_frame = ttk.Frame(win)
        btn_frame.pack(fill=tk.X, padx=10, pady=8)

        ttk.Button(btn_frame, text="Save", command=self._save).pack(
            side=tk.RIGHT, padx=4
        )
        ttk.Button(btn_frame, text="Cancel",
                   command=win.destroy).pack(side=tk.RIGHT, padx=4)

        win.columnconfigure(1, weight=1)

    def _browse_everything_path(self) -> None:
        path = filedialog.askopenfilename(
            title="Select Everything.exe",
            filetypes=[("Executable", "*.exe"), ("All files", "*.*")],
        )
        if path:
            self._ev_path_var.set(path)

    def _save(self) -> None:
        values = {
            "ANTHROPIC_API_KEY": self._api_key_var.get().strip(),
            "HOTKEY":            self._hotkey_var.get().strip(),
            "MAX_RESULTS":       str(int(self._max_results_var.get())),
            "DEFAULT_SORT_COLUMN":    self._sort_col_var.get(),
            "DEFAULT_SORT_DIRECTION": self._sort_dir_var.get(),
            "RESULT_VALIDATION": "true" if self._validation_var.get() else "false",
            "EVERYTHING_PATH":   self._ev_path_var.get().strip(),
            "CACHE_ENABLED":     "true" if self._cache_enabled_var.get() else "false",
            "CACHE_TTL":         str(int(self._cache_ttl_var.get())),
            "THEME":             self._theme_var.get(),
            "FONT_SIZE":         str(int(self._font_size_var.get())),
            "WINDOW_OPACITY":    f"{self._opacity_var.get():.2f}",
        }
        _write_env(values)
        messagebox.showinfo(
            "Settings saved",
            "Settings saved to .env.\nSome changes will take effect after restarting.",
        )
        if self._win:
            self._win.destroy()

    def _clear_cache(self) -> None:
        cache_mod.clear()
        messagebox.showinfo("Cache cleared", "Translation cache has been cleared.")

    def _clear_history(self) -> None:
        if messagebox.askyesno("Clear history", "Delete all search history?"):
            history_mod.clear()
            messagebox.showinfo("History cleared", "Search history has been deleted.")

    def _export_history(self) -> None:
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            title="Export history",
        )
        if path:
            csv_data = history_mod.export_csv()
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(csv_data)
            messagebox.showinfo("Exported", f"History exported to:\n{path}")
