"""cache.py — in-memory LRU cache + SQLite-backed persistent cache.

Cache key: SHA-256 of the lowercased, stripped NL query string.
Entries that contain date-relative terms (e.g. "last week", "today") are stored
in the in-memory LRU only; they are never persisted to SQLite to avoid serving
stale date-relative results across days.
"""

import hashlib
import logging
import re
import sqlite3
import time
from collections import OrderedDict
from typing import Optional

import config

logger = logging.getLogger(__name__)

# ── Date-relative terms that must not be persisted ──────────────────────────

_DATE_RELATIVE_PATTERN = re.compile(
    r"\b(today|yesterday|this\s+week|last\s+week|this\s+month|last\s+month"
    r"|this\s+year|last\s+year|this\s+decade|recent|now)\b",
    re.IGNORECASE,
)


def _is_date_relative(nl_query: str) -> bool:
    return bool(_DATE_RELATIVE_PATTERN.search(nl_query))


def _cache_key(nl_query: str) -> str:
    normalised = nl_query.strip().lower()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


# ── SQLite helpers ────────────────────────────────────────────────────────────

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS cache (
    key         TEXT PRIMARY KEY,
    nl_query    TEXT NOT NULL,
    eq_query    TEXT NOT NULL,
    created_at  REAL NOT NULL,
    hit_count   INTEGER NOT NULL DEFAULT 0
);
"""


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(config.CACHE_DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute(_CREATE_TABLE)
    conn.commit()
    return conn


# ── LRU in-memory cache ───────────────────────────────────────────────────────

_LRU_CAPACITY = 100
_lru: OrderedDict[str, str] = OrderedDict()


def _lru_get(key: str) -> Optional[str]:
    if key in _lru:
        _lru.move_to_end(key)
        return _lru[key]
    return None


def _lru_put(key: str, value: str) -> None:
    if key in _lru:
        _lru.move_to_end(key)
    _lru[key] = value
    if len(_lru) > _LRU_CAPACITY:
        _lru.popitem(last=False)


# ── Public API ────────────────────────────────────────────────────────────────

def get(nl_query: str) -> Optional[str]:
    """Return cached Everything query for *nl_query*, or None on miss."""
    if not config.CACHE_ENABLED:
        return None

    key = _cache_key(nl_query)

    # 1. Check in-memory LRU first.
    hit = _lru_get(key)
    if hit is not None:
        logger.debug("Cache LRU hit for key %s", key[:8])
        return hit

    # 2. Check SQLite (persistent).
    try:
        with _get_db() as conn:
            row = conn.execute(
                "SELECT eq_query, created_at FROM cache WHERE key = ?", (key,)
            ).fetchone()
            if row:
                age = time.time() - row["created_at"]
                if age <= config.CACHE_TTL:
                    conn.execute(
                        "UPDATE cache SET hit_count = hit_count + 1 WHERE key = ?",
                        (key,),
                    )
                    conn.commit()
                    _lru_put(key, row["eq_query"])
                    logger.debug("Cache DB hit for key %s (age %.0fs)", key[:8], age)
                    return row["eq_query"]
                else:
                    # Expired — remove it.
                    conn.execute("DELETE FROM cache WHERE key = ?", (key,))
                    conn.commit()
    except sqlite3.Error as exc:
        logger.error("Cache DB read error: %s", exc)

    return None


def put(nl_query: str, eq_query: str) -> None:
    """Store *eq_query* as the translation of *nl_query*."""
    if not config.CACHE_ENABLED:
        return

    key = _cache_key(nl_query)
    _lru_put(key, eq_query)

    if _is_date_relative(nl_query):
        logger.debug("Skipping DB persistence for date-relative query: %s", nl_query[:40])
        return

    try:
        with _get_db() as conn:
            conn.execute(
                """
                INSERT INTO cache (key, nl_query, eq_query, created_at, hit_count)
                VALUES (?, ?, ?, ?, 0)
                ON CONFLICT(key) DO UPDATE SET
                    eq_query   = excluded.eq_query,
                    created_at = excluded.created_at,
                    hit_count  = cache.hit_count
                """,
                (key, nl_query.strip(), eq_query, time.time()),
            )
            conn.commit()
    except sqlite3.Error as exc:
        logger.error("Cache DB write error: %s", exc)


def clear() -> None:
    """Clear both in-memory LRU and SQLite cache."""
    _lru.clear()
    try:
        with _get_db() as conn:
            conn.execute("DELETE FROM cache")
            conn.commit()
        logger.info("Cache cleared.")
    except sqlite3.Error as exc:
        logger.error("Cache DB clear error: %s", exc)


def clear_expired() -> None:
    """Remove SQLite entries whose TTL has elapsed."""
    try:
        cutoff = time.time() - config.CACHE_TTL
        with _get_db() as conn:
            conn.execute("DELETE FROM cache WHERE created_at < ?", (cutoff,))
            conn.commit()
        logger.debug("Expired cache entries pruned.")
    except sqlite3.Error as exc:
        logger.error("Cache DB prune error: %s", exc)
