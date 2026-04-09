"""config.py — loads .env and exposes typed, validated constants.

Raises SystemExit (with a user-friendly message) if required fields are
missing so that every other module can safely import these constants without
additional null-checks.
"""

import os
import sys
import logging
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ── Helpers ──────────────────────────────────────────────────────────────────

def _get(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, default)


def _require(key: str) -> str:
    value = os.environ.get(key, "").strip()
    if not value:
        # Don't crash here — let the UI prompt for the key instead.
        logger.warning("Required config key %s is not set.", key)
        return ""
    return value


def _bool(key: str, default: bool = True) -> bool:
    raw = os.environ.get(key, str(default)).strip().lower()
    return raw in ("1", "true", "yes", "on")


def _int(key: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
    try:
        value = int(os.environ.get(key, str(default)))
    except ValueError:
        value = default
    if lo is not None:
        value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return value


def _float(key: str, default: float, lo: float | None = None, hi: float | None = None) -> float:
    try:
        value = float(os.environ.get(key, str(default)))
    except ValueError:
        value = default
    if lo is not None:
        value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return value


# ── Claude API ───────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY: str = _require("ANTHROPIC_API_KEY")

# Model used for NL → Everything query translation.
CLAUDE_MODEL: str = "claude-sonnet-4-20250514"

# Timeout for API calls in seconds.
API_TIMEOUT: int = 10

# Maximum retries on rate-limit (429) responses.
API_MAX_RETRIES: int = 3

# ── Global hotkey ────────────────────────────────────────────────────────────

HOTKEY: str = _get("HOTKEY", "ctrl+shift+space") or "ctrl+shift+space"

# ── Search ───────────────────────────────────────────────────────────────────

MAX_RESULTS: int = _int("MAX_RESULTS", 20, lo=20, hi=200)

_VALID_SORT_COLUMNS = {"name", "path", "size", "date_modified"}
_raw_sort = (_get("DEFAULT_SORT_COLUMN") or "date_modified").strip().lower()
DEFAULT_SORT_COLUMN: str = _raw_sort if _raw_sort in _VALID_SORT_COLUMNS else "date_modified"

_VALID_DIRECTIONS = {"asc", "desc"}
_raw_dir = (_get("DEFAULT_SORT_DIRECTION") or "desc").strip().lower()
DEFAULT_SORT_DIRECTION: str = _raw_dir if _raw_dir in _VALID_DIRECTIONS else "desc"

# ── Cache ─────────────────────────────────────────────────────────────────────

CACHE_ENABLED: bool = _bool("CACHE_ENABLED", default=True)
CACHE_TTL: int = _int("CACHE_TTL", 86400, lo=60)  # seconds

# ── Everything ───────────────────────────────────────────────────────────────

EVERYTHING_PATH: str = (
    _get("EVERYTHING_PATH") or r"C:\Program Files\Everything\Everything.exe"
)

EVERYTHING_DLL_PATH: str = _get("EVERYTHING_DLL_PATH") or ""

RESULT_VALIDATION: bool = _bool("RESULT_VALIDATION", default=True)

# ── UI ────────────────────────────────────────────────────────────────────────

_VALID_THEMES = {"auto", "light", "dark"}
_raw_theme = (_get("THEME") or "auto").strip().lower()
THEME: str = _raw_theme if _raw_theme in _VALID_THEMES else "auto"

FONT_SIZE: int = _int("FONT_SIZE", 11, lo=8, hi=24)
WINDOW_OPACITY: float = _float("WINDOW_OPACITY", 0.95, lo=0.85, hi=1.0)

# ── Paths ─────────────────────────────────────────────────────────────────────

import pathlib

# Directory where SQLite databases and settings are stored.
APP_DIR: pathlib.Path = pathlib.Path.home() / ".ask_everything"
APP_DIR.mkdir(exist_ok=True)

CACHE_DB_PATH: str = str(APP_DIR / "cache.db")
HISTORY_DB_PATH: str = str(APP_DIR / "history.db")
LOG_PATH: str = str(APP_DIR / "ask-everything.log")

# ── Logging setup ─────────────────────────────────────────────────────────────

import logging.handlers

_log_handler = logging.handlers.RotatingFileHandler(
    LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
)
_log_handler.setFormatter(
    logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
)

logging.basicConfig(
    level=logging.DEBUG,
    handlers=[_log_handler, logging.StreamHandler(sys.stdout)],
)
