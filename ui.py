"""ui.py — tkinter overlay window for Ask Everything.

The overlay is a borderless, always-on-top window that appears centred on the
active monitor when triggered by the global hotkey.

Layout (top → bottom):
  ┌─────────────────────────────────────────────┐
  │ [Input field]                    [500]       │
  │ Everything query: [translated query] [Copy]  │
  │ ─ filter bar: All | Files | Folders + chips ─│
  │ [Results list]                               │
  │ Showing X of Y results        [Load more]    │
  │ Status bar                                   │
  └─────────────────────────────────────────────┘
"""

import ctypes
import logging
import os
import subprocess
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Callable, Optional

import config
import cache as cache_mod
import history as history_mod
import search as search_mod
import translator as translator_mod

logger = logging.getLogger(__name__)

# ── DPI awareness ─────────────────────────────────────────────────────────────

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# ── Theme detection ───────────────────────────────────────────────────────────

def _detect_dark_mode() -> bool:
    """Return True if Windows is in dark mode (checks registry)."""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        winreg.CloseKey(key)
        return value == 0
    except Exception:
        return False


def _is_dark() -> bool:
    theme = config.THEME
    if theme == "dark":
        return True
    if theme == "light":
        return False
    return _detect_dark_mode()


# ── Colour palette ────────────────────────────────────────────────────────────

class _Palette:
    def __init__(self, dark: bool):
        if dark:
            self.bg         = "#1e1e1e"
            self.bg_input   = "#2d2d2d"
            self.bg_list    = "#252526"
            self.fg         = "#d4d4d4"
            self.fg_dim     = "#858585"
            self.accent     = "#569cd6"
            self.border     = "#3c3c3c"
            self.stale      = "#5a5a5a"
        else:
            self.bg         = "#f5f5f5"
            self.bg_input   = "#ffffff"
            self.bg_list    = "#ffffff"
            self.fg         = "#1e1e1e"
            self.fg_dim     = "#6b6b6b"
            self.accent     = "#0078d4"
            self.border     = "#d0d0d0"
            self.stale      = "#b0b0b0"


# ── Main overlay window ───────────────────────────────────────────────────────

class OverlayWindow:
    PAGE_SIZE = 20  # results per page

    def __init__(self, on_tray_rerun: Optional[Callable[[str], None]] = None):
        self._on_tray_rerun = on_tray_rerun
        self._dark = _is_dark()
        self._pal = _Palette(self._dark)
        self._root: Optional[tk.Tk] = None
        self._visible = False
        self._history_index = -1
        self._history_list: list[str] = []
        self._results: list[search_mod.SearchResult] = []
        self._total_count = 0
        self._current_page = 0
        self._active_query = ""
        self._ext_filter: Optional[str] = None
        self._type_filter = "all"  # all | file | folder
        self._translation_time_ms = 0
        self._search_time_ms = 0
        self._suppress_focus_out = False

    # ── Build UI ──────────────────────────────────────────────────────────────

    def _build(self) -> None:
        root = tk.Tk()
        self._root = root

        root.title("Ask Everything")
        root.overrideredirect(True)   # borderless
        root.attributes("-topmost", True)
        root.attributes("-alpha", config.WINDOW_OPACITY)
        root.configure(bg=self._pal.bg)

        fnt = (
            "Segoe UI",
            config.FONT_SIZE,
        )
        fnt_small = ("Segoe UI", max(8, config.FONT_SIZE - 2))
        fnt_mono  = ("Consolas", config.FONT_SIZE)

        pad = dict(padx=8, pady=4)

        # ── Top frame (input + char counter) ──────────────────────────────
        top_frame = tk.Frame(root, bg=self._pal.bg)
        top_frame.pack(fill=tk.X, padx=10, pady=(10, 0))

        self._input_var = tk.StringVar()
        self._input_var.trace_add("write", self._on_input_change)

        self._input_entry = tk.Entry(
            top_frame,
            textvariable=self._input_var,
            font=fnt,
            bg=self._pal.bg_input,
            fg=self._pal.fg,
            insertbackground=self._pal.fg,
            relief=tk.FLAT,
            bd=4,
        )
        self._input_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._input_entry.bind("<Return>", self._on_submit)
        self._input_entry.bind("<Escape>", lambda _e: self.hide())
        self._input_entry.bind("<Up>",   self._history_up)
        self._input_entry.bind("<Down>", self._history_down)
        self._input_entry.focus_set()

        self._char_label = tk.Label(
            top_frame, text="500", font=fnt_small,
            bg=self._pal.bg, fg=self._pal.fg_dim, width=4, anchor=tk.E,
        )
        self._char_label.pack(side=tk.RIGHT, padx=(4, 0))

        tk.Button(
            top_frame, text="✕", font=fnt_small,
            bg=self._pal.bg, fg=self._pal.fg_dim,
            relief=tk.FLAT, bd=0, padx=4,
            command=self._quit,
        ).pack(side=tk.RIGHT, padx=(8, 0))

        # ── Translated query row ───────────────────────────────────────────
        eq_frame = tk.Frame(root, bg=self._pal.bg)
        eq_frame.pack(fill=tk.X, padx=10, pady=(6, 0))

        tk.Label(
            eq_frame, text="Everything query:", font=fnt_small,
            bg=self._pal.bg, fg=self._pal.fg_dim,
        ).pack(side=tk.LEFT)

        self._eq_var = tk.StringVar(value="")
        self._eq_entry = tk.Entry(
            eq_frame,
            textvariable=self._eq_var,
            font=fnt_mono,
            bg=self._pal.bg_input,
            fg=self._pal.accent,
            insertbackground=self._pal.accent,
            relief=tk.FLAT,
            bd=2,
            state="readonly",
        )
        self._eq_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 6))

        self._copy_btn = tk.Button(
            eq_frame, text="Copy", font=fnt_small,
            bg=self._pal.bg_input, fg=self._pal.fg,
            relief=tk.FLAT, bd=1, padx=6,
            command=self._copy_query,
        )
        self._copy_btn.pack(side=tk.RIGHT)

        # ── Filter bar ─────────────────────────────────────────────────────
        self._filter_frame = tk.Frame(root, bg=self._pal.bg)
        self._filter_frame.pack(fill=tk.X, padx=10, pady=(6, 0))

        for label, value in [("All", "all"), ("Files", "file"), ("Folders", "folder")]:
            btn = tk.Button(
                self._filter_frame, text=label, font=fnt_small,
                bg=self._pal.bg_input, fg=self._pal.fg,
                relief=tk.FLAT, bd=1, padx=8,
                command=lambda v=value: self._set_type_filter(v),
            )
            btn.pack(side=tk.LEFT, padx=(0, 4))

        self._chip_frame = tk.Frame(self._filter_frame, bg=self._pal.bg)
        self._chip_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # ── Results list ───────────────────────────────────────────────────
        list_frame = tk.Frame(root, bg=self._pal.bg)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(6, 0))

        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        cols = ("icon", "filename", "path", "size", "modified")
        self._tree = ttk.Treeview(
            list_frame,
            columns=cols,
            show="headings",
            yscrollcommand=scrollbar.set,
            height=14,
        )
        scrollbar.config(command=self._tree.yview)

        col_defs = [
            ("icon",     "T",        40,   False),
            ("filename", "Name",    240,   True),
            ("path",     "Path",    320,   True),
            ("size",     "Size",     80,   False),
            ("modified", "Modified",130,   False),
        ]
        for cid, heading, width, stretch in col_defs:
            self._tree.heading(cid, text=heading,
                               command=lambda c=cid: self._sort_by(c))
            self._tree.column(cid, width=width,
                              stretch=tk.YES if stretch else tk.NO,
                              anchor=tk.W)

        self._tree.tag_configure("stale", foreground=self._pal.stale)
        self._tree.pack(fill=tk.BOTH, expand=True)
        self._tree.bind("<Double-1>",    self._on_double_click)
        self._tree.bind("<Return>",      self._on_open_selected)
        self._tree.bind("<Button-3>",    self._on_right_click)

        # ── Pagination row ─────────────────────────────────────────────────
        page_frame = tk.Frame(root, bg=self._pal.bg)
        page_frame.pack(fill=tk.X, padx=10, pady=(4, 0))

        self._count_label = tk.Label(
            page_frame, text="", font=fnt_small,
            bg=self._pal.bg, fg=self._pal.fg_dim,
        )
        self._count_label.pack(side=tk.LEFT)

        self._load_more_btn = tk.Button(
            page_frame, text="Load more", font=fnt_small,
            bg=self._pal.bg_input, fg=self._pal.fg,
            relief=tk.FLAT, bd=1, padx=8,
            command=self._load_more,
            state=tk.DISABLED,
        )
        self._load_more_btn.pack(side=tk.RIGHT)

        # ── Status bar ─────────────────────────────────────────────────────
        self._status_var = tk.StringVar(value="Ready")
        status_bar = tk.Label(
            root,
            textvariable=self._status_var,
            font=fnt_small,
            bg=self._pal.border,
            fg=self._pal.fg_dim,
            anchor=tk.W,
            padx=8,
            pady=3,
        )
        status_bar.pack(fill=tk.X, side=tk.BOTTOM)

        # ── Window sizing + positioning ────────────────────────────────────
        root.update_idletasks()
        self._centre_on_active_monitor()

        # Dismiss on click-outside.
        root.bind("<FocusOut>", self._on_focus_out)
        root.bind("<FocusIn>",  lambda _e: root.attributes("-topmost", True))

        # Drag to reposition.
        root.bind("<Button-1>",  self._on_drag_start)
        root.bind("<B1-Motion>", self._on_drag_motion)

        self._drag_x = 0
        self._drag_y = 0

    # ── Geometry helpers ──────────────────────────────────────────────────────

    def _centre_on_active_monitor(self) -> None:
        """Position the window in the centre of the monitor with the foreground window."""
        root = self._root
        assert root is not None
        w, h = 860, 560
        root.geometry(f"{w}x{h}")
        root.update_idletasks()

        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            monitor = ctypes.windll.shcore.MonitorFromWindow(hwnd, 2)

            class MONITORINFO(ctypes.Structure):
                _fields_ = [
                    ("cbSize",    ctypes.c_ulong),
                    ("rcMonitor", ctypes.c_long * 4),
                    ("rcWork",    ctypes.c_long * 4),
                    ("dwFlags",   ctypes.c_ulong),
                ]

            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(MONITORINFO)
            ctypes.windll.user32.GetMonitorInfoW(monitor, ctypes.byref(info))
            ml, mt, mr, mb = info.rcWork
            x = ml + (mr - ml - w) // 2
            y = mt + (mb - mt - h) // 2
        except Exception:
            sw = root.winfo_screenwidth()
            sh = root.winfo_screenheight()
            x = (sw - w) // 2
            y = (sh - h) // 2

        root.geometry(f"{w}x{h}+{x}+{y}")

    # ── Drag handlers ─────────────────────────────────────────────────────────

    def _on_drag_start(self, event: tk.Event) -> None:
        self._drag_x = event.x_root - (self._root.winfo_x() if self._root else 0)
        self._drag_y = event.y_root - (self._root.winfo_y() if self._root else 0)

    def _on_drag_motion(self, event: tk.Event) -> None:
        if self._root:
            x = event.x_root - self._drag_x
            y = event.y_root - self._drag_y
            self._root.geometry(f"+{x}+{y}")

    # ── Focus / visibility ────────────────────────────────────────────────────

    def _on_focus_out(self, event: tk.Event) -> None:
        if self._root and event.widget == self._root and not self._suppress_focus_out:
            self.hide()

    def show(self) -> None:
        if self._root is None:
            self._build()
        assert self._root is not None
        self._root.deiconify()
        self._root.lift()
        self._root.attributes("-topmost", True)
        self._root.focus_force()
        self._input_entry.focus_set()
        self._visible = True
        # Refresh history for up/down cycling.
        self._history_list = [e["nl_query"] for e in history_mod.recent(50)]
        self._history_index = -1

    def hide(self) -> None:
        if self._root:
            self._root.withdraw()
        self._visible = False

    def toggle(self) -> None:
        if self._visible:
            self.hide()
        else:
            self.show()

    def _quit(self) -> None:
        import main as main_mod
        main_mod._shutdown()

    def run(self) -> None:
        """Build the window and start the tkinter event loop."""
        self._build()
        assert self._root is not None
        # tkinter's mainloop() blocks Python signal delivery on Windows.
        # This periodic no-op yields back to Python every 200 ms so Ctrl+C works.
        def _poll_signals():
            self._root.after(200, _poll_signals)  # type: ignore[union-attr]
        self._root.after(200, _poll_signals)
        self._root.mainloop()

    # ── Input helpers ─────────────────────────────────────────────────────────

    def _on_input_change(self, *_args) -> None:
        text = self._input_var.get()
        remaining = 500 - len(text)
        self._char_label.config(
            text=str(remaining),
            fg=self._pal.accent if remaining < 50 else self._pal.fg_dim,
        )

    def _history_up(self, _event: tk.Event) -> None:
        if not self._history_list:
            return
        self._history_index = min(
            self._history_index + 1, len(self._history_list) - 1
        )
        self._input_var.set(self._history_list[self._history_index])

    def _history_down(self, _event: tk.Event) -> None:
        if self._history_index <= 0:
            self._history_index = -1
            self._input_var.set("")
            return
        self._history_index -= 1
        self._input_var.set(self._history_list[self._history_index])

    # ── Submit / search flow ──────────────────────────────────────────────────

    def _on_submit(self, _event: tk.Event) -> None:
        nl_query = self._input_var.get().strip()
        if not nl_query:
            return
        self._run_search(nl_query)

    def _run_search(self, nl_query: str) -> None:
        """Kick off the translate → search pipeline in a background thread."""
        self._set_status("Translating…")
        self._input_entry.config(state=tk.DISABLED)
        self._results = []
        self._current_page = 0

        def _worker():
            t_start = time.perf_counter()

            # 1. Check cache.
            eq_query = cache_mod.get(nl_query)
            cached = eq_query is not None

            # 2. Translate if not cached.
            if eq_query is None:
                try:
                    eq_query = translator_mod.translate(nl_query)
                except translator_mod.ConfigError:
                    self._after(self._show_key_error)
                    return
                except translator_mod.TranslationTimeout:
                    self._after(lambda: self._set_status_and_enable_input(
                        "Translation timed out — try again."
                    ))
                    return
                except translator_mod.TranslationError as exc:
                    msg = str(exc)
                    self._after(lambda: self._set_status_and_enable_input(msg))
                    return

            translation_ms = int((time.perf_counter() - t_start) * 1000)

            # 3. Handle UNCLEAR / BROAD.
            if eq_query == translator_mod.UNCLEAR:
                self._after(lambda: self._show_unclear())
                return
            if eq_query == translator_mod.BROAD_QUERY:
                self._after(lambda: self._confirm_broad(nl_query))
                return

            # 4. Store in cache.
            if not cached:
                cache_mod.put(nl_query, eq_query)

            # 5. Execute search.
            t_search = time.perf_counter()

            # Apply type filter.
            final_query = self._apply_type_filter(eq_query)

            resp = search_mod.search(final_query, max_results=config.MAX_RESULTS)
            search_ms = int((time.perf_counter() - t_search) * 1000)

            # 6. Save to history.
            history_mod.add(
                nl_query, eq_query,
                result_count=resp.total_count,
                duration_ms=int((time.perf_counter() - t_start) * 1000),
            )

            # 7. Update UI on main thread.
            def _update():
                self._active_query = eq_query
                self._translation_time_ms = translation_ms
                self._search_time_ms = search_ms
                self._display_results(resp, eq_query, nl_query, cached)
                self._input_entry.config(state=tk.NORMAL)

            self._after(_update)

        threading.Thread(target=_worker, daemon=True).start()

    def _after(self, fn: Callable) -> None:
        if self._root:
            self._root.after(0, fn)

    def _show_unclear(self) -> None:
        self._eq_var.set("UNCLEAR")
        self._set_status("Couldn't translate — try rephrasing.")
        self._input_entry.config(state=tk.NORMAL)

    def _confirm_broad(self, nl_query: str) -> None:
        """Ask user to confirm a query that would match all files."""
        self._input_entry.config(state=tk.NORMAL)
        self._suppress_focus_out = True
        result = messagebox.askyesno(
            "Broad query",
            "This query will match all files — are you sure you want to continue?",
        )
        self._suppress_focus_out = False
        if result:
            # Re-run with the translated query directly.
            self._run_search_with_query(nl_query, "*")

    def _run_search_with_query(self, nl_query: str, eq_query: str) -> None:
        """Run a search with a pre-known eq_query (skips translation)."""
        def _worker():
            resp = search_mod.search(eq_query, max_results=config.MAX_RESULTS)
            history_mod.add(nl_query, eq_query, result_count=resp.total_count)

            def _update():
                self._active_query = eq_query
                self._display_results(resp, eq_query, nl_query, False)
                self._input_entry.config(state=tk.NORMAL)

            self._after(_update)

        self._input_entry.config(state=tk.DISABLED)
        threading.Thread(target=_worker, daemon=True).start()

    # ── Results display ───────────────────────────────────────────────────────

    def _display_results(
        self,
        resp: search_mod.SearchResponse,
        eq_query: str,
        nl_query: str,
        cached: bool,
    ) -> None:
        if resp.error:
            self._handle_search_error(resp.error)
            return

        self._eq_var.set(eq_query)
        self._results = resp.results
        self._total_count = resp.total_count

        # Clear tree.
        for row in self._tree.get_children():
            self._tree.delete(row)

        if not resp.results:
            self._set_status(
                f"No results — query was: {eq_query}"
                + ("  (cached)" if cached else "")
            )
            self._count_label.config(text="No results")
            self._load_more_btn.config(state=tk.DISABLED)
            self._update_ext_chips([])
            return

        self._populate_tree(resp.results)
        self._update_count_label()
        self._update_ext_chips(resp.results)

        more = resp.total_count > len(resp.results)
        self._load_more_btn.config(state=tk.NORMAL if more else tk.DISABLED)

        cache_note = " (cached)" if cached else ""
        self._set_status(
            f"Translated in {self._translation_time_ms}ms  |  "
            f"Searched in {self._search_time_ms}ms  |  "
            f"{resp.total_count:,} total results"
            + cache_note
        )

    def _populate_tree(self, results: list[search_mod.SearchResult]) -> None:
        for r in results:
            icon   = "📁" if r.is_folder else "📄"
            size   = self._fmt_size(r.size)
            mdate  = self._fmt_date(r.date_modified)
            path   = self._truncate_path(r.full_path)
            tags   = ("stale",) if r.exists is False else ()
            try:
                self._tree.insert(
                    "", tk.END,
                    iid=r.full_path,
                    values=(icon, r.filename, path, size, mdate),
                    tags=tags,
                )
            except tk.TclError:
                logger.debug("Skipping duplicate result path: %s", r.full_path)

    def _update_count_label(self) -> None:
        shown = len(self._tree.get_children())
        total = self._total_count
        self._count_label.config(
            text=f"Showing {shown:,} of {total:,} results"
        )

    def _load_more(self) -> None:
        """Append the next page of results."""
        self._current_page += 1
        offset = self._current_page * config.MAX_RESULTS

        def _worker():
            try:
                import ctypes
                ev = search_mod._ev
                if ev is None:
                    raise RuntimeError("Everything SDK DLL not loaded.")
                ev.Everything_SetSearchW(self._apply_type_filter(self._active_query))
                ev.Everything_SetMax(config.MAX_RESULTS)
                ev.Everything_SetOffset(offset)
                ev.Everything_SetRequestFlags(
                    search_mod.EVERYTHING_REQUEST_FILE_NAME
                    | search_mod.EVERYTHING_REQUEST_PATH
                    | search_mod.EVERYTHING_REQUEST_SIZE
                    | search_mod.EVERYTHING_REQUEST_DATE_MODIFIED
                )
                ev.Everything_QueryW(1)
                num = ev.Everything_GetNumResults()
                new_results: list[search_mod.SearchResult] = []
                for i in range(num):
                    filename  = ev.Everything_GetResultFileNameW(i) or ""
                    path_str  = ev.Everything_GetResultPathW(i) or ""
                    full_path = os.path.join(path_str, filename) if path_str else filename
                    raw_size  = ctypes.c_ulonglong(0)
                    size      = int(raw_size.value) if ev.Everything_GetResultSize(i, ctypes.byref(raw_size)) else -1
                    raw_date  = ctypes.c_ulonglong(0)
                    if ev.Everything_GetResultDateModified(i, ctypes.byref(raw_date)) and raw_date.value > 0:
                        dm = (raw_date.value - 116444736000000000) / 10_000_000
                    else:
                        dm = 0.0
                    new_results.append(search_mod.SearchResult(
                        filename=filename,
                        full_path=full_path,
                        size=size,
                        date_modified=dm,
                        is_folder=bool(ev.Everything_IsFolderResult(i)),
                    ))
                search_mod.validate_results(new_results)
                self._results.extend(new_results)

                def _update():
                    self._populate_tree(new_results)
                    self._update_count_label()
                    total_shown = len(self._tree.get_children())
                    if total_shown >= self._total_count:
                        self._load_more_btn.config(state=tk.DISABLED)

                self._after(_update)
            except Exception as exc:
                self._after(lambda: self._set_status(f"Load more failed: {exc}"))

        threading.Thread(target=_worker, daemon=True).start()

    # ── Filter bar ────────────────────────────────────────────────────────────

    def _apply_type_filter(self, query: str) -> str:
        if self._type_filter == "file":
            return f"file: {query}"
        if self._type_filter == "folder":
            return f"folder: {query}"
        return query

    def _set_type_filter(self, value: str) -> None:
        self._type_filter = value
        if self._active_query:
            self._run_search_with_query(
                self._input_var.get().strip(), self._active_query
            )

    def _update_ext_chips(self, results: list[search_mod.SearchResult]) -> None:
        """Rebuild the extension chip row from current results."""
        for w in self._chip_frame.winfo_children():
            w.destroy()

        if not results:
            return

        from collections import Counter
        exts = Counter(
            os.path.splitext(r.filename)[1].lstrip(".").lower()
            for r in results
            if not r.is_folder and os.path.splitext(r.filename)[1]
        )
        for ext, count in exts.most_common(8):
            btn = tk.Button(
                self._chip_frame,
                text=f"{ext} ×{count}",
                font=("Segoe UI", max(8, config.FONT_SIZE - 2)),
                bg=self._pal.bg_input,
                fg=self._pal.fg,
                relief=tk.FLAT,
                bd=1,
                padx=6,
                command=lambda e=ext: self._filter_by_ext(e),
            )
            btn.pack(side=tk.LEFT, padx=(0, 3))

    def _filter_by_ext(self, ext: str) -> None:
        new_query = f"{self._active_query} ext:{ext}".strip()
        self._eq_var.set(new_query)
        self._run_search_with_query(self._input_var.get().strip(), new_query)

    # ── Sorting ───────────────────────────────────────────────────────────────

    def _sort_by(self, column: str) -> None:
        col_map = {"filename": "filename", "path": "full_path",
                   "size": "size", "modified": "date_modified"}
        attr = col_map.get(column)
        if not attr:
            return
        self._results.sort(
            key=lambda r: getattr(r, attr, ""),
            reverse=not getattr(self, f"_sort_asc_{column}", False),
        )
        setattr(self, f"_sort_asc_{column}",
                not getattr(self, f"_sort_asc_{column}", False))
        for row in self._tree.get_children():
            self._tree.delete(row)
        self._populate_tree(self._results)

    # ── Context menu ──────────────────────────────────────────────────────────

    def _on_right_click(self, event: tk.Event) -> None:
        item = self._tree.identify_row(event.y)
        if not item:
            return
        self._tree.selection_set(item)
        path = item  # iid == full_path

        menu = tk.Menu(self._root, tearoff=0)
        menu.add_command(label="Open file",
                         command=lambda: self._open_path(path))
        menu.add_command(label="Open containing folder",
                         command=lambda: self._open_folder(path))
        menu.add_separator()
        menu.add_command(label="Copy full path",
                         command=lambda: self._copy_to_clipboard(path))
        menu.add_command(label="Copy filename only",
                         command=lambda: self._copy_to_clipboard(os.path.basename(path)))
        menu.add_command(
            label="Copy as markdown link",
            command=lambda: self._copy_to_clipboard(
                f"[{os.path.basename(path)}]({path})"
            ),
        )
        menu.tk_popup(event.x_root, event.y_root)

    def _on_double_click(self, _event: tk.Event) -> None:
        self._on_open_selected(None)

    def _on_open_selected(self, _event) -> None:
        sel = self._tree.selection()
        if sel:
            self._open_path(sel[0])

    def _open_path(self, path: str) -> None:
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except AttributeError:
            try:
                subprocess.Popen(["xdg-open", path])
            except Exception as exc:
                self._set_status(f"Could not open: {exc}")
        except PermissionError:
            self._set_status(f"Access denied: {path}")
        except Exception as exc:
            self._set_status(f"Could not open: {exc}")

    def _open_folder(self, path: str) -> None:
        folder = os.path.dirname(path) if not os.path.isdir(path) else path
        self._open_path(folder)

    def _copy_query(self) -> None:
        self._copy_to_clipboard(self._eq_var.get())

    def _copy_to_clipboard(self, text: str) -> None:
        if self._root:
            self._root.clipboard_clear()
            self._root.clipboard_append(text)
            self._set_status(f"Copied: {text[:60]}")

    # ── Error handling ────────────────────────────────────────────────────────

    def _handle_search_error(self, error: str) -> None:
        if "not running" in error.lower() or "ipc" in error.lower():
            self._set_status("Everything is not running.")
            self._suppress_focus_out = True
            result = messagebox.askyesno(
                "Everything not running",
                "Everything is not running. Launch it now?",
            )
            self._suppress_focus_out = False
            if result:
                try:
                    search_mod.launch_everything()
                    self._set_status("Launching Everything… please wait.")
                    threading.Timer(2.0, self._retry_connection).start()
                except FileNotFoundError:
                    self._set_status("Everything.exe not found. Check settings.")
        else:
            self._set_status(f"Search error: {error}")
        self._input_entry.config(state=tk.NORMAL)

    def _retry_connection(self) -> None:
        if search_mod.is_connection_ok():
            self._after(lambda: self._set_status("Everything is running. Try your search again."))
        else:
            self._after(lambda: self._set_status("Could not connect to Everything. Please launch it manually."))

    # ── Formatting helpers ────────────────────────────────────────────────────

    @staticmethod
    def _fmt_size(size: int) -> str:
        if size < 0:
            return "—"
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024:
                return f"{size:.0f} {unit}"
            size /= 1024
        return f"{size:.1f} PB"

    @staticmethod
    def _fmt_date(ts: float) -> str:
        if ts <= 0:
            return "—"
        import datetime
        dt = datetime.datetime.fromtimestamp(ts)
        return dt.strftime("%Y-%m-%d %H:%M")

    @staticmethod
    def _truncate_path(path: str, max_len: int = 50) -> str:
        if len(path) <= max_len:
            return path
        keep = (max_len - 3) // 2
        return path[:keep] + "…" + path[-keep:]

    # ── Status bar ────────────────────────────────────────────────────────────

    def _set_status(self, msg: str) -> None:
        if self._root:
            self._status_var.set(msg)

    def _set_status_and_enable_input(self, msg: str) -> None:
        self._set_status(msg)
        self._input_entry.config(state=tk.NORMAL)

    def _show_key_error(self) -> None:
        self._suppress_focus_out = True
        messagebox.showerror(
            "API Key Missing",
            "Anthropic API key is not configured.\n"
            "Please add ANTHROPIC_API_KEY to your .env file and restart.",
        )
        self._suppress_focus_out = False
        self._input_entry.config(state=tk.NORMAL)
