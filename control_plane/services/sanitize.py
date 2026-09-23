"""PII/secret sanitizer for task 08 observability payloads.

Structural rule (enforced by the snapshot/alert builders, verified by tests):
observability payloads carry counts/ids/statuses/timestamps only — never
message bodies (``outbound_queue.text``, neuro-chat content,
``client_interactions.body``) and never proxy credentials. This module is
defense-in-depth: it scrubs any string that looks like a secret and masks
values stored under deny-listed keys.

Deliberately conservative so diagnostics survive:

* value-pattern redaction covers only shapes that cannot be legitimate
  diagnostics: Telegram bot tokens (``<digits>:<long tail>``), proxy
  credentials inside URLs (``scheme://user:pass@host``) and international
  phone numbers (must start with ``+`` so naive timestamps, counters and
  git SHAs are never touched);
* key-based masking applies to string values only, so numeric counters such
  as ``session_count`` pass through untouched;
* git SHAs / version strings / ISO timestamps are always preserved.
"""

from __future__ import annotations

import re
from typing import Any

MASK = "***REDACTED***"

# <5-15 digits>:<20+ token chars> — Telegram bot token shape.
_BOT_TOKEN_RE = re.compile(r"\b\d{5,15}:[A-Za-z0-9_-]{20,}\b")

# scheme://user:password@host — proxy credentials inside a URL.
_PROXY_CREDS_RE = re.compile(r"(?<=://)[^/\s:?#]+:[^/\s@?#]+@")

# International phone numbers. The leading ``+`` is mandatory so that naive
# UTC timestamps (``2026-09-22 10:00:00``), counters and SHAs never match.
_PHONE_RE = re.compile(r"\+\d[\d\s\-()]{7,}\d")

#: Key fragments whose *string* values are always masked. Matching is
#: substring-based on the lowercased key; non-string values (ints, bools,
#: None) are never masked, so counters like ``session_count`` survive.
DENY_KEY_FRAGMENTS = frozenset(
    {
        "token",
        "secret",
        "password",
        "passwd",
        "pwd",
        "api_hash",
        "api_key",
        "apikey",
        "ciphertext",
        "private_key",
        "session_string",
        "auth_key",
        "proxy_pass",
        "connection_string",
    }
)

#: Hard cap for a single string inside a payload (log-bomb protection).
MAX_STRING_LEN = 4000


def _is_deny_key(key: str) -> bool:
    lowered = str(key).lower()
    return any(frag in lowered for frag in DENY_KEY_FRAGMENTS)


def scrub_text(value: str) -> str:
    """Redact secret-shaped substrings inside free text."""
    if not isinstance(value, str):
        return value
    out = _BOT_TOKEN_RE.sub("[BOT_TOKEN]", value)
    out = _PROXY_CREDS_RE.sub("***:***@", out)
    out = _PHONE_RE.sub("[PHONE]", out)
    if len(out) > MAX_STRING_LEN:
        out = out[:MAX_STRING_LEN] + "…[truncated]"
    return out


def sanitize(obj: Any, _key: str = "") -> Any:
    """Recursively sanitize a JSON-like payload (dicts/lists/scalars)."""
    if isinstance(obj, dict):
        return {
            k: (
                MASK
                if (_is_deny_key(k) and isinstance(v, str))
                else sanitize(v, str(k))
            )
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [sanitize(v, _key) for v in obj]
    if isinstance(obj, str):
        if _key and _is_deny_key(_key):
            return MASK
        return scrub_text(obj)
    return obj


def find_leaks(obj: Any, needles: list[str], _path: str = "$") -> list[str]:
    """Return payload paths where any of ``needles`` appears (test helper).

    Empty ``needles`` and empty needle strings are ignored. Useful to assert
    that a snapshot/alert JSON contains no secret or message-body text.
    """
    hits: list[str] = []
    live = [n for n in needles if n]
    if isinstance(obj, dict):
        for k, v in obj.items():
            hits.extend(find_leaks(v, live, f"{_path}.{k}"))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            hits.extend(find_leaks(v, live, f"{_path}[{i}]"))
    elif isinstance(obj, str):
        for needle in live:
            if needle in obj:
                hits.append(f"{_path} contains {needle!r:.60}")
    return hits


__all__ = [
    "MASK",
    "DENY_KEY_FRAGMENTS",
    "MAX_STRING_LEN",
    "scrub_text",
    "sanitize",
    "find_leaks",
]
