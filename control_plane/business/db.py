"""
Sync-подключение к основной базе бота (corebot.db).

Используется веб-панелью только для CRUD/чтения бизнес-сущностей
(аккаунты, диалоги, очередь ручных отправок, аудит классов).
Сам бот продолжает работать через async-движок database/repository.py.
"""
from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from control_plane.config import BOT_DATABASE_URL


def _normalize_url(url: str) -> str:
    """`sqlite+aiosqlite:///...` -> `sqlite:///...` для sync-движка."""
    if not url:
        return url
    if url.startswith("sqlite+aiosqlite"):
        return "sqlite" + url[len("sqlite+aiosqlite") :]
    return url


_resolved_url = _normalize_url(BOT_DATABASE_URL)

bot_engine = create_engine(
    _resolved_url,
    future=True,
    connect_args={"timeout": 30, "check_same_thread": False}
    if _resolved_url.startswith("sqlite")
    else {},
    pool_pre_ping=True,
)


@event.listens_for(bot_engine, "connect")
def _set_sqlite_pragma(dbapi_conn, _conn_record):
    if not _resolved_url.startswith("sqlite"):
        return
    cur = dbapi_conn.cursor()
    try:
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=15000")
        cur.execute("PRAGMA foreign_keys=ON")
    finally:
        cur.close()


BotSession = sessionmaker(bind=bot_engine, autoflush=False, autocommit=False, future=True)


def get_bot_db():
    """FastAPI dependency: yields sync Session к corebot.db."""
    db = BotSession()
    try:
        yield db
    finally:
        db.close()


__all__ = ["bot_engine", "BotSession", "get_bot_db"]
