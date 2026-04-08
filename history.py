"""history.py — SQLite store for past NL queries.

Schema:
    id            INTEGER PRIMARY KEY AUTOINCREMENT
    nl_query      TEXT    — original natural language query
    eq_query      TEXT    — translated Everything query
    timestamp     REAL    — Unix timestamp of the search
    result_count  INTEGER — number of results returned (-1 if unknown)
    duration_ms   INTEGER — total wall-clock time in ms (-1 if unknown)

At most 500 entries are kept; oldest entries are pruned automatically.
"""

import csv
import io
import logging
import sqlite3
import time
from typing import Optional

import config

logger = logging.getLogger(__name__)

MAX_HISTORY = 500

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    nl_query     TEXT    NOT NULL,
    eq_query     TEXT    NOT NULL,
    timestamp    REAL    NOT NULL,
    result_count INTEGER NOT NULL DEFAULT -1,
    duration_ms  INTEGER NOT NULL DEFAULT -1
);
"""


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(config.HISTORY_DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute(_CREATE_TABLE)
    conn.commit()
    return conn


# ── Public API ────────────────────────────────────────────────────────────────

def add(nl_query: str, eq_query: str, result_count: int = -1, duration_ms: int = -1) -> None:
    """Append a new history entry and prune old ones."""
    try:
        with _get_db() as conn:
            conn.execute(
                "INSERT INTO history (nl_query, eq_query, timestamp, result_count, duration_ms)"
                " VALUES (?, ?, ?, ?, ?)",
                (nl_query.strip(), eq_query, time.time(), result_count, duration_ms),
            )
            # Keep only the most recent MAX_HISTORY rows.
            conn.execute(
                "DELETE FROM history WHERE id NOT IN "
                "(SELECT id FROM history ORDER BY timestamp DESC LIMIT ?)",
                (MAX_HISTORY,),
            )
            conn.commit()
    except sqlite3.Error as exc:
        logger.error("History DB write error: %s", exc)


def recent(limit: int = 500) -> list[dict]:
    """Return the most recent *limit* entries, newest first."""
    try:
        with _get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM history ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
    except sqlite3.Error as exc:
        logger.error("History DB read error: %s", exc)
        return []


def recent_nl_queries(limit: int = 5) -> list[str]:
    """Return the last *limit* unique NL query strings for the tray menu."""
    entries = recent(limit * 3)
    seen: set[str] = set()
    result: list[str] = []
    for entry in entries:
        q = entry["nl_query"]
        if q not in seen:
            seen.add(q)
            result.append(q)
        if len(result) >= limit:
            break
    return result


def delete(entry_id: int) -> None:
    """Delete a single history entry by id."""
    try:
        with _get_db() as conn:
            conn.execute("DELETE FROM history WHERE id = ?", (entry_id,))
            conn.commit()
    except sqlite3.Error as exc:
        logger.error("History DB delete error: %s", exc)


def clear() -> None:
    """Delete all history entries."""
    try:
        with _get_db() as conn:
            conn.execute("DELETE FROM history")
            conn.commit()
        logger.info("History cleared.")
    except sqlite3.Error as exc:
        logger.error("History DB clear error: %s", exc)


def export_csv() -> str:
    """Return history as a CSV string (headers + rows)."""
    rows = recent(MAX_HISTORY)
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=["id", "nl_query", "eq_query", "timestamp", "result_count", "duration_ms"],
    )
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()
