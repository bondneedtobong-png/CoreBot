"""
Depth=2: ограниченное расширение запросов по «семенам» (заголовки найденных сущностей).
Без рекурсивного depth=3 — только один дополнительный слой запросов.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

_STOP = frozenset(
    "и в на с по для из к а я о у мы вы они он она это как что не да ли "
    "the a an of and or to in on for is at by from with be are was were "
    "news chat channel live tv group".split()
)


def _tokens(title: str, max_tokens: int = 3) -> list[str]:
    t = re.sub(r"[^\w\s\u0400-\u04FF]", " ", (title or "").lower())
    parts = [p for p in t.split() if len(p) > 2 and p not in _STOP]
    return parts[:max_tokens]


def expand_queries_from_seeds(
    seeds: Iterable[dict[str, Any]],
    base_keyword: str,
    *,
    max_extra_queries: int = 25,
) -> list[str]:
    """
    seeds: iterable of dicts with keys title, username (optional)
    Возвращает дополнительные строки поиска (без base_keyword дублей).
    """
    base_kw = (base_keyword or "").strip().lower()
    out: list[str] = []
    seen: set[str] = set()
    for s in seeds:
        title = str(s.get("title") or "")
        un = str(s.get("username") or "").lstrip("@")
        for tok in _tokens(title):
            q = f"{tok}".strip()
            if not q or len(q) < 3:
                continue
            key = q.lower()
            if key == base_kw or key in seen:
                continue
            seen.add(key)
            out.append(q)
            if len(out) >= max_extra_queries:
                return out
        if un and len(un) >= 4:
            q = un
            key = q.lower()
            if key != base_kw and key not in seen:
                seen.add(key)
                out.append(q)
                if len(out) >= max_extra_queries:
                    return out
    return out
