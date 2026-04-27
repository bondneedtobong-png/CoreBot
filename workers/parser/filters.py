"""Общие фильтры сущностей парсинга (активность, язык, эвристики anti-bot)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
    lang = flt.get("lang")
    if lang and row.get("lang") and row["lang"] != lang:
        return False, "lang"
    return True, ""


def user_passes_filters(row: dict[str, Any], flt: dict[str, Any]) -> tuple[bool, str]:
    if not flt:
        return True, ""
    if flt.get("require_username") and not (row.get("username") or "").strip():
        return False, "no_username"
    if flt.get("skip_suspicious") and row.get("is_suspicious"):
        return False, "suspicious"
    lang = flt.get("lang")
    if lang and row.get("lang_guess") and row["lang_guess"] != lang:
        return False, "lang"
    return True, ""
