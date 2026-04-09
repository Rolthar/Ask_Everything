"""main.py — Ask Everything entry point.

Startup sequence:
1. Configure logging (via config.py import).
2. Validate API key (warn if missing; prompt on first search).
3. Register global hotkey via `keyboard` library.
4. Start pystray tray icon in a background daemon thread.
5. Create OverlayWindow and run the tkinter event loop.

App lifecycle:
- Hotkey press → overlay.toggle()
- Tray "Quit"  → clean shutdown (keyboard unhook, tray stop, atexit)
- Ctrl+C       → same clean shutdown

Single-instance enforcement:
- A lock file (APP_DIR / "ask-everything.lock") is written on startup.
  If the file already exists and the owning PID is still alive, the new
  instance exits and signals the existing one to show its overlay.
"""

import atexit
import logging
import os
import signal
import sys
import threading

logger = logging.getLogger(__name__)

# ── Single-instance lock ──────────────────────────────────────────────────────

import config  # noqa: E402  (must be first to configure logging)

_LOCK_FILE = str(config.APP_DIR / "ask-everything.lock")


def _acquire_lock() -> bool:
    """Return True if this process owns the lock; False if another instance is running."""
    my_pid = os.getpid()
    if os.path.exists(_LOCK_FILE):
        try:
            with open(_LOCK_FILE) as f:
                existing_pid = int(f.read().strip())
            # Check if that PID is still running.
            os.kill(existing_pid, 0)
            logger.warning(
                "Another instance is running (PID %d). Exiting.", existing_pid
            )
            return False
        except (ValueError, ProcessLookupError, PermissionError):
            pass  # Stale lock — take it over.
    with open(_LOCK_FILE, "w") as f:
        f.write(str(my_pid))
    return True


def _release_lock() -> None:
    try:
        os.remove(_LOCK_FILE)
    except OSError:
        pass


# ── SDK cleanup ───────────────────────────────────────────────────────────────

def _sdk_cleanup() -> None:
    try:
        import search
        if search._ev is not None:
            search._ev.Everything_CleanUp()
            logger.info("Everything SDK cleaned up.")
    except Exception:
        pass


atexit.register(_sdk_cleanup)
atexit.register(_release_lock)


# ── Hotkey registration ───────────────────────────────────────────────────────

def _register_hotkey(overlay: "ui.OverlayWindow") -> None:
    try:
        import keyboard  # type: ignore[import]
        # keyboard callbacks run in a background thread; use _after for thread safety.
        keyboard.add_hotkey(
            config.HOTKEY,
            lambda: overlay._after(overlay.toggle),
        )
        logger.info("Hotkey registered: %s", config.HOTKEY)
    except Exception as exc:
        logger.error("Failed to register hotkey '%s': %s", config.HOTKEY, exc)
        # Prompt user to change the hotkey in settings on next open.


# ── Graceful shutdown ─────────────────────────────────────────────────────────

_tray: "tray.TrayIcon | None" = None


def _shutdown() -> None:
    logger.info("Shutting down Ask Everything.")
    if _tray:
        _tray.stop()
    try:
        import keyboard  # type: ignore[import]
        keyboard.unhook_all()
    except Exception:
        pass
    _sdk_cleanup()
    _release_lock()
    sys.exit(0)


def _handle_signal(sig, frame) -> None:
    _shutdown()


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    global _tray

    if not _acquire_lock():
        sys.exit(1)

    signal.signal(signal.SIGINT,  _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # Warn if API key is missing — don't crash; the UI will prompt on first use.
    if not config.ANTHROPIC_API_KEY:
        logger.warning(
            "ANTHROPIC_API_KEY is not set. "
            "Open Settings before making your first search."
        )

    # Prune expired cache entries on startup.
    import cache as cache_mod
    cache_mod.clear_expired()

    # Late imports (tkinter must be imported on the main thread).
    import ui
    import tray
    import history as history_mod
    import settings as settings_mod

    overlay = ui.OverlayWindow()

    def _rerun(nl_query: str) -> None:
        overlay.show()
        overlay._input_var.set(nl_query)
        overlay._run_search(nl_query)

    _tray = tray.TrayIcon(
        on_open=overlay.show,
        on_settings=lambda: settings_mod.SettingsWindow(
            parent=overlay._root
        ).show(),
        on_clear_cache=cache_mod.clear,
        on_quit=_shutdown,
        get_recent_searches=history_mod.recent_nl_queries,
        on_rerun=_rerun,
    )
    _tray.run_in_thread()

    _register_hotkey(overlay)

    logger.info("Ask Everything started. Press %s to open the overlay.", config.HOTKEY)

    # Run the tkinter event loop (blocks until window is destroyed).
    # _build() is called inside run(), so _root exists by the time we need it.
    overlay.run()


if __name__ == "__main__":
    main()
