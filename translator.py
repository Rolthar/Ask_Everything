"""translator.py — natural language → Everything query via Claude API.

Returns the raw Everything query string, or the sentinel "UNCLEAR" if Claude
cannot produce a valid translation.

Edge cases handled:
- Missing / invalid API key  → raises ConfigError before making any call
- Rate limiting (429)        → exponential backoff, up to API_MAX_RETRIES
- Network timeout            → raises TranslationTimeout
- Non-query response         → sanitised; falls back to UNCLEAR
- Overly broad query         → returns BroadQueryWarning sentinel
- Very long NL input         → truncated to 500 chars
"""

import logging
import re
import time
from typing import Optional

import anthropic

import config

logger = logging.getLogger(__name__)

UNCLEAR = "UNCLEAR"
BROAD_QUERY = "__BROAD__"  # Returned when query is just `*` or has no filters


# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a search query translator for the Windows application "Everything" by voidtools.

Your ONLY job is to translate the user's natural language description into a valid Everything search query string.

RULES:
1. Return ONLY the raw Everything query string — no explanation, no markdown, no surrounding quotes.
2. If you cannot produce a meaningful query, return the single token: UNCLEAR
3. Never include newlines in your response.
4. Keep the query under 500 characters.

EVERYTHING SYNTAX REFERENCE:

Boolean:
  a b          → AND (implicit, space-separated)
  a | b        → OR
  !a           → NOT
  <a b>        → grouping

Wildcards:
  *            → any sequence of characters
  ?            → any single character

Filters:
  ext:pdf                  → file extension (no dot); OR: ext:pdf|docx|xlsx
  file:                    → files only
  folder:                  → folders only
  path:C:\\Users\\Dom        → path containing string
  parent:C:\\Projects       → direct parent folder
  depth:1                  → folder depth from root
  empty:                   → empty folders

Size:
  size:>1mb    size:<500kb    size:1gb..2gb
  Units: kb, mb, gb, tb    Also raw bytes: size:>1048576

Date modifiers (dm = modified, da = accessed, dc = created):
  dm:today     dm:yesterday
  dm:this week    dm:last week    dm:this month    dm:last month
  dm:this year    dm:last year
  dm:2024      dm:01/2024      dm:01/01/2024
  dm:01/01/2024..31/12/2024

Attributes:
  attrib:H   attrib:R   attrib:S   attrib:A

Run history:
  rc:>5        dr:today

Regex:
  regex:\\.log$

Content (Everything 1.5+ only):
  content:TODO

EXAMPLE QUERIES:
  ext:mp4|mov|mkv size:>500mb da:<last year
  path:*unreal* | path:*ue5* ext:docx|pdf dm:last month
  *invoice* ext:pdf dm:this year !path:*archive*
  ext:py dm:this week !path:*node_modules* !path:*.venv*
  folder: dm:last month size:>1gb
"""


# ── Known valid filter prefixes (for lightweight validation) ─────────────────

_VALID_FILTERS = {
    "ext", "file", "folder", "path", "parent", "depth", "empty",
    "size", "dm", "da", "dc", "attrib", "rc", "dr", "regex", "content",
}

_INVALID_FILTER_RE = re.compile(r"\b([a-z]+):")


def _has_invalid_filter(query: str) -> bool:
    """Return True if query contains a filter prefix not in _VALID_FILTERS."""
    for match in _INVALID_FILTER_RE.finditer(query):
        prefix = match.group(1)
        if prefix not in _VALID_FILTERS:
            logger.warning("Possibly invalid filter in query: '%s:'", prefix)
            return True
    return False


def _has_unmatched_angle_brackets(query: str) -> bool:
    depth = 0
    for ch in query:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        if depth < 0:
            return True
    return depth != 0


def _sanitise(raw: str) -> str:
    """Strip markdown, surrounding quotes, and validate the response."""
    # Strip markdown code fences.
    raw = re.sub(r"```[^\n]*\n?", "", raw)
    # Strip leading/trailing whitespace and quotes.
    raw = raw.strip().strip('"').strip("'").strip("`").strip()
    # Reject multi-line responses.
    if "\n" in raw:
        logger.warning("Translator returned multi-line response; falling back to UNCLEAR.")
        return UNCLEAR
    # Reject overly long responses.
    if len(raw) > 500:
        logger.warning("Translator response too long (%d chars); falling back to UNCLEAR.", len(raw))
        return UNCLEAR
    # Reject empty.
    if not raw:
        return UNCLEAR
    return raw


def _is_broad(query: str) -> bool:
    """Return True if the query has no meaningful filter and would match everything."""
    stripped = query.strip()
    return stripped in ("*", "", "file:", "folder:")


# ── Main translation function ─────────────────────────────────────────────────

def translate(nl_query: str) -> str:
    """Translate *nl_query* to an Everything query.

    Returns:
        The Everything query string.
        ``UNCLEAR`` if translation failed.
        ``BROAD_QUERY`` if the query would match all files.

    Raises:
        ConfigError: if the API key is missing.
        TranslationTimeout: if the API call timed out.
    """
    if not config.ANTHROPIC_API_KEY:
        raise ConfigError("Anthropic API key is not configured.")

    # Truncate very long NL input.
    if len(nl_query) > 500:
        logger.warning("NL query truncated from %d to 500 chars.", len(nl_query))
        nl_query = nl_query[:500]

    client = anthropic.Anthropic(
        api_key=config.ANTHROPIC_API_KEY,
        timeout=config.API_TIMEOUT,
    )

    last_exc: Optional[Exception] = None
    delay = 1.0

    for attempt in range(1, config.API_MAX_RETRIES + 1):
        try:
            logger.debug("Translation attempt %d for: %s", attempt, nl_query[:60])
            response = client.messages.create(
                model=config.CLAUDE_MODEL,
                max_tokens=256,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": nl_query}],
            )
            raw = response.content[0].text if response.content else ""
            query = _sanitise(raw)

            # Lightweight validation.
            if query != UNCLEAR and _has_unmatched_angle_brackets(query):
                logger.warning("Query has unmatched angle brackets: %s", query)
                query = UNCLEAR

            if query != UNCLEAR and _has_invalid_filter(query):
                # Log but don't reject — filter detection can have false positives.
                logger.warning("Suspect filter in query: %s", query)

            if query != UNCLEAR and _is_broad(query):
                logger.info("Query is overly broad: %s", query)
                return BROAD_QUERY

            logger.info("Translated '%s' → '%s'", nl_query[:60], query)
            return query

        except anthropic.RateLimitError as exc:
            last_exc = exc
            logger.warning("Rate limited (attempt %d); retrying in %.0fs.", attempt, delay)
            if attempt < config.API_MAX_RETRIES:
                time.sleep(delay)
                delay *= 2
        except anthropic.APITimeoutError as exc:
            logger.error("Translation timed out: %s", exc)
            raise TranslationTimeout("Translation timed out — try again.") from exc
        except anthropic.AuthenticationError as exc:
            logger.error("Authentication error: %s", exc)
            raise ConfigError("Invalid Anthropic API key.") from exc
        except anthropic.APIError as exc:
            logger.error("API error (attempt %d): %s %s", attempt, exc.status_code, exc)
            last_exc = exc
            if attempt < config.API_MAX_RETRIES:
                time.sleep(delay)
                delay *= 2

    raise TranslationError(f"Translation failed after {config.API_MAX_RETRIES} retries.") from last_exc


# ── Custom exceptions ─────────────────────────────────────────────────────────

class TranslatorError(Exception):
    """Base class for translator errors."""


class ConfigError(TranslatorError):
    """API key missing or invalid."""


class TranslationTimeout(TranslatorError):
    """Claude API call timed out."""


class TranslationError(TranslatorError):
    """General translation failure (e.g. all retries exhausted)."""
