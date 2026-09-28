"""
Генерация поисковых запросов: keyword + endings (каналы/группы).
Тестируемая чистая логика без Telethon.
"""
from __future__ import annotations

import re
from typing import Any, Iterable


_DEFAULT_ENDINGS = ("", " news", " chat", " channel", " live", " tv")
_MAX_SEARCH_INPUT = 100_000
_MAX_SEARCH_ITEMS = 200
_MAX_SEARCH_QUERIES = 200


def normalize_keyword(k: str) -> str:
    s = (k or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s[:200]


def _normalized_length(value: str) -> int:
    return len(re.sub(r"\s+", " ", value.strip()))


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
        if kw.casefold() not in seen:
            seen.add(kw.casefold())
            out.append(kw)
        # 2) keyword + ending
        for e in endings:
            q = f"{kw} {e}".strip()
            if q and q.casefold() not in seen:
                seen.add(q.casefold())
                out.append(q)
    return out


def preview_channel_or_group_queries(params: dict[str, Any]) -> dict[str, Any]:
    """Show the search plan and validation errors before queueing a task.

    Valid plans use the same generator and source order as collect_channels/groups.
    Limits here bound local worker input; they are not Telegram limits.
    """
    errors: list[dict[str, Any]] = []
    if not isinstance(params, dict):
        return {"queries": [], "count": 0, "errors": [
            {"field": "params", "reason": "invalid_type"}
        ], "truncated": False}

    text_fields = ("keyword", "keywords_text", "endings_text", "manual_usernames_text")
    list_fields = ("keywords", "endings", "keyword_endings")
    total_length = 0
    for field in text_fields:
        value = params.get(field)
        if value is None:
            continue
        if not isinstance(value, str):
            errors.append({"field": field, "reason": "invalid_type"})
            continue
        total_length += len(value)
        if field == "keyword" and _normalized_length(value) > 200:
            errors.append({"field": field, "reason": "too_long", "limit": 200})
        if field in ("keywords_text", "endings_text"):
            parts = [part.strip() for part in re.split(r"[\n,;]+", value) if part.strip()]
            if len(parts) > _MAX_SEARCH_ITEMS:
                errors.append({"field": field, "reason": "too_many_items", "limit": _MAX_SEARCH_ITEMS})
            for index, part in enumerate(parts, 1):
                if _normalized_length(part) > 200:
                    errors.append({"field": field, "index": index, "reason": "too_long", "limit": 200})
                    break
    for field in list_fields:
        value = params.get(field)
        if value is None:
            continue
        if not isinstance(value, list):
            errors.append({"field": field, "reason": "invalid_type"})
            continue
        if len(value) > _MAX_SEARCH_ITEMS:
            errors.append({"field": field, "reason": "too_many_items", "limit": _MAX_SEARCH_ITEMS})
        for index, item in enumerate(value, 1):
            if not isinstance(item, str):
                errors.append({"field": field, "index": index, "reason": "invalid_type"})
                continue
            total_length += len(item)
            if _normalized_length(item) > 200:
                errors.append({"field": field, "index": index, "reason": "too_long", "limit": 200})
    if total_length > _MAX_SEARCH_INPUT:
        errors.append({"field": "params", "reason": "too_long", "limit": _MAX_SEARCH_INPUT})
    if errors:
        return {"queries": [], "count": 0, "errors": errors, "truncated": False}

    manual = preview_manual_sources(params.get("manual_usernames_text") or "")
    errors.extend({"field": "manual_usernames_text", **error} for error in manual["errors"])
    queries = merge_query_lists(build_channel_or_group_queries(params), manual["sources"])
    if not queries and not errors:
        errors.append({"field": "params", "reason": "empty_queries"})
    for index, query in enumerate(queries, 1):
        if len(query) > 200:
            errors.append({"field": "queries", "index": index, "reason": "too_long", "limit": 200})
            break
    if len(queries) > _MAX_SEARCH_QUERIES:
        errors.append({"field": "queries", "reason": "too_many_queries", "limit": _MAX_SEARCH_QUERIES})
    return {
        "queries": queries[:_MAX_SEARCH_QUERIES],
        "count": len(queries),
        "errors": errors,
        "truncated": len(queries) > _MAX_SEARCH_QUERIES,
    }


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


def preview_manual_sources(text: str) -> dict[str, Any]:
    """Validate and deduplicate chat sources before a users task is queued."""
    accepted: list[str] = []
    errors: list[dict[str, Any]] = []
    duplicates = 0
    seen: set[str] = set()
    lines = (text or "").splitlines()
    if len(lines) > 2000:
        return {"sources": [], "duplicates": 0,
                "errors": [{"line": 0, "reason": "too_many_lines"}]}
    for line_no, raw in enumerate(lines, 1):
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        if len(value) > 512:
            errors.append({"line": line_no, "reason": "too_long"})
            continue
        linked = re.fullmatch(
            r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]+)(?:/|\?.*)?",
            value, re.I,
        )
        if linked:
            value = linked.group(1)
        elif "t.me/" in value.lower() or "telegram.me/" in value.lower():
            errors.append({"line": line_no, "reason": "unsupported_link"})
            continue
        value = value.removeprefix("@")
        if re.fullmatch(r"-?\d{1,20}", value):
            normalized = value
        elif re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{2,63}", value):
            normalized = value.lower()
        else:
            errors.append({"line": line_no, "reason": "invalid_source"})
            continue
        if normalized in seen:
            duplicates += 1
            continue
        seen.add(normalized)
        accepted.append(normalized)
    return {"sources": accepted, "duplicates": duplicates, "errors": errors}


def merge_query_lists(*lists: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for lst in lists:
        for q in lst:
            q = (q or "").strip()
            if not q or q.casefold() in seen:
                continue
            seen.add(q.casefold())
            out.append(q)
    return out
