"""Лимиты проверки TData (задача 12) — числа + env-переопределения.

Значения по умолчанию зафиксированы в docstring пакета
:mod:`services.tdata_check` и дублируются здесь как код (single source —
этот модуль; доки ссылаются сюда).
"""

from __future__ import annotations

import os


def _get_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default)).strip() or default))
    except (TypeError, ValueError):
        return default


def _get_float(name: str, default: float) -> float:
    try:
        return max(1.0, float(os.getenv(name, str(default)).strip() or default))
    except (TypeError, ValueError):
        return default


#: Максимальный размер принимаемого ZIP-архива (байты). Дефолт 200 МБ.
MAX_ARCHIVE_BYTES = _get_int("TDATA_CHECK_MAX_ARCHIVE_BYTES", 200 * 1024 * 1024)

#: Максимальное число TData-папок из одного ZIP. Остаток отбрасывается
#: (run.truncated=True), run остаётся partial success.
MAX_FOLDERS = _get_int("TDATA_CHECK_MAX_FOLDERS", 20)

#: Параллельных проверок внутри одного run (bounded concurrency).
MAX_CONCURRENCY = _get_int("TDATA_CHECK_MAX_CONCURRENCY", 3)

#: Timeout Telethon connect/auth на одну папку (секунды).
CONNECT_TIMEOUT_SEC = _get_float("TDATA_CHECK_CONNECT_TIMEOUT_SEC", 25.0)

#: Timeout SpamBot-check (секунды). Проверка идёт только через тот же proxy,
#: без отправки сообщений реальным пользователям.
SPAMCHECK_TIMEOUT_SEC = _get_float("TDATA_CHECK_SPAMCHECK_TIMEOUT_SEC", 15.0)

#: Префикс временных каталогов проверки (системный tmp, НЕ data/sessions).
TMP_PREFIX = "tdata-check-"

#: Сколько run-результатов держит in-memory store CP API (защита от роста).
RUN_STORE_CAP = _get_int("TDATA_CHECK_RUN_STORE_CAP", 100)


__all__ = [
    "MAX_ARCHIVE_BYTES",
    "MAX_FOLDERS",
    "MAX_CONCURRENCY",
    "CONNECT_TIMEOUT_SEC",
    "SPAMCHECK_TIMEOUT_SEC",
    "TMP_PREFIX",
    "RUN_STORE_CAP",
]
