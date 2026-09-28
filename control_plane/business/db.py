"""
Sync-подключение к основной базе бота (corebot.db).

Используется веб-панелью только для CRUD/чтения бизнес-сущностей
(аккаунты, диалоги, очередь ручных отправок, аудит классов).
Сам бот продолжает работать через async-движок database/repository.py.
"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from control_plane.config import BOT_DATABASE_URL
from database.sqlite_pragmas import register_sqlite_pragmas


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


# Единые SQLite PRAGMA для всех engine (см. database/sqlite_pragmas.py).
# URL не меняем: только PRAGMA/retry (требование tools/validate_config.py).
if _resolved_url.startswith("sqlite"):
    register_sqlite_pragmas(bot_engine)


BotSession = sessionmaker(bind=bot_engine, autoflush=False, autocommit=False, future=True)


def get_bot_db():
    """FastAPI dependency: yields sync Session к corebot.db."""
    db = BotSession()
    try:
        yield db
        # Если endpoint забыл commit/rollback, откатываем висячую транзакцию.
        try:
            if db.in_transaction():
                db.rollback()
        except Exception:
            pass
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise
    finally:
        db.close()


def commit_sync(db, *, op_name: str = "cp-commit") -> None:
    """commit sync-сессии с busy_retry + rollback при любой ошибке.

    Покрывает ~50 мест business/*, где был голый db.commit() без retry:
    остаточные transient-гонки трёх писателей (бот/CP/парсер) закрывает
    busy_timeout + ограниченный retry, логические ошибки — rollback+raise.
    """
    from database.sqlite_pragmas import run_sync_with_busy_retry

    try:
        run_sync_with_busy_retry(lambda: db.commit(), op_name=op_name)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise


__all__ = ["bot_engine", "BotSession", "get_bot_db", "commit_sync"]
