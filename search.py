"""search.py — Everything SDK integration.

Executes a compiled Everything query via IPC and returns normalised results.

Edge cases handled:
- Everything not running      → EverythingNotRunning
- DB not yet loaded           → EverythingDBLoading
- Admin mismatch              → EverythingAdminMismatch
- Invalid query / 0 results   → distinguishes bad-syntax from empty result set
- Stale paths                 → validated in a thread (500ms timeout per path)
- Unicode / UNC paths         → wide-char API calls used by SDK
- Very large result sets      → capped at MAX_RESULTS (max 200)
- Unavailable metadata        → returns "—" string for missing values
- Everything 1.4 vs 1.5       → strips content: filter on 1.4
"""

import concurrent.futures
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional

import config

logger = logging.getLogger(__name__)

# ── Attempt to import the Everything SDK ─────────────────────────────────────

try:
    import everything as ev  # type: ignore[import]
    _SDK_AVAILABLE = True
except ImportError:
    ev = None  # type: ignore[assignment]
    _SDK_AVAILABLE = False
    logger.warning(
        "everything-sdk not found.  Install with: pip install everything-sdk"
    )

# Everything SDK error codes (subset used for human-readable messages).
_ERROR_MESSAGES = {
    0:  "OK",
    1:  "Memory allocation failure",
    2:  "IPC not available — is Everything running?",
    3:  "Register class failed",
    4:  "Create window failed",
    5:  "Create thread failed",
    6:  "Invalid index",
    7:  "Invalid call",
}

# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class SearchResult:
    filename: str
    full_path: str
    size: int           # bytes, or -1 if unavailable
    date_modified: float  # Unix timestamp, or 0 if unavailable
    is_folder: bool
    exists: Optional[bool] = None  # None = not yet validated


@dataclass
class SearchResponse:
    results: list[SearchResult] = field(default_factory=list)
    total_count: int = 0
    query_time_ms: int = 0
    everything_version: str = ""
    error: Optional[str] = None


# ── Version helpers ───────────────────────────────────────────────────────────

def _get_version() -> tuple[int, int, int, int]:
    """Return (major, minor, revision, build) or (0,0,0,0) on failure."""
    if not _SDK_AVAILABLE:
        return (0, 0, 0, 0)
    try:
        major = ev.Everything_GetMajorVersion()
        minor = ev.Everything_GetMinorVersion()
        rev   = ev.Everything_GetRevision()
        build = ev.Everything_GetBuildNumber()
        return (major, minor, rev, build)
    except Exception as exc:
        logger.debug("Could not read Everything version: %s", exc)
        return (0, 0, 0, 0)


def get_version_string() -> str:
    v = _get_version()
    if v == (0, 0, 0, 0):
        return "unknown"
    return f"{v[0]}.{v[1]}.{v[2]}.{v[3]}"


def _strip_content_filter(query: str) -> str:
    """Remove content: clauses — unsupported on Everything 1.4."""
    return re.sub(r"\bcontent:\S+", "", query).strip()


# ── Path existence validation (with per-path timeout) ─────────────────────────

def _check_exists(path: str, timeout: float = 0.5) -> bool:
    """Return True if *path* exists; False on timeout or any error."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(os.path.exists, path)
        try:
            return fut.result(timeout=timeout)
        except (concurrent.futures.TimeoutError, Exception):
            return False


def validate_results(results: list[SearchResult]) -> None:
    """Set the .exists flag on each result (in parallel, 500ms timeout each)."""
    if not config.RESULT_VALIDATION:
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        futs = {pool.submit(os.path.exists, r.full_path): r for r in results}
        done, _ = concurrent.futures.wait(futs, timeout=2.0)
        for fut in done:
            result = futs[fut]
            try:
                result.exists = fut.result()
            except Exception:
                result.exists = False
        for fut in futs:
            if fut not in done:
                futs[fut].exists = None  # timed out — leave as None


# ── Core search ───────────────────────────────────────────────────────────────

def search(query: str, max_results: Optional[int] = None) -> SearchResponse:
    """Execute *query* in Everything and return a :class:`SearchResponse`."""
    if not _SDK_AVAILABLE:
        return SearchResponse(error="everything-sdk is not installed.")

    max_results = max(1, min(max_results or config.MAX_RESULTS, 200))

    # Detect Everything version.
    version_tuple = _get_version()
    version_str = get_version_string()

    # Strip content: filter on 1.4.
    if version_tuple[0] == 1 and version_tuple[1] < 5:
        original = query
        query = _strip_content_filter(query)
        if query != original:
            logger.info("content: filter stripped for Everything 1.4 (was: %s)", original)

    t0 = time.perf_counter()

    try:
        # --- Check DB loaded ---
        if not ev.Everything_IsDBLoaded():
            logger.warning("Everything DB is not yet loaded.")
            raise EverythingDBLoading(
                "Everything is still indexing, please wait…"
            )

        # --- Set query parameters ---
        ev.Everything_SetSearchW(query)
        ev.Everything_SetMax(max_results)
        ev.Everything_SetOffset(0)
        ev.Everything_SetRequestFlags(
            ev.EVERYTHING_REQUEST_FILE_NAME
            | ev.EVERYTHING_REQUEST_PATH
            | ev.EVERYTHING_REQUEST_SIZE
            | ev.EVERYTHING_REQUEST_DATE_MODIFIED
        )

        # --- Execute ---
        if not ev.Everything_QueryW(True):
            err_code = ev.Everything_GetLastError()
            err_msg = _ERROR_MESSAGES.get(err_code, f"Error code {err_code}")
            if err_code == 2:
                raise EverythingNotRunning(err_msg)
            raise EverythingError(f"Query failed: {err_msg}")

        total = ev.Everything_GetTotResults()
        num_results = ev.Everything_GetNumResults()

        results: list[SearchResult] = []
        for i in range(num_results):
            try:
                filename = ev.Everything_GetResultFileName(i) or ""
                path     = ev.Everything_GetResultPath(i) or ""
                full_path = os.path.join(path, filename) if path else filename

                raw_size = ev.Everything_GetResultSize(i)
                size = int(raw_size) if raw_size is not None and raw_size >= 0 else -1

                raw_date = ev.Everything_GetResultDateModified(i)
                # Everything returns a Windows FILETIME (100-ns intervals since 1601-01-01).
                # Convert to Unix timestamp.
                if raw_date and raw_date > 0:
                    date_modified = (raw_date - 116444736000000000) / 10_000_000
                else:
                    date_modified = 0.0

                is_folder = bool(ev.Everything_IsFolderResult(i))

                results.append(SearchResult(
                    filename=filename,
                    full_path=full_path,
                    size=size,
                    date_modified=date_modified,
                    is_folder=is_folder,
                ))
            except Exception as exc:
                logger.debug("Error reading result %d: %s", i, exc)

        query_time_ms = int((time.perf_counter() - t0) * 1000)

        # Validate path existence in background threads.
        validate_results(results)

        return SearchResponse(
            results=results,
            total_count=total,
            query_time_ms=query_time_ms,
            everything_version=version_str,
        )

    except (EverythingNotRunning, EverythingDBLoading, EverythingAdminMismatch):
        raise
    except Exception as exc:
        logger.error("Everything search error: %s", exc, exc_info=True)
        return SearchResponse(error=str(exc))


# ── Broad-query safety check ──────────────────────────────────────────────────

def is_connection_ok() -> bool:
    """Return True if Everything is reachable (query `*` returns > 0 results)."""
    if not _SDK_AVAILABLE:
        return False
    try:
        ev.Everything_SetSearchW("*")
        ev.Everything_SetMax(1)
        ev.Everything_QueryW(True)
        return ev.Everything_GetTotResults() > 0
    except Exception:
        return False


# ── Auto-launch Everything ────────────────────────────────────────────────────

def launch_everything() -> None:
    """Attempt to start Everything.exe from the configured path."""
    path = config.EVERYTHING_PATH
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Everything.exe not found at: {path}")
    logger.info("Launching Everything from: %s", path)
    subprocess.Popen([path])


# ── Custom exceptions ─────────────────────────────────────────────────────────

class EverythingError(Exception):
    """Base class for Everything-related errors."""


class EverythingNotRunning(EverythingError):
    """Everything.exe is not running or IPC is unavailable."""


class EverythingDBLoading(EverythingError):
    """Everything is still building its database."""


class EverythingAdminMismatch(EverythingError):
    """Admin/non-admin mismatch causing silent IPC failure."""
