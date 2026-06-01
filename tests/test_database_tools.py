"""Регрессия меню «База данных»: экспорт классов и удаление клиентов."""
from __future__ import annotations

import asyncio

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from database.models import (
    Base,
    Client,
    ClientClassCounter,
    ClientInteraction,
    ClientStatus,
)
from services.database import client_delete, client_export


def _engine():
    return create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def test_export_classes_and_delete_with_children():
    engine = _engine()
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def run():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with Session() as s:
            c_fresh = Client(username="fresh1", status=ClientStatus.NEW)
            c_alive = Client(
                username="alive1", status=ClientStatus.CONTACTED, telegram_user_id=111
            )
            c_bl = Client(username="bad1", status=ClientStatus.NEW)
            # Клиент без @username (после миграции username nullable) — ключ по tg id.
            c_null = Client(username=None, telegram_user_id=222, status=ClientStatus.NEW)
            s.add_all([c_fresh, c_alive, c_bl, c_null])
            await s.commit()
            for c in (c_fresh, c_alive, c_bl, c_null):
                await s.refresh(c)

            s.add_all([
                ClientClassCounter(client_id=c_alive.id, class_key="pulse", count=2),
                ClientClassCounter(client_id=c_bl.id, class_key="bl", count=1),
                ClientClassCounter(client_id=c_null.id, class_key="alive", count=1),
                ClientInteraction(client_id=c_bl.id, kind="pulse", direction="in"),
            ])
            await s.commit()

            fresh = {c.username for c in await client_export.export_fresh(s)}
            # NEW без негатива: fresh1 и c_null (alive — не негатив). bad1 (bl) исключён.
            assert "fresh1" in fresh
            assert None in fresh
            assert "bad1" not in fresh

            blk = {client_export.client_handle(c) for c in await client_export.export_blacklist(s)}
            assert "@bad1" in blk

            alive = {client_export.client_handle(c) for c in await client_export.export_alive(s)}
            assert "@alive1" in alive          # pulse
            assert "tg:222" in alive            # alive-класс, username=NULL

            # render: NULL username → tg:<id>
            txt = client_export.render_client_list_txt([c_null], "T")
            assert "tg:222" in txt and "# T: 1" in txt

            # Удаление по классу bl + проверка зачистки детей.
            ids = await client_delete.find_client_ids_by_class(s, "bl")
            assert c_bl.id in ids
            n = await client_delete.delete_clients(s, ids)
            assert n == len(ids)

            left = {c.username for c in await client_export.export_blacklist(s)}
            assert "bad1" not in left
            cc = await s.scalar(
                select(func.count(ClientClassCounter.id)).where(
                    ClientClassCounter.client_id == c_bl.id
                )
            )
            assert cc == 0
            ci = await s.scalar(
                select(func.count(ClientInteraction.id)).where(
                    ClientInteraction.client_id == c_bl.id
                )
            )
            assert ci == 0

    asyncio.run(run())
    asyncio.run(engine.dispose())


def test_find_client_ids_by_usernames_normalizes():
    engine = _engine()
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def run():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with Session() as s:
            s.add_all([
                Client(username="userone", status=ClientStatus.NEW),
                Client(username="usertwo", status=ClientStatus.NEW),
            ])
            await s.commit()
            # С @, разным регистром, мусором — должно нормализоваться.
            ids = await client_delete.find_client_ids_by_usernames(
                s, ["@UserOne", "usertwo", "  ", "missing"]
            )
            assert len(ids) == 2

    asyncio.run(run())
    asyncio.run(engine.dispose())
