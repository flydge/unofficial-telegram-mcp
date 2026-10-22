"""Sanitize all untrusted Telegram-originated strings."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit

_SPACE_RUN = re.compile(r"\s+")
_EVIDENCE_PREFIX = "[untrusted Telegram evidence] "
_LINK_HOSTS = frozenset({"t.me", "telegram.me"})
MAX_TELEGRAM_LINK = 512


def sanitize_telegram_text(value: object, *, max_length: int = 2000) -> str:
    """Return single-line text without controls, bidi formatting, or hidden payloads."""
    if max_length < 1:
        raise ValueError("max_length must be positive")
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    safe: list[str] = []
    for character in normalized:
        if character.isspace():
            safe.append(" ")
            continue
        if unicodedata.category(character).startswith("C"):
            continue
        safe.append(character)
    collapsed = _SPACE_RUN.sub(" ", "".join(safe)).strip()
    if len(collapsed) > max_length:
        return collapsed[: max_length - 1] + "…"
    return collapsed


def normalize_search_text(value: object, *, max_length: int) -> str:
    """Normalize search operands identically before case-insensitive comparison."""
    return sanitize_telegram_text(value, max_length=max_length).casefold()


def render_evidence(value: object, *, max_length: int = 2000) -> str:
    """Mark sanitized content so consumers cannot confuse it with instructions."""
    available = max_length - len(_EVIDENCE_PREFIX)
    if available < 1:
        raise ValueError("max_length is too small for the evidence marker")
    return _EVIDENCE_PREFIX + sanitize_telegram_text(value, max_length=available)


def safe_telegram_link(value: object) -> str | None:
    """Return a provider link only when it is an exact https t.me/telegram.me URL.

    The one shared rule for chat and message links: a string of at most 512
    characters that sanitization leaves unchanged (so never truncated or
    rewritten), scheme https, and a bare host t.me or telegram.me with no
    credentials or port. Anything else is no link at all.
    """
    if not isinstance(value, str) or not value or len(value) > MAX_TELEGRAM_LINK or not value.isascii():
        return None
    if sanitize_telegram_text(value, max_length=MAX_TELEGRAM_LINK) != value or " " in value:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.netloc not in _LINK_HOSTS:
        return None
    return value
