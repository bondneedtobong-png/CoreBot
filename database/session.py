"""
Единая точка входа для AsyncSession (SQLAlchemy 2.x async).

Вместо связки db.engine.begin() + sessionmaker(conn) используйте:

    async with session_scope() as session:
        ...

Репозитории по-прежнему сами вызывают await session.commit() там, где нужно.
При необработанном исключении сессия откатывает незакоммиченные изменения.
"""
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from database.repository import db


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with db.async_session_maker() as session:
        try:
            yield session
        except BaseException:
            await session.rollback()
            raise
