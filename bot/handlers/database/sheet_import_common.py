"""Общий разбор TXT со списком @username для импорта листов."""
from __future__ import annotations

import re

_USERNAME_RE = re.compile(r"@?([a-zA-Z0-9_]{5,32})")


def parse_usernames_from_txt(content: str) -> set[str]:
    usernames: set[str] = set()
    for match in _USERNAME_RE.finditer(content or ""):
        username = match.group(1).lower()
        if not username.startswith("telegram") and not username.startswith("bot"):
            usernames.add(username)
    return usernames
