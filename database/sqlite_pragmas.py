"""
Единый helper настройки SQLite PRAGMA и ограниченного retry для CoreBot.

Инвентаризация SQLite-подключений (пути/URL НЕ менять — tools/validate_config.py
требует абсолютных sqlite-путей в production, ExecStartPre блокирует старт иначе):

+--------------------------------+------------------------------------------+---------------------------+
| БД (файл)                      | Engine                                   | Процесс / модуль          |
+--------------------------------+------------------------------------------+---------------------------+
| data/corebot.db (async, bot)   | create_async_engine (aiosqlite)          | бот: database/repository  |
|                                | `db` (Database.connect)                  | .py                       |
+--------------------------------+------------------------------------------+---------------------------+
| data/corebot.db (sync, CP)     | create_engine (sync sqlite)              | Control Plane бизнес-CRUD:|
|                                | `bot_engine`                             | control_plane/business/   |
|                                |                                          | db.py                     |
+--------------------------------+------------------------------------------+---------------------------+
| data/control_plane.db (sync)   | create_engine (sync sqlite)              | Control Plane auth/jobs:  |
|                                | `engine`                                 | control_plane/database.py |
+--------------------------------+------------------------------------------+---------------------------+

Все три engine обязаны получать одинаковые PRAGMA через этот модуль:
journal_mode=WAL, foreign_keys=ON, busy_timeout=30000, wal_autocheckpoint=1000.

Миграционные `PRAGMA table_info(...)` в database/repository.py — интроспекция
схемы, не настройки подключения; helper их не касается.

Глобальный BEGIN IMMEDIATE запрещён без измерений: короткие конкурентные записи
идут через SQLite ON CONFLICT (см. workers/parser/storage.py, репозитории),
а остаточные transient-гонки — через ограниченный retry ниже.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Awaitable, Callable, Dict, Optional

from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from utils.logger import log

# Единый busy_timeout для всех engine: 30000 мс.
# Обоснование: берём максимум из ранее распределённых значений
# (async bot 30000 / sync CP 15000 / CP DB отсутствовал → effectively 0/5с
# драйвера). Один VPS, три писателя (бот, CP, парсер), длинные Telethon-операции
# и WAL-чекпоинты периодически держат write-lock секундами. busy_timeout —
# дешёвое ожидание на уровне sqlite-драйвера (для async — в треде aiosqlite,
# event loop не блокируется) и заведомо меньше SLO-бюджетов (рестарт ≤5 мин,
# RTO ≤60 мин). Уменьшать до 15000 нельзя: sync CP падал бы первым при
# конкуренции с ботом. Увеличивать сверх 30000 бессмысленно: остаток покрывает
# ограниченный retry (≤5 попыток) поверх.
SQLITE_BUSY_TIMEOUT_MS = 30000

# Чекпоинт WAL каждые 1000 страниц (~4 МБ при page_size=4096): WAL-файл не
# разрастается между чекпоинтами, читатели не видят вечно растущий -wal.
# Значение уже стояло на async bot-engine; фиксируем его для всех.
SQLITE_WAL_AUTOCHECKPOINT_PAGES = 1000

# Retry поверх busy_timeout: максимум попыток (включая первую).
SQLITE_BUSY_MAX_ATTEMPTS = 5

# Маркеры transient-ошибок SQLite. Только они ретраятся.
SQLITE_BUSY_MARKERS = (
    "database is locked",
    "database table is locked",
    "database is busy",
    "sqlite_busy",
)

# Простая счётчик-метрика retry (читается мониторингом задачи 08,
# инкрементируется только здесь).
SQLITE_BUSY_RETRY_STATS: Dict[str, int] = {
    "attempts": 0,   # всего вызовов через retry-helper
    "retries": 0,    # transient-повторов выполнено
    "exhausted": 0,  # исчерпаний лимита попыток
    "failed_fast": 0,  # немедленных пробросов (IntegrityError и др.)
}


def reset_retry_stats() -> None:
    for k in SQLITE_BUSY_RETRY_STATS:
        SQLITE_BUSY_RETRY_STATS[k] = 0


def apply_sqlite_pragmas(
    dbapi_connection: Any,
    *,
    busy_timeout_ms: int = SQLITE_BUSY_TIMEOUT_MS,
    wal_autocheckpoint_pages: int = SQLITE_WAL_AUTOCHECKPOINT_PAGES,
) -> None:
    """Выставить обязательные PRAGMA на сыром DBAPI-подключении.

    Вызывать только из connect-listener (вне транзакции): journal_mode=WAL
    нельзя менять внутри транзакции.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA wal_autocheckpoint={int(wal_autocheckpoint_pages)}")
    finally:
        cursor.close()


def _connect_listener(
    dbapi_connection: Any,
    _connection_record: Any,
    *,
    busy_timeout_ms: int,
    wal_autocheckpoint_pages: int,
) -> None:
    apply_sqlite_pragmas(
        dbapi_connection,
        busy_timeout_ms=busy_timeout_ms,
        wal_autocheckpoint_pages=wal_autocheckpoint_pages,
    )


def register_sqlite_pragmas(
    engine: Any,
    *,
    busy_timeout_ms: int = SQLITE_BUSY_TIMEOUT_MS,
    wal_autocheckpoint_pages: int = SQLITE_WAL_AUTOCHECKPOINT_PAGES,
) -> Any:
    """Подключить PRAGMA-listener к sync engine. Возвращает engine."""
    event.listen(
        engine,
        "connect",
        lambda dbapi_conn, rec: _connect_listener(
            dbapi_conn,
            rec,
            busy_timeout_ms=busy_timeout_ms,
            wal_autocheckpoint_pages=wal_autocheckpoint_pages,
        ),
    )
    return engine


def register_async_sqlite_pragmas(
    async_engine: Any,
    *,
    busy_timeout_ms: int = SQLITE_BUSY_TIMEOUT_MS,
    wal_autocheckpoint_pages: int = SQLITE_WAL_AUTOCHECKPOINT_PAGES,
) -> Any:
    """Подключить PRAGMA-listener к async engine (через sync-интерфейс).

    Тот же механизм, что раньше был инлайн в database/repository.py:
    event.listen(engine.sync_engine, "connect", ...).
    """
    return register_sqlite_pragmas(
        async_engine.sync_engine,
        busy_timeout_ms=busy_timeout_ms,
        wal_autocheckpoint_pages=wal_autocheckpoint_pages,
    )


def read_sqlite_pragmas(dbapi_connection: Any) -> Dict[str, Any]:
    """Прочитать фактические PRAGMA подключения (для тестов/диагностики)."""
    out: Dict[str, Any] = {}
    cursor = dbapi_connection.cursor()
    try:
        for name in ("journal_mode", "foreign_keys", "busy_timeout", "wal_autocheckpoint"):
            cursor.execute(f"PRAGMA {name}")
            row = cursor.fetchone()
            out[name] = row[0] if row else None
    finally:
        cursor.close()
    return out


def is_transient_sqlite_busy(exc: BaseException) -> bool:
    """True только для transient `database is locked/busy`.

    IntegrityError и любые логические ошибки — всегда False (не ретраить).
    """
    if isinstance(exc, IntegrityError):
        return False
    parts = [str(exc)]
    orig = getattr(exc, "orig", None)
    if orig is not None:
        parts.append(str(orig))
    cause = getattr(exc, "__cause__", None)
    if cause is not None:
        parts.append(str(cause))
    text = " ".join(parts).lower()
    return any(marker in text for marker in SQLITE_BUSY_MARKERS)


def _backoff_delay_sec(attempt: int, base_delay_sec: float, max_delay_sec: float) -> float:
    """Экспоненциальный backoff с jitter. attempt: 1-based номер повтора."""
    delay = min(max_delay_sec, base_delay_sec * (2 ** max(0, attempt - 1)))
    return delay + random.uniform(0, base_delay_sec)


async def run_with_busy_retry(
    fn: Callable[[], Awaitable[Any]],
    *,
    attempts: int = SQLITE_BUSY_MAX_ATTEMPTS,
    base_delay_sec: float = 0.05,
    max_delay_sec: float = 2.0,
    op_name: str = "sqlite-op",
) -> Any:
    """Выполнить async fn с retry только при transient SQLITE_BUSY.

    Лимит: attempts ≤ 5 по умолчанию, суммарное время сна ограничено
    (base 0.05с → не более ~0.8с + jitter). IntegrityError пробрасывается сразу.
    """
    attempts = max(1, min(int(attempts), SQLITE_BUSY_MAX_ATTEMPTS))
    SQLITE_BUSY_RETRY_STATS["attempts"] += 1
    last_exc: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001 — классификация ниже
            if not is_transient_sqlite_busy(exc):
                SQLITE_BUSY_RETRY_STATS["failed_fast"] += 1
                raise
            last_exc = exc
            if attempt >= attempts:
                break
            SQLITE_BUSY_RETRY_STATS["retries"] += 1
            delay = _backoff_delay_sec(attempt, base_delay_sec, max_delay_sec)
            log.warning(
                f"SQLite busy ({op_name}): попытка {attempt}/{attempts}, "
                f"повтор через {delay:.2f}с: {exc}"
            )
            await asyncio.sleep(delay)
    SQLITE_BUSY_RETRY_STATS["exhausted"] += 1
    assert last_exc is not None
    raise last_exc


def run_sync_with_busy_retry(
    fn: Callable[[], Any],
    *,
    attempts: int = SQLITE_BUSY_MAX_ATTEMPTS,
    base_delay_sec: float = 0.05,
    max_delay_sec: float = 2.0,
    op_name: str = "sqlite-op",
) -> Any:
    """Sync-вариант retry только при transient SQLITE_BUSY (для CP-сессий)."""
    attempts = max(1, min(int(attempts), SQLITE_BUSY_MAX_ATTEMPTS))
    SQLITE_BUSY_RETRY_STATS["attempts"] += 1
    last_exc: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — классификация ниже
            if not is_transient_sqlite_busy(exc):
                SQLITE_BUSY_RETRY_STATS["failed_fast"] += 1
                raise
            last_exc = exc
            if attempt >= attempts:
                break
            SQLITE_BUSY_RETRY_STATS["retries"] += 1
            delay = _backoff_delay_sec(attempt, base_delay_sec, max_delay_sec)
            log.warning(
                f"SQLite busy ({op_name}): попытка {attempt}/{attempts}, "
                f"повтор через {delay:.2f}с: {exc}"
            )
            time.sleep(delay)
    SQLITE_BUSY_RETRY_STATS["exhausted"] += 1
    assert last_exc is not None
    raise last_exc


async def execute_with_busy_retry(session: Any, stmt: Any, op_name: str = "execute") -> Any:
    """session.execute с retry только при transient SQLITE_BUSY."""
    return await run_with_busy_retry(lambda: session.execute(stmt), op_name=op_name)


async def commit_with_busy_retry(session: Any, op_name: str = "commit") -> None:
    """session.commit с retry только при transient SQLITE_BUSY."""
    await run_with_busy_retry(lambda: session.commit(), op_name=op_name)


__all__ = [
    "SQLITE_BUSY_TIMEOUT_MS",
    "SQLITE_WAL_AUTOCHECKPOINT_PAGES",
    "SQLITE_BUSY_MAX_ATTEMPTS",
    "SQLITE_BUSY_MARKERS",
    "SQLITE_BUSY_RETRY_STATS",
    "reset_retry_stats",
    "apply_sqlite_pragmas",
    "register_sqlite_pragmas",
    "register_async_sqlite_pragmas",
    "read_sqlite_pragmas",
    "is_transient_sqlite_busy",
    "run_with_busy_retry",
    "run_sync_with_busy_retry",
    "execute_with_busy_retry",
    "commit_with_busy_retry",
]
