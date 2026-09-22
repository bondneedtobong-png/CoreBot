"""Чтение файлов loguru для выгрузки в боте."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from utils.time import utcnow_naive
from pathlib import Path
from typing import Optional

from utils.logger import LOG_FILE

_LINE_TIME = re.compile(
    r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})"
)


def window_to_delta(callback_data: str) -> Optional[timedelta]:
    m = {
        "db_dbg_10m": timedelta(minutes=10),
        "db_dbg_30m": timedelta(minutes=30),
        "db_dbg_2h": timedelta(hours=2),
        "db_dbg_1d": timedelta(days=1),
        "db_dbg_7d": timedelta(days=7),
        "db_dbg_30d": timedelta(days=30),
    }
    return m.get(callback_data)


def read_log_tail_bytes(path: Path, max_bytes: int = 900_000) -> str:
    if not path.exists():
        return ""
    raw = path.read_bytes()
    if len(raw) <= max_bytes:
        return raw.decode("utf-8", errors="replace")
    return raw[-max_bytes:].decode("utf-8", errors="replace")


def filter_log_by_time(text: str, since: datetime) -> str:
    """Оставляет строки с меткой времени >= since (UTC naive vs aware fix)."""
    if since.tzinfo:
        since = since.astimezone(timezone.utc).replace(tzinfo=None)
    lines_out: list[str] = []
    for line in text.splitlines():
        m = _LINE_TIME.match(line)
        if not m:
            if lines_out:
                lines_out.append(line)
            continue
        try:
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if ts >= since:
            lines_out.append(line)
    return "\n".join(lines_out) if lines_out else text[-200_000:]


def build_debug_excerpt(callback_data: str) -> tuple[str, str]:
    delta = window_to_delta(callback_data)
    if not delta or not LOG_FILE.exists():
        return "", "Лог-файл не найден или окно неизвестно."
    raw = read_log_tail_bytes(LOG_FILE)
    since = utcnow_naive() - delta
    filtered = filter_log_by_time(raw, since)
    if len(filtered) > 350_000:
        filtered = filtered[-350_000:]
    return filtered, LOG_FILE.name
