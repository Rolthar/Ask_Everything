"""tray.py — system tray icon using pystray + Pillow.

Menu structure:
  Open            → show overlay
  Recent searches → submenu of last 5 NL queries
  Settings        → open settings window
  Clear cache     → clear translation cache
  ──────────────
  Quit            → stop the app

Left-click toggles the overlay.
"""

import logging
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)

try:
    import pystray
    from pystray import MenuItem as Item, Menu
    from PIL import Image, ImageDraw
    _PYSTRAY_AVAILABLE = True
except ImportError:
    pystray = None  # type: ignore[assignment]
    _PYSTRAY_AVAILABLE = False
    logger.warning("pystray / Pillow not available — tray icon disabled.")


def _make_icon_image(size: int = 64) -> "Image.Image":
    """Generate a simple tray icon programmatically."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Draw a rounded rectangle background.
    draw.rounded_rectangle([2, 2, size - 2, size - 2], radius=12, fill="#0078d4")
    # Draw the letter "A" (Ask).
    draw.text((size // 4, size // 6), "A", fill="white")
    return img


class TrayIcon:
    def __init__(
        self,
        on_open: Callable,
        on_settings: Callable,
        on_clear_cache: Callable,
        on_quit: Callable,
        get_recent_searches: Callable[[], list[str]],
        on_rerun: Callable[[str], None],
    ):
        self._on_open = on_open
        self._on_settings = on_settings
        self._on_clear_cache = on_clear_cache
        self._on_quit = on_quit
        self._get_recent = get_recent_searches
        self._on_rerun = on_rerun
        self._icon: Optional["pystray.Icon"] = None

    def _build_menu(self) -> "Menu":
        recent = self._get_recent()
        recent_items = (
            [Item(q[:60], lambda _icon, _item, q=q: self._on_rerun(q)) for q in recent]
            if recent
            else [Item("(none)", lambda *_: None, enabled=False)]
        )

        return Menu(
            Item("Open", lambda *_: self._on_open()),
            Item("Recent searches", Menu(*recent_items)),
            Item("Settings", lambda *_: self._on_settings()),
            Item("Clear cache", lambda *_: self._on_clear_cache()),
            Menu.SEPARATOR,
            Item("Quit", lambda *_: self._on_quit()),
        )

    def run_in_thread(self) -> None:
        """Start the tray icon in a daemon thread (non-blocking)."""
        if not _PYSTRAY_AVAILABLE:
            logger.warning("Tray icon skipped — pystray not available.")
            return

        def _run():
            try:
                image = _make_icon_image()
                self._icon = pystray.Icon(
                    name="ask_everything",
                    icon=image,
                    title="Ask Everything",
                    menu=self._build_menu(),
                )
                # Rebuild menu on each click so recent searches are fresh.
                self._icon.menu = pystray.Menu(lambda: self._build_menu().items)
                self._icon.run()
            except Exception as exc:
                logger.error("Tray icon error: %s", exc)

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        logger.info("Tray icon started.")

    def stop(self) -> None:
        if self._icon:
            try:
                self._icon.stop()
            except Exception:
                pass
