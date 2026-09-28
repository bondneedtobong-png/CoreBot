"""Proxy failover keeps an account on an unoccupied proxy in its own list."""

import asyncio
import time
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from database.models import Account, Base, Proxy, ProxyGroup, ProxyType
from services.proxy_rotation import claim_proxy, is_transport_error, rotation_candidates
from utils.time import utcnow_naive
from workers import manager as manager_module


def test_only_transport_errors_trigger_rotation():
    assert is_transport_error(OSError("Host unreachable"))
    assert is_transport_error("ProxyError: connection refused")
    assert not is_transport_error("FloodWait: wait 100 seconds")
    assert not is_transport_error("unauthorized")


async def _database(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rotation.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    return engine, maker


async def _seed(maker):
    async with maker() as session:
        group = ProxyGroup(name="colombia", purpose="ACCOUNT_RUNTIME", rr_cursor=0)
        other_group = ProxyGroup(name="elsewhere", purpose="ACCOUNT_RUNTIME")
        check_group = ProxyGroup(name="check", purpose="TDATA_CHECK")
        session.add_all([group, other_group, check_group])
        await session.flush()
        proxies = [
            Proxy(name=f"proxy-{number}", group_id=group.id, host="127.0.0.1",
                  port=10000 + number, proxy_type=ProxyType.SOCKS5,
                  is_active=True, is_working=True)
            for number in range(1, 5)
        ]
        proxies.extend([
            Proxy(name="other-list", group_id=other_group.id, host="127.0.0.1",
                  port=10005, proxy_type=ProxyType.SOCKS5, is_active=True),
            Proxy(name="check-list", group_id=check_group.id, host="127.0.0.1",
                  port=10006, proxy_type=ProxyType.SOCKS5, is_active=True),
        ])
        session.add_all(proxies)
        await session.flush()
        first = Account(phone="+10000000001", session_name="first", proxy_id=proxies[0].id)
        second = Account(phone="+10000000002", session_name="second", proxy_id=proxies[2].id)
        session.add_all([first, second])
        await session.commit()
        return group.id, first.id, second.id, [p.id for p in proxies]


def test_rotation_candidates_and_atomic_claim(tmp_path):
    async def scenario():
        engine, maker = await _database(tmp_path)
        try:
            group_id, first_id, second_id, proxy_ids = await _seed(maker)
            async with maker() as session:
                rows = await rotation_candidates(session, group_id=group_id)
                assert {p.id for p in rows} == {proxy_ids[1], proxy_ids[3]}
            async with maker() as session:
                assert not await claim_proxy(
                    session, account_id=first_id, expected_proxy_id=proxy_ids[0],
                    candidate_id=proxy_ids[2], group_id=group_id,
                )  # Already occupied by another account.
            async with maker() as session:
                assert not await claim_proxy(
                    session, account_id=first_id, expected_proxy_id=proxy_ids[0],
                    candidate_id=proxy_ids[4], group_id=group_id,
                )  # Different list.
            async with maker() as session:
                assert await claim_proxy(
                    session, account_id=first_id, expected_proxy_id=proxy_ids[0],
                    candidate_id=proxy_ids[1], group_id=group_id,
                )
            async with maker() as session:
                assert not await claim_proxy(
                    session, account_id=second_id, expected_proxy_id=proxy_ids[2],
                    candidate_id=proxy_ids[1], group_id=group_id,
                )
                assert not await claim_proxy(
                    session, account_id=first_id, expected_proxy_id=proxy_ids[0],
                    candidate_id=proxy_ids[3], group_id=group_id,
                )  # Stale compare-and-swap.
                accounts = list((await session.scalars(select(Account))).all())
                assert len({a.proxy_id for a in accounts}) == len(accounts)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_recently_failed_proxy_is_skipped_then_retried(tmp_path):
    async def scenario():
        engine, maker = await _database(tmp_path)
        try:
            group_id, _, _, proxy_ids = await _seed(maker)
            async with maker() as session:
                failed = await session.get(Proxy, proxy_ids[1])
                failed.is_working = False
                failed.last_checked = utcnow_naive()
                await session.commit()
            async with maker() as session:
                assert [p.id for p in await rotation_candidates(session, group_id=group_id)] == [proxy_ids[3]]
            async with maker() as session:
                failed = await session.get(Proxy, proxy_ids[1])
                failed.last_checked = utcnow_naive() - timedelta(minutes=11)
                await session.commit()
            async with maker() as session:
                assert {p.id for p in await rotation_candidates(session, group_id=group_id)} == {
                    proxy_ids[1], proxy_ids[3],
                }
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_thousand_proxy_list_uses_bounded_disjoint_batches(tmp_path):
    async def scenario():
        engine, maker = await _database(tmp_path)
        try:
            async with maker() as session:
                group = ProxyGroup(name="large-list", purpose="ACCOUNT_RUNTIME", rr_cursor=0)
                session.add(group)
                await session.flush()
                session.add_all([
                    Proxy(name=f"large-{index}", group_id=group.id,
                          host="127.0.0.1", port=10000 + index,
                          proxy_type=ProxyType.SOCKS5, is_active=True,
                          is_working=True)
                    for index in range(1000)
                ])
                await session.commit()
                group_id = group.id
            async with maker() as session:
                first = await rotation_candidates(session, group_id=group_id)
            async with maker() as session:
                second = await rotation_candidates(session, group_id=group_id)
            assert len(first) == len(second) == 12
            assert {p.id for p in first}.isdisjoint({p.id for p in second})
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_runtime_disconnect_tries_next_free_proxy(tmp_path, monkeypatch):
    async def scenario():
        engine, maker = await _database(tmp_path)
        try:
            group_id, first_id, second_id, proxy_ids = await _seed(maker)
            monkeypatch.setattr(manager_module, "session_scope", lambda: maker())

            async def no_op(*_args, **_kwargs):
                return None

            monkeypatch.setattr(manager_module.telemetry_emitter, "emit_event", no_op)
            async with maker() as session:
                account = await manager_module.AccountRepository.get_by_id(session, first_id)

            class FakeWorker:
                def __init__(self):
                    self.account = account
                    self.proxy = account.proxy
                    self.client = SimpleNamespace(is_connected=lambda: False)
                    self.is_connected = True
                    self.proxy_transport_failed = True
                    self.last_connect_error = None
                    self.tried = []

                async def disconnect(self):
                    self.is_connected = False
                    return True

                async def connect(self, *, quiet=False):
                    self.tried.append(self.proxy.id)
                    if self.proxy.id == proxy_ids[1]:
                        self.last_connect_error = OSError("Host unreachable")
                        return False
                    self.last_connect_error = None
                    self.proxy_transport_failed = False
                    self.is_connected = True
                    self.client = SimpleNamespace(is_connected=lambda: True)
                    return True

            worker = FakeWorker()
            pool = manager_module.WorkerManager()
            pool.workers[first_id] = worker
            pool._proxy_lost_since[first_id] = time.monotonic() - 21
            await pool.check_proxy_health_once()
            assert worker.tried == [proxy_ids[1], proxy_ids[3]]
            assert worker.is_connected
            async with maker() as session:
                first = await session.get(Account, first_id)
                second = await session.get(Account, second_id)
                failed = await session.get(Proxy, proxy_ids[1])
                assert first.proxy_id == proxy_ids[3]
                assert second.proxy_id == proxy_ids[2]
                assert failed.is_working is False
        finally:
            await engine.dispose()

    asyncio.run(scenario())
