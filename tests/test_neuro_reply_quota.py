"""Persistent quota across dialogs, sends, restarts, and local day boundaries."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import sessionmaker

from control_plane.business import mailings
from control_plane.business.db import get_bot_db
from control_plane.deps import get_current_user, require_operator_write
from database.models import Base, Mailing
from database.repository import Database
from database.sqlite_pragmas import register_async_sqlite_pragmas
from services.neurochat import post_actions
from services.neurochat import incoming_service
from services.neurochat.reply_quota import reserve_reply


def _setup(path, *, limit=1, zone="UTC"):
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with sessionmaker(engine).begin() as session:
        session.add(Mailing(
            name="quota", message_text="hello", neuro_daily_reply_limit=limit,
            neuro_timezone=zone,
        ))
    engine.dispose()


def _engine(path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}", connect_args={"timeout": 30})
    register_async_sqlite_pragmas(engine)
    return engine


def test_concurrent_reservations_across_independent_connections_and_restart(tmp_path):
    path = tmp_path / "quota.db"
    _setup(path, limit=2)

    async def run():
        engines = [_engine(path) for _ in range(8)]
        try:
            results = await asyncio.gather(*(
                reserve_reply(1, engine=engine) for engine in engines
            ))
            winners = [r for r in results if r is not None]
            assert len(winners) == 2
            await winners[0].mark_sent()
            # The second pending reservation survives an engine/process restart.
        finally:
            await asyncio.gather(*(engine.dispose() for engine in engines))

        restarted = _engine(path)
        try:
            assert await reserve_reply(1, engine=restarted) is None
            with create_engine(f"sqlite:///{path}").connect() as conn:
                assert conn.execute(text(
                    "SELECT sent_count, reserved_count FROM neuro_reply_daily_usage"
                )).one() == (1, 1)
        finally:
            await restarted.dispose()

    asyncio.run(run())


def test_local_date_boundary_and_release(tmp_path):
    path = tmp_path / "day.db"
    _setup(path, limit=1, zone="Europe/Samara")

    async def run():
        engine = _engine(path)
        try:
            before = datetime(2026, 9, 27, 19, 59, tzinfo=timezone.utc)
            after = datetime(2026, 9, 27, 20, 0, tzinfo=timezone.utc)
            first = await reserve_reply(1, engine=engine, now=before)
            assert first.local_date == "2026-09-27"
            assert await reserve_reply(1, engine=engine, now=before) is None
            await first.release()
            replacement = await reserve_reply(1, engine=engine, now=before)
            assert replacement is not None
            await replacement.mark_sent()
            assert await reserve_reply(1, engine=engine, now=before) is None
            next_day = await reserve_reply(1, engine=engine, now=after)
            assert next_day.local_date == "2026-09-28"
            await next_day.release()
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_text_and_link_count_only_after_successful_telegram_send(tmp_path, monkeypatch):
    path = tmp_path / "send.db"
    _setup(path, limit=1)
    monkeypatch.setattr(post_actions, "persist_sent_reply", AsyncMock())
    monkeypatch.setattr(post_actions.telemetry_emitter, "emit_event", AsyncMock())
    monkeypatch.setattr(post_actions.NeuroActionRepository, "create", AsyncMock())

    @asynccontextmanager
    async def fake_session_scope():
        yield object()

    monkeypatch.setattr(post_actions, "session_scope", fake_session_scope)

    class Worker:
        def __init__(self, ok):
            self.ok = ok

        async def send_message_with_typing(self, *_args, **_kwargs):
            return self.ok, 1 if self.ok else None, None, None

    async def run():
        engine = _engine(path)
        try:
            failed = await reserve_reply(1, engine=engine)
            assert not await post_actions.send_and_record_reply(
                Worker(False), peer_uid=5, reply="hello", use_typing_neuro=False,
                account_id=1, client_id=2, reservation=failed,
            )
            await failed.release()
            text_slot = await reserve_reply(1, engine=engine)
            assert await post_actions.send_and_record_reply(
                Worker(True), peer_uid=5, reply="hello", use_typing_neuro=False,
                account_id=1, client_id=2, reservation=text_slot,
            )
            assert await reserve_reply(1, engine=engine) is None
            # Fresh local date permits a link; the command uses the same quota.
            tomorrow = datetime(2030, 1, 1, tzinfo=timezone.utc)
            link_slot = await reserve_reply(1, engine=engine, now=tomorrow)
            assert await post_actions.process_send_link_command(
                Worker(True), cmd_send_link=True, link_for_send="https://example.org",
                mailing_id=1, account_id=1, client_id=2, peer_uid=5,
                reservation=link_slot,
            )
            assert await reserve_reply(1, engine=engine, now=tomorrow) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_typing_crosses_midnight_and_new_day_limit_is_rechecked(tmp_path):
    path = tmp_path / "midnight.db"
    _setup(path, limit=1, zone="Europe/Samara")

    async def run():
        engine = _engine(path)
        try:
            before = datetime(2026, 9, 27, 19, 59, tzinfo=timezone.utc)
            after = datetime(2026, 9, 27, 20, 0, tzinfo=timezone.utc)
            occupied = await reserve_reply(1, engine=engine, now=after)
            old = await reserve_reply(1, engine=engine, now=before)
            assert not await old.refresh_for_send(now=after)
            await old.release()
            await occupied.release()
            moved = await reserve_reply(1, engine=engine, now=before)
            assert await moved.refresh_for_send(now=after)
            assert moved.local_date == "2026-09-28"
            await moved.mark_sent()
            async with engine.connect() as conn:
                rows = (await conn.execute(text(
                    "SELECT local_date, sent_count, reserved_count "
                    "FROM neuro_reply_daily_usage ORDER BY local_date"
                ))).all()
            assert rows == [("2026-09-27", 0, 0), ("2026-09-28", 1, 0)]
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_reduced_limit_blocks_reserved_reply_before_send(tmp_path):
    path = tmp_path / "lowered.db"
    _setup(path, limit=2)

    async def run():
        engine = _engine(path)
        try:
            first = await reserve_reply(1, engine=engine)
            second = await reserve_reply(1, engine=engine)
            async with engine.begin() as conn:
                await conn.execute(text(
                    "UPDATE mailings SET neuro_daily_reply_limit=1 WHERE id=1"
                ))
            assert not await first.refresh_for_send()
            await first.release()
            assert await second.refresh_for_send()
            await second.mark_sent()
            assert await reserve_reply(1, engine=engine) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_slot_context_releases_on_exception(tmp_path, monkeypatch):
    path = tmp_path / "exception.db"
    _setup(path, limit=1)

    async def run():
        engine = _engine(path)
        try:
            async def reserve(mailing_id):
                return await reserve_reply(mailing_id, engine=engine)

            monkeypatch.setattr(incoming_service, "reserve_reply", reserve)
            mailing = SimpleNamespace(id=1, neuro_daily_reply_limit=1)
            with pytest.raises(RuntimeError, match="LLM failed"):
                async with incoming_service._reply_slot(mailing):
                    raise RuntimeError("LLM failed")
            replacement = await reserve_reply(1, engine=engine)
            assert replacement is not None
            await replacement.release()
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_migration_adds_default_to_legacy_mailing(tmp_path):
    path = tmp_path / "legacy.db"
    _setup(path)
    with create_engine(f"sqlite:///{path}").begin() as conn:
        conn.execute(text("ALTER TABLE mailings DROP COLUMN neuro_daily_reply_limit"))

    async def run():
        database = Database(f"sqlite+aiosqlite:///{path}")
        await database.connect()
        try:
            await database._run_migrations()
            async with database.engine.connect() as conn:
                assert (await conn.execute(text(
                    "SELECT neuro_daily_reply_limit FROM mailings WHERE id=1"
                ))).scalar_one() == 0
                assert (await conn.execute(text(
                    "SELECT COUNT(*) FROM mailings_neuro_reply_limit_backup"
                ))).scalar_one() == 1
        finally:
            await database.disconnect()

    asyncio.run(run())


def test_api_get_patch_and_validation(tmp_path):
    path = tmp_path / "api.db"
    _setup(path, limit=0)
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    maker = sessionmaker(engine)
    app = FastAPI()
    app.include_router(mailings.router)

    def session():
        with maker() as db:
            yield db

    def user():
        return SimpleNamespace(role="tenant_admin", username="owner")

    app.dependency_overrides[get_bot_db] = session
    app.dependency_overrides[get_current_user] = user
    app.dependency_overrides[require_operator_write] = user
    try:
        with TestClient(app) as client:
            url = "/business/mailings/1"
            assert client.get(url).json()["neuro_daily_reply_limit"] == 0
            response = client.patch(url, json={"neuro_daily_reply_limit": 3})
            assert response.status_code == 200, response.text
            assert client.get(url).json()["neuro_daily_reply_limit"] == 3
            for value in (-1, 10001, None, "wrong", True, 1.5):
                assert client.patch(url, json={"neuro_daily_reply_limit": value}).status_code == 422
            assert client.get(url).json()["neuro_daily_reply_limit"] == 3
    finally:
        engine.dispose()


def test_exhausted_limit_skips_llm(monkeypatch):
    client = SimpleNamespace(id=7)
    mailing = SimpleNamespace(id=3, neuro_daily_reply_limit=1)

    @asynccontextmanager
    async def fake_session_scope():
        yield object()

    async def prepare(*_args, **_kwargs):
        return SimpleNamespace(
            client=client, mailing=mailing, model="test", api_key="key",
            link_for_prompt="", link_for_send="", messages=[], generation={},
            use_typing_neuro=False, provider=object(),
        ), "ok"

    monkeypatch.setattr(incoming_service, "session_scope", fake_session_scope)
    monkeypatch.setattr(
        incoming_service.ClientRepository, "get_by_telegram_user_id",
        AsyncMock(return_value=client),
    )
    monkeypatch.setattr(
        incoming_service.MailingLogRepository, "get_mailing_for_neuro_reply",
        AsyncMock(return_value=mailing),
    )
    monkeypatch.setattr(incoming_service, "prepare_incoming_context", prepare)
    monkeypatch.setattr(incoming_service, "reserve_reply", AsyncMock(return_value=None))
    llm = AsyncMock(return_value=("hello", None))
    monkeypatch.setattr(incoming_service, "generate_reply_with_retries_and_fallback", llm)
    event = SimpleNamespace(
        is_private=True, sender_id=123, message=SimpleNamespace(message="hello"),
        get_sender=AsyncMock(return_value=None),
    )
    worker = SimpleNamespace(client=object(), account=SimpleNamespace(id=5))
    asyncio.run(incoming_service.handle_incoming(worker, event))
    llm.assert_not_awaited()
