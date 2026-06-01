"""Регрессия: массовая чистка прокси/аккаунтов и прокси-гейт connect_all."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from database.models import (
    Account,
    Base,
    Client,
    ClientInteraction,
    ClientStatus,
    NeuroChatMessage,
    Proxy,
    ProxyType,
)
from services.database import fleet_cleanup


def _engine():
    return create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def test_fleet_cleanup_proxies_and_accounts():
    engine = _engine()
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def run():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with Session() as s:
            p1 = Proxy(name="p1", host="1.1.1.1", port=1080, proxy_type=ProxyType.SOCKS5)
            p2 = Proxy(name="p2", host="2.2.2.2", port=1080, proxy_type=ProxyType.SOCKS5)
            s.add_all([p1, p2])
            await s.commit()
            for p in (p1, p2):
                await s.refresh(p)

            a1 = Account(phone="+1", session_name="s_a1", proxy_id=p1.id)
            a2 = Account(phone="+2", session_name="s_a2", proxy_id=None)
            c1 = Client(username="lead1", status=ClientStatus.NEW)
            s.add_all([a1, a2, c1])
            await s.commit()
            for x in (a1, a2, c1):
                await s.refresh(x)

            s.add_all([
                NeuroChatMessage(account_id=a1.id, peer_user_id=999, role="user", content="hi"),
                ClientInteraction(client_id=c1.id, account_id=a1.id, kind="pulse", direction="in"),
            ])
            await s.commit()

            # Подсчёты.
            assert await fleet_cleanup.count_proxies(s) == 2
            assert await fleet_cleanup.count_free_proxies(s) == 1  # p2
            assert await fleet_cleanup.count_accounts(s) == 2
            assert await fleet_cleanup.count_accounts_with_proxy(s) == 1  # a1

            # Удаляем свободные → остаётся только p1.
            assert await fleet_cleanup.delete_free_proxies(s) == 1
            assert await fleet_cleanup.count_proxies(s) == 1

            # Отвязка прокси у всех → a1 теряет прокси.
            assert await fleet_cleanup.detach_all_account_proxies(s) == 1
            assert await fleet_cleanup.count_accounts_with_proxy(s) == 0

            # Удаляем все прокси (p1).
            assert await fleet_cleanup.delete_all_proxies(s) == 1
            assert await fleet_cleanup.count_proxies(s) == 0

            names = await fleet_cleanup.list_account_session_names(s)
            assert set(names) == {"s_a1", "s_a2"}

            # Удаляем все аккаунты + дети.
            assert await fleet_cleanup.delete_all_accounts(s) == 2
            assert await fleet_cleanup.count_accounts(s) == 0
            assert int(await s.scalar(select(func.count(NeuroChatMessage.id)))) == 0
            # Клиент сохранён, но account_id у его interaction обнулён.
            assert int(await s.scalar(select(func.count(Client.id)))) == 1
            ci_acc = await s.scalar(select(ClientInteraction.account_id))
            assert ci_acc is None

    asyncio.run(run())
    asyncio.run(engine.dispose())


def test_connect_all_skips_dead_and_missing_proxy(monkeypatch):
    from workers.manager import WorkerManager

    class FakeWorker:
        def __init__(self, aid, proxy):
            self.account = SimpleNamespace(id=aid)
            self.proxy = proxy
            self.connected = False

        async def connect(self, quiet=False):
            self.connected = True
            return True

    wm = WorkerManager()
    wm.workers = {
        1: FakeWorker(1, object()),   # рабочий прокси
        2: FakeWorker(2, None),       # нет прокси
        3: FakeWorker(3, object()),   # мёртвый прокси
    }

    async def fake_precheck(self, *, timeout=8):
        return {1: True, 2: None, 3: False}

    monkeypatch.setattr(WorkerManager, "precheck_proxies", fake_precheck)

    summary = asyncio.run(wm.connect_all())
    assert summary["connected"] == 1
    assert summary["skipped_no_proxy"] == 1
    assert summary["skipped_dead_proxy"] == 1
    assert wm.workers[1].connected is True
    assert wm.workers[2].connected is False  # нет прокси — не подключали
    assert wm.workers[3].connected is False  # мёртвый прокси — не подключали

    # Без гейта подключаются все (старое поведение).
    for w in wm.workers.values():
        w.connected = False
    summary2 = asyncio.run(wm.connect_all(require_working_proxy=False))
    assert summary2["connected"] == 3
