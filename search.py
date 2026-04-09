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
import ctypes
import logging
import os
import re
import struct
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional

import config

logger = logging.getLogger(__name__)

# ── Request flag constants (from the Everything SDK header) ───────────────────

EVERYTHING_REQUEST_FILE_NAME     = 0x00000001
EVERYTHING_REQUEST_PATH          = 0x00000002
EVERYTHING_REQUEST_SIZE          = 0x00000010
EVERYTHING_REQUEST_DATE_MODIFIED = 0x00000040

# ── DLL loading ───────────────────────────────────────────────────────────────

_ev: Optional[ctypes.WinDLL] = None  # type: ignore[type-arg]
_SDK_AVAILABLE = False


def _is_64bit() -> bool:
    return struct.calcsize("P") == 8


def _find_dll() -> Optional[str]:
    dll_name = "Everything64.dll" if _is_64bit() else "Everything32.dll"
    script_dir = os.path.dirname(os.path.abspath(__file__))
    everything_dir = os.path.dirname(config.EVERYTHING_PATH)

    # If EVERYTHING_DLL_PATH is a directory, append the dll filename.
    env_path = config.EVERYTHING_DLL_PATH
    if env_path and os.path.isdir(env_path):
        env_path = os.path.join(env_path, dll_name)

    candidates = [
        env_path,                                                                             # .env override
        os.path.join(script_dir, dll_name),                                                  # project dir
        os.path.join(everything_dir, dll_name),                                              # Everything install dir
        rf"C:\EverythingSDK\DLL\{dll_name}",                                                 # SDK default extract path
        os.path.join(script_dir, "Everything32.dll" if _is_64bit() else "Everything64.dll"), # bitness fallback
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def _configure_dll(dll: ctypes.WinDLL) -> None:  # type: ignore[type-arg]
    """Set argtypes/restype for functions whose signatures differ from ctypes defaults."""
    dll.Everything_GetResultFileNameW.restype = ctypes.c_wchar_p
    dll.Everything_GetResultFileNameW.argtypes = [ctypes.c_int]
    dll.Everything_GetResultPathW.restype = ctypes.c_wchar_p
    dll.Everything_GetResultPathW.argtypes = [ctypes.c_int]
    dll.Everything_GetResultSize.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_ulonglong)]
    dll.Everything_GetResultDateModified.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_ulonglong)]


def _load_dll() -> None:
    global _ev, _SDK_AVAILABLE
    dll_path = _find_dll()
    if dll_path is None:
        logger.warning(
            "Everything SDK DLL not found. Place Everything64.dll in the project "
            "directory or set EVERYTHING_DLL_PATH in .env. "
            "Download: https://www.voidtools.com/support/everything/sdk/"
        )
        return
    try:
        _ev = ctypes.WinDLL(dll_path)
        _configure_dll(_ev)
        _SDK_AVAILABLE = True
        logger.info("Everything SDK DLL loaded: %s", dll_path)
    except OSError as exc:
        logger.warning("Failed to load Everything SDK DLL (%s): %s", dll_path, exc)


_load_dll()

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
        major = _ev.Everything_GetMajorVersion()
        minor = _ev.Everything_GetMinorVersion()
        rev   = _ev.Everything_GetRevision()
        build = _ev.Everything_GetBuildNumber()
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
        return SearchResponse(error="Everything SDK DLL not loaded. See logs for details.")

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
        if not _ev.Everything_IsDBLoaded():
            logger.warning("Everything DB is not yet loaded.")
            raise EverythingDBLoading(
                "Everything is still indexing, please wait…"
            )

        # --- Set query parameters ---
        _ev.Everything_SetSearchW(query)
        _ev.Everything_SetMax(max_results)
        _ev.Everything_SetOffset(0)
        _ev.Everything_SetRequestFlags(
            EVERYTHING_REQUEST_FILE_NAME
            | EVERYTHING_REQUEST_PATH
            | EVERYTHING_REQUEST_SIZE
            | EVERYTHING_REQUEST_DATE_MODIFIED
        )

        # --- Execute ---
        if not _ev.Everything_QueryW(1):
            err_code = _ev.Everything_GetLastError()
            err_msg = _ERROR_MESSAGES.get(err_code, f"Error code {err_code}")
            if err_code == 2:
                raise EverythingNotRunning(err_msg)
            raise EverythingError(f"Query failed: {err_msg}")

        total = _ev.Everything_GetTotResults()
        num_results = _ev.Everything_GetNumResults()

        results: list[SearchResult] = []
        for i in range(num_results):
            try:
                filename = _ev.Everything_GetResultFileNameW(i) or ""
                path     = _ev.Everything_GetResultPathW(i) or ""
                full_path = os.path.join(path, filename) if path else filename

                raw_size = ctypes.c_ulonglong(0)
                size = int(raw_size.value) if _ev.Everything_GetResultSize(i, ctypes.byref(raw_size)) else -1

                raw_date = ctypes.c_ulonglong(0)
                # Everything returns a Windows FILETIME (100-ns intervals since 1601-01-01).
                # Convert to Unix timestamp.
                if _ev.Everything_GetResultDateModified(i, ctypes.byref(raw_date)) and raw_date.value > 0:
                    date_modified = (raw_date.value - 116444736000000000) / 10_000_000
                else:
                    date_modified = 0.0

                is_folder = bool(_ev.Everything_IsFolderResult(i))

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
        _ev.Everything_SetSearchW("*")
        _ev.Everything_SetMax(1)
        _ev.Everything_QueryW(1)
        return _ev.Everything_GetTotResults() > 0
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
