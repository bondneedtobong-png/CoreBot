"""
Генерация поисковых запросов: keyword + endings (каналы/группы).
Тестируемая чистая логика без Telethon.
"""
from __future__ import annotations

import re
from typing import Any, Iterable


_DEFAULT_ENDINGS = ("", " news", " chat", " channel", " live", " tv")


def normalize_keyword(k: str) -> str:
    s = (k or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s[:200]


def _normalize_list_items(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for v in values:
        s = normalize_keyword(str(v or ""))
        if s:
            out.append(s)
    return out


def _split_csv_like(text: str) -> list[str]:
    if not text:
        return []
    raw = re.split(r"[\n,;]+", text)
    return [normalize_keyword(x) for x in raw if normalize_keyword(x)]


def build_channel_or_group_queries(params: dict[str, Any]) -> list[str]:
    """
    params:
      keywords: list[str] preferred (multi-input)
      keyword: str fallback
      endings: list[str] preferred (multi-input)
      keyword_endings: list[str] fallback
    """
    keywords = _normalize_list_items(params.get("keywords"))
    if not keywords:
        keywords = _split_csv_like(str(params.get("keywords_text") or ""))
    if not keywords:
        one = normalize_keyword(str(params.get("keyword") or ""))
        if one:
            keywords = [one]
    if not keywords:
        return []

    endings = _normalize_list_items(params.get("endings"))
    if not endings:
        endings = _split_csv_like(str(params.get("endings_text") or ""))
    if not endings:
        raw_old = params.get("keyword_endings")
        endings = [normalize_keyword(str(e or "")) for e in raw_old] if isinstance(raw_old, list) else []
        endings = [x for x in endings if x]
    if not endings:
        endings = [x for x in _DEFAULT_ENDINGS if x]

    seen: set[str] = set()
    out: list[str] = []
    for kw in keywords:
        # 1) keyword itself
        if kw not in seen:
            seen.add(kw)
            out.append(kw)
        # 2) keyword + ending
        for e in endings:
            q = f"{kw} {e}".strip()
            if q and q not in seen:
                seen.add(q)
                out.append(q)
    return out


def manual_queries_from_txt(text: str) -> list[str]:
    """Разбор многострочного ввода / вставки из .txt: @name, t.me/name, числовые id."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.split("#", 1)[0].strip()
        if "t.me/" in line or "telegram.me/" in line:
            m = re.search(r"(?:t\.me|telegram\.me)/([A-Za-z0-9_]+)", line, re.I)
            if m:
                line = m.group(1)
        if line.startswith("@"):
            line = line[1:]
        if line and line not in seen:
            seen.add(line)
            out.append(line)
    return out


def merge_query_lists(*lists: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for lst in lists:
        for q in lst:
            q = (q or "").strip()
            if not q or q in seen:
                continue
            seen.add(q)
            out.append(q)
    return out
