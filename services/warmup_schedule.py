"""Local off-hours schedule for opt-in, low-frequency account activity."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


MIN_INTERVAL_SECONDS = 30 * 60
DEFAULT_WORK_START = 9
DEFAULT_WORK_END = 18
DEFAULT_TIME_ZONE = "Europe/Moscow"
_TARGET_RE = re.compile(r"^@?[A-Za-z0-9_]{5,32}$")


def parse_read_targets(raw: str) -> list[str]:
    """Only public usernames explicitly supplied by the operator; never invite links."""
    targets = []
    for line in (raw or "").splitlines():
        item = line.strip()
        if item.startswith(("https://t.me/", "http://t.me/")):
            item = item.split("t.me/", 1)[1].split("/", 1)[0]
        if _TARGET_RE.fullmatch(item):
            target = "@" + item.lstrip("@")
            if target not in targets:
                targets.append(target)
    return targets


def validate_schedule(time_zone: str, work_start: int, work_end: int) -> None:
    try:
        ZoneInfo(time_zone)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError("Неизвестный часовой пояс IANA") from exc
    if not (0 <= work_start <= 23 and 0 <= work_end <= 23 and work_start != work_end):
        raise ValueError("Рабочие часы: 0–23, начало и конец должны различаться")


def is_off_hours(now_utc: datetime, time_zone: str, work_start: int, work_end: int) -> bool:
    """Work interval is [start, end), including intervals across midnight."""
    validate_schedule(time_zone, work_start, work_end)
    aware_utc = now_utc.replace(tzinfo=timezone.utc) if now_utc.tzinfo is None else now_utc.astimezone(timezone.utc)
    local = aware_utc.astimezone(ZoneInfo(time_zone))
    hour = local.hour
    if work_start < work_end:
        return not (work_start <= hour < work_end)
    return not (hour >= work_start or hour < work_end)


def next_off_hours(now_utc: datetime, time_zone: str, work_start: int, work_end: int) -> datetime:
    """Return the first eligible UTC minute (naive UTC for SQLite)."""
    validate_schedule(time_zone, work_start, work_end)
    instant = now_utc.replace(tzinfo=None) if now_utc.tzinfo else now_utc
    if is_off_hours(instant, time_zone, work_start, work_end):
        return instant
    # Minute scan handles DST gaps and repeated local hours without constructing nonexistent local times.
    candidate = instant.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(24 * 60 + 1):
        if is_off_hours(candidate, time_zone, work_start, work_end):
            return candidate
        candidate += timedelta(minutes=1)
    raise RuntimeError("No off-hours found within 24 hours")


def next_action_at(now_utc: datetime, last_at: datetime | None, requested_seconds: float, jitter_seconds: float) -> datetime:
    """Lower bound from persisted last action; caller draws jitter once and persists result."""
    delay = max(MIN_INTERVAL_SECONDS, requested_seconds + jitter_seconds)
    earliest = now_utc + timedelta(seconds=delay)
    if last_at is not None:
        earliest = max(earliest, last_at + timedelta(seconds=MIN_INTERVAL_SECONDS))
    return earliest
