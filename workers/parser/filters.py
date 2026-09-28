"""Общие фильтры сущностей парсинга (активность, язык, эвристики anti-bot)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from utils.time import utcnow_aware
from typing import Any, Optional
import re


def _utcnow() -> datetime:
    return utcnow_aware()


def is_active_7d(last_post_at: Optional[datetime]) -> bool:
    if last_post_at is None:
        return False
    lp = last_post_at
    if lp.tzinfo is None:
        lp = lp.replace(tzinfo=timezone.utc)
    return lp >= _utcnow() - timedelta(days=7)


def detect_lang_approx(text: str) -> Optional[str]:
    """Грубая эвристика ru/en по буквам."""
    if not text:
        return None
    cyr = sum(1 for c in text if "\u0400" <= c <= "\u04ff")
    lat = sum(1 for c in text.lower() if "a" <= c <= "z")
    if cyr >= 3 and cyr >= lat:
        return "ru"
    if lat >= 3 and lat > cyr:
        return "en"
    return None


def user_looks_suspicious(
    *,
    username: Optional[str],
    telegram_id: int,
    has_avatar: Optional[bool],
) -> bool:
    """Простая эвристика «бот/пустышка» (MVP)."""
    if username and len(username) >= 4:
        return False
    if has_avatar is True:
        return False
    # Очень длинные id без username — подозрительно
    if not username and telegram_id > 10**12:
        return True
    return False


def channel_passes_filters(row: dict[str, Any], flt: dict[str, Any]) -> tuple[bool, str]:
    if not flt:
        return True, ""
    if flt.get("is_active_7d") and not row.get("is_active_7d"):
        return False, "inactive_7d"
    if flt.get("has_discussion") and not row.get("has_discussion"):
        return False, "no_discussion"
    if flt.get("is_public") is not None:
        want = bool(flt["is_public"])
        if row.get("is_public") is not None and bool(row["is_public"]) != want:
            return False, "is_public"
    smin = flt.get("subscribers_min")
    smax = flt.get("subscribers_max")
    subs = row.get("subscribers")
    if smin is not None and subs is not None and subs < int(smin):
        return False, "subscribers_min"
    if smax is not None and subs is not None and subs > int(smax):
        return False, "subscribers_max"
    lang = flt.get("lang")
    if lang and row.get("lang") and row["lang"] != lang:
        return False, "lang"
    return True, ""


def group_passes_filters(row: dict[str, Any], flt: dict[str, Any]) -> tuple[bool, str]:
    if not flt:
        return True, ""
    if flt.get("is_active_7d") and not row.get("is_active_7d"):
        return False, "inactive_7d"
    gt = flt.get("group_type")
    if gt and row.get("group_type") and row["group_type"] != gt:
        return False, "group_type"
    # Match channel subscriber bounds: accept integer-like inputs (including
    # numeric strings) and leave unknown Telegram counts eligible.
    members_min = flt.get("members_min")
    members_max = flt.get("members_max")
    members = row.get("members_count")
    if members_min is not None and members is not None and members < int(members_min):
        return False, "members_min"
    if members_max is not None and members is not None and members > int(members_max):
        return False, "members_max"
    lang = flt.get("lang")
    if lang and row.get("lang") and row["lang"] != lang:
        return False, "lang"
    return True, ""


def user_passes_filters(row: dict[str, Any], flt: dict[str, Any]) -> tuple[bool, str]:
    if not flt:
        return True, ""
    # Treat unknown/missing Telegram premium flags as not confirmed Premium.
    if flt.get("require_premium") and row.get("premium") is not True:
        return False, "not_premium"
    if flt.get("exclude_scam_fake"):
        if row.get("scam") is True:
            return False, "scam"
        if row.get("fake") is True:
            return False, "fake"
    if flt.get("require_username") and not (row.get("username") or "").strip():
        return False, "no_username"
    if flt.get("require_avatar") and not bool(row.get("has_avatar")):
        return False, "no_avatar"
    if flt.get("exclude_deleted") and bool(row.get("is_deleted")):
        return False, "deleted"
    if flt.get("anti_bot"):
        if bool(row.get("is_deleted")):
            return False, "deleted"
        if not (row.get("username") or "").strip() and not bool(row.get("has_avatar")):
            return False, "empty_profile"
    if flt.get("skip_suspicious") and row.get("is_suspicious"):
        return False, "suspicious"
    if flt.get("recent_online_7d"):
        last_seen_at = row.get("last_seen_at")
        if not last_seen_at:
            return False, "not_recent_online"
        if isinstance(last_seen_at, datetime):
            ls = last_seen_at if last_seen_at.tzinfo else last_seen_at.replace(tzinfo=timezone.utc)
            if ls < _utcnow() - timedelta(days=7):
                return False, "not_recent_online"
    lang = flt.get("lang")
    if lang and row.get("lang_guess") and row["lang_guess"] != lang:
        return False, "lang"
    return True, ""


def normalize_message_filters(raw: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate a message-only filter and store its window in UTC."""
    if not isinstance(raw, dict):
        return {}, [{"field": "message_filters", "reason": "invalid_type"}]
    errors: list[dict[str, Any]] = []
    normalized: dict[str, Any] = {}
    for field in raw:
        if field not in ("keywords", "from_at", "to_at"):
            errors.append({"field": field, "reason": "unknown_field"})

    if "keywords" in raw:
        keywords = raw["keywords"]
        if not isinstance(keywords, list):
            errors.append({"field": "keywords", "reason": "invalid_type"})
        else:
            if len(keywords) > 20:
                errors.append({"field": "keywords", "reason": "too_many_keywords", "limit": 20})
            accepted: list[str] = []
            seen: set[str] = set()
            for index, value in enumerate(keywords[:20], 1):
                if not isinstance(value, str):
                    errors.append({"field": "keywords", "index": index, "reason": "invalid_type"})
                    continue
                term = re.sub(r"\s+", " ", value.strip())
                if not term:
                    errors.append({"field": "keywords", "index": index, "reason": "empty_keyword"})
                elif len(term) > 80:
                    errors.append({"field": "keywords", "index": index,
                                   "reason": "keyword_too_long", "limit": 80})
                elif term.casefold() not in seen:
                    seen.add(term.casefold())
                    accepted.append(term)
            if accepted:
                normalized["keywords"] = accepted

    parsed_dates: dict[str, datetime] = {}
    for field in ("from_at", "to_at"):
        if field not in raw or raw[field] in (None, ""):
            continue
        value = raw[field]
        if not isinstance(value, str):
            errors.append({"field": field, "reason": "invalid_type"})
            continue
        if len(value) > 64:
            errors.append({"field": field, "reason": "invalid_datetime"})
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            errors.append({"field": field, "reason": "invalid_datetime"})
            continue
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            errors.append({"field": field, "reason": "timezone_required"})
            continue
        parsed_dates[field] = parsed.astimezone(timezone.utc)
        normalized[field] = parsed_dates[field].isoformat()
    if ("from_at" in parsed_dates and "to_at" in parsed_dates
            and parsed_dates["from_at"] > parsed_dates["to_at"]):
        errors.append({"field": "to_at", "reason": "invalid_range"})
    return normalized, errors


def message_passes_filters(
    text: str | None,
    message_at: datetime | None,
    flt: dict[str, Any] | None,
) -> tuple[bool, str]:
    """Apply inclusive UTC window and OR keywords to a visible message."""
    if not flt:
        return True, ""
    from_at = flt.get("from_at")
    to_at = flt.get("to_at")
    if from_at or to_at:
        if not isinstance(message_at, datetime):
            return False, "message_date_missing"
        event_at = message_at if message_at.tzinfo else message_at.replace(tzinfo=timezone.utc)
        event_at = event_at.astimezone(timezone.utc)
        if from_at and event_at < datetime.fromisoformat(from_at):
            return False, "message_before_from"
        if to_at and event_at > datetime.fromisoformat(to_at):
            return False, "message_after_to"
    keywords = flt.get("keywords") or []
    if keywords and not any(term.casefold() in (text or "").casefold() for term in keywords):
        return False, "message_keyword_no_match"
    return True, ""
