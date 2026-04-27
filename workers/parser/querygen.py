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


def build_channel_or_group_queries(params: dict[str, Any]) -> list[str]:
    """
    params:
      keyword: str (required)
      keyword_endings: list[str] optional — доп. суффиксы к keyword
    """
    kw = normalize_keyword(str(params.get("keyword") or ""))
    if not kw:
        return []
    endings = params.get("keyword_endings")
    if endings is None or not isinstance(endings, list):
        endings = list(_DEFAULT_ENDINGS)
    else:
        endings = [str(e) if e is not None else "" for e in endings]
    if "" not in endings:
        endings = [""] + endings
    seen: set[str] = set()
    out: list[str] = []
    for e in endings:
        q = (kw + str(e)).strip()
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
