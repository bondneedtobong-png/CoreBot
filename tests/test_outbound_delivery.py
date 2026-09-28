"""Outbound delivery never automatically repeats a possibly executed RPC."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from database.models import Account, Base, OutboundQueue
from database.repositories import OutboundQueueRepository as Queue
from workers import outbound_consumer as module


def test_claim_is_exclusive_and_cancel_cannot_win_after_claim(tmp_path):
    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'claim.db'}")
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with maker() as session:
                session.add(Account(phone="+11111111111", session_name="claim_acc"))
                await session.commit()
                account_id = await session.scalar(select(Account.id))
                row = await Queue.enqueue(session, account_id=account_id, peer_user_id=123, text="hello")
                queue_id = row.id

            async def claim():
                async with maker() as session:
                    return await Queue.claim_pending(session, queue_id)

            assert sorted(await asyncio.gather(claim(), claim())) == [False, True]
            async with maker() as session:
                assert not await Queue.cancel(session, queue_id)
                row = await session.get(OutboundQueue, queue_id)
                assert row.status == "sending"
                assert row.attempts == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_crash_recovery_requires_explicit_retry(tmp_path):
    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'recover.db'}")
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with maker() as session:
                session.add(Account(phone="+12222222222", session_name="recover_acc"))
                await session.commit()
                account_id = await session.scalar(select(Account.id))
                row = await Queue.enqueue(session, account_id=account_id, peer_user_id=123, text="hello")
                queue_id = row.id
                assert await Queue.claim_pending(session, queue_id)
            async with maker() as session:
                assert await Queue.recover_stale_sending(session, older_than_sec=0) == 1
                assert await Queue.fetch_pending_batch(session) == []
                row = await session.get(OutboundQueue, queue_id)
                assert row.status == "uncertain"
                assert await Queue.retry(session, queue_id)
                assert len(await Queue.fetch_pending_batch(session)) == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_send_success_and_uncertain_failure(tmp_path, monkeypatch):
    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'send.db'}")
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with maker() as session:
                session.add(Account(phone="+13333333333", session_name="send_acc"))
                await session.commit()
                account_id = await session.scalar(select(Account.id))
                first = await Queue.enqueue(session, account_id=account_id, peer_user_id=123, text="one")
                second = await Queue.enqueue(session, account_id=account_id, peer_user_id=124, text="two")
                first_id, second_id = first.id, second.id

            @asynccontextmanager
            async def scope():
                async with maker() as session:
                    yield session

            monkeypatch.setattr(module, "session_scope", scope)
            async def no_client(*_args):
                return None
            monkeypatch.setattr(module.ClientRepository, "get_by_telegram_user_id", no_client)
            monkeypatch.setattr(module.ClientRepository, "get_by_id", no_client)
            async def no_history(*_args):
                return None
            monkeypatch.setattr(module.NeuroChatRepository, "append", no_history)

            calls = []
            class Worker:
                is_connected = True
                async def send_message_with_typing(self, peer, text, **_kwargs):
                    calls.append((peer, text))
                    if text == "one":
                        return True, 999, None, peer
                    raise ConnectionError("RPC outcome unknown")

            manager = SimpleNamespace(workers={account_id: Worker()})
            consumer = module.OutboundConsumer()
            async with maker() as session:
                first = await session.get(OutboundQueue, first_id)
                second = await session.get(OutboundQueue, second_id)
                await consumer._process_one(first, manager)
                await consumer._process_one(second, manager)
            async with maker() as session:
                first = await session.get(OutboundQueue, first_id)
                second = await session.get(OutboundQueue, second_id)
                assert (first.status, first.telegram_message_id) == ("sent", 999)
                assert second.status == "uncertain"
                assert await Queue.fetch_pending_batch(session) == []
                await consumer._process_one(second, manager)
            assert calls == [(123, "one"), (124, "two")]
        finally:
            await engine.dispose()

    asyncio.run(scenario())
