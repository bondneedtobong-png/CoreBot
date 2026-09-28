"""Daily AI reply availability belongs to each mailing."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy import text as sql_text
from sqlalchemy.orm import sessionmaker

from control_plane.business import mailings
from control_plane.business.db import get_bot_db
from control_plane.deps import get_current_user, require_operator_write
from database.models import Base, Mailing
from database.repository import Database
from services.neurochat import manager
from services.neurochat import incoming_service
from services.neurochat import post_actions
from workers import manager as worker_manager


UTC = timezone.utc


@pytest.mark.parametrize("start,end,instant,expected", [
    (540, 1020, "2026-09-27T08:59:00+00:00", False),
    (540, 1020, "2026-09-27T09:00:00+00:00", True),
    (540, 1020, "2026-09-27T16:59:59+00:00", True),
    (540, 1020, "2026-09-27T17:00:00+00:00", False),
    (1320, 120, "2026-09-27T21:59:00+00:00", False),
    (1320, 120, "2026-09-27T22:00:00+00:00", True),
    (1320, 120, "2026-09-28T01:59:00+00:00", True),
    (1320, 120, "2026-09-28T02:00:00+00:00", False),
])
def test_active_window_boundaries(start, end, instant, expected):
    mailing = SimpleNamespace(
        neuro_active_start_minute=start,
        neuro_active_end_minute=end,
        neuro_timezone="UTC",
    )
    assert manager.neuro_active_now(mailing, now_utc=datetime.fromisoformat(instant)) is expected


def test_active_window_uses_iana_timezone_and_rejects_bad_configuration():
    instant = datetime(2026, 9, 27, 6, 0, tzinfo=UTC)
    mailing = SimpleNamespace(
        neuro_active_start_minute=600,
        neuro_active_end_minute=660,
        neuro_timezone="Europe/Samara",
    )
    assert manager.neuro_active_now(mailing, now_utc=instant)
    mailing.neuro_timezone = "Invalid/Zone"
    assert not manager.neuro_active_now(mailing, now_utc=instant)
    mailing.neuro_timezone = "UTC"
    mailing.neuro_active_end_minute = None
    assert not manager.neuro_active_now(mailing, now_utc=instant)
    mailing.neuro_active_start_minute = None
    assert manager.neuro_active_now(mailing, now_utc=instant)


def test_patch_and_get_neuro_active_hours(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'hours.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    maker = sessionmaker(engine)
    with maker.begin() as db:
        db.add(Mailing(name="hours", message_text="hello"))

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
            assert client.get(url).json()["neuro_timezone"] == "UTC"
            updated = client.patch(url, json={
                "neuro_active_start_minute": 1320,
                "neuro_active_end_minute": 120,
                "neuro_timezone": "Europe/Samara",
            })
            assert updated.status_code == 200, updated.text
            assert updated.json()["neuro_active_start_minute"] == 1320
            assert updated.json()["neuro_active_end_minute"] == 120
            assert client.get(url).json()["neuro_timezone"] == "Europe/Samara"

            assert client.patch(url, json={"neuro_active_start_minute": None}).status_code == 422
            assert client.patch(url, json={"neuro_active_end_minute": 1320}).status_code == 422
            assert client.patch(url, json={"neuro_timezone": "Invalid/Zone"}).status_code == 422
            assert client.patch(url, json={"neuro_timezone": None}).status_code == 422
            assert client.patch(url, json={"neuro_active_start_minute": 1440}).status_code == 422
            assert client.get(url).json()["neuro_active_start_minute"] == 1320

            cleared = client.patch(url, json={
                "neuro_active_start_minute": None,
                "neuro_active_end_minute": None,
            })
            assert cleared.status_code == 200, cleared.text
            assert cleared.json()["neuro_active_start_minute"] is None
            assert cleared.json()["neuro_active_end_minute"] is None
            assert client.get(url).json()["neuro_active_start_minute"] is None
    finally:
        engine.dispose()


def test_outside_window_does_not_prepare_llm_request(monkeypatch):
    monkeypatch.setattr(manager, "can_process_incoming", AsyncMock(return_value=True))
    provider = AsyncMock()
    monkeypatch.setattr(manager, "resolve_provider_async", provider)
    mailing = SimpleNamespace(
        id=3, neurochat_enabled=True, neuro_active_start_minute=1,
        neuro_active_end_minute=2, neuro_timezone="Invalid/Zone",
    )
    worker = SimpleNamespace(is_connected=True, account=SimpleNamespace(id=5))
    client = SimpleNamespace(id=7)
    context, reason = asyncio.run(manager.prepare_incoming_context(
        object(), worker=worker, sender=None, client=client, mailing=mailing,
        text="hello", peer_uid=10,
    ))
    assert context is None
    assert reason == "neuro_outside_active_hours"
    provider.assert_not_awaited()


def test_neuro_active_hours_migration_is_idempotent(tmp_path):
    path = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with sessionmaker(engine).begin() as db:
        db.add(Mailing(name="old", message_text="hello"))
    with engine.begin() as conn:
        for column in (
            "neuro_active_start_minute", "neuro_active_end_minute", "neuro_timezone"
        ):
            conn.execute(sql_text(f"ALTER TABLE mailings DROP COLUMN {column}"))
    engine.dispose()

    async def migrate_twice():
        db = Database(f"sqlite+aiosqlite:///{path}")
        await db.connect()
        try:
            await db._run_migrations()
            async with db.engine.connect() as conn:
                columns = [row[1] for row in (
                    await conn.execute(sql_text("PRAGMA table_info(mailings)"))
                ).fetchall()]
                zone = (await conn.execute(sql_text(
                    "SELECT neuro_timezone FROM mailings WHERE id=1"
                ))).scalar_one()
                backup = (await conn.execute(sql_text(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='mailings_neuro_active_hours_backup'"
                ))).scalar_one()
                assert all(name in columns for name in (
                    "neuro_active_start_minute", "neuro_active_end_minute", "neuro_timezone"
                ))
                assert zone == "UTC"
                assert backup
        finally:
            await db.disconnect()

    asyncio.run(migrate_twice())


@pytest.mark.parametrize("source", ["neuro_reply", "neuro_link"])
def test_window_closing_during_typing_prevents_telegram_send(monkeypatch, source):
    state = {"open": True, "sent": 0}

    @asynccontextmanager
    async def fake_session_scope():
        yield object()

    async def allowed(*_args, **_kwargs):
        return True, "ok"

    async def fake_sleep(_seconds):
        state["open"] = False

    class FakeClient:
        async def get_input_entity(self, peer):
            return peer

        async def __call__(self, _request):
            return None

        async def send_message(self, *_args, **_kwargs):
            state["sent"] += 1
            return SimpleNamespace(id=1, peer_id=None)

    worker = worker_manager.Worker.__new__(worker_manager.Worker)
    worker._send_lock = asyncio.Lock()
    worker.account = SimpleNamespace(id=1)
    worker.client = FakeClient()
    worker.is_connected = True
    monkeypatch.setattr(worker_manager, "session_scope", fake_session_scope)
    monkeypatch.setattr(worker_manager, "check_account_gate", allowed)
    monkeypatch.setattr(worker_manager, "reserve_send", allowed)
    monkeypatch.setattr(worker_manager.asyncio, "sleep", fake_sleep)

    async def before_send():
        return state["open"]

    result = asyncio.run(worker.send_message_with_typing(
        123, "hello", typing_delay=1, use_typing=True,
        source=source, before_send=before_send,
    ))
    assert result[0] is False
    assert result[2] == "NEURO_WINDOW_CLOSED"
    assert state["sent"] == 0


def test_closed_window_does_not_record_assistant_reply(monkeypatch):
    recorded = []

    class FakeWorker:
        async def send_message_with_typing(self, *_args, **kwargs):
            assert not await kwargs["before_send"]()
            return False, None, "NEURO_WINDOW_CLOSED", None

    async def record(*args):
        recorded.append(args)

    monkeypatch.setattr(post_actions, "persist_sent_reply", record)
    monkeypatch.setattr(post_actions.telemetry_emitter, "emit_event", AsyncMock())

    async def closed():
        return False

    sent = asyncio.run(post_actions.send_and_record_reply(
        FakeWorker(), peer_uid=123, reply="hello", use_typing_neuro=False,
        account_id=1, client_id=2, before_send=closed,
    ))
    assert not sent
    assert recorded == []


@pytest.mark.parametrize("close_before_retry", [False, True])
def test_multi_step_path_rechecks_window_before_each_llm_call(monkeypatch, close_before_retry):
    client = SimpleNamespace(id=7)
    mailing = SimpleNamespace(id=3)
    account = SimpleNamespace(id=5)

    @asynccontextmanager
    async def fake_session_scope():
        yield object()

    async def get_client(*_args):
        return client

    async def get_mailing(*_args):
        return mailing

    async def prepare(*_args, **_kwargs):
        return SimpleNamespace(
            client=client, mailing=mailing, model="test", api_key="key",
            link_for_prompt="", link_for_send="", messages=[], generation={},
            use_typing_neuro=False, provider=object(),
        ), "ok"

    checks = 0

    async def window_open(_mailing_id):
        nonlocal checks
        checks += 1
        return checks == 1 if close_before_retry else False

    llm = AsyncMock(return_value=("[SEND_LINK]", None))
    monkeypatch.setattr(incoming_service, "session_scope", fake_session_scope)
    monkeypatch.setattr(incoming_service.ClientRepository, "get_by_telegram_user_id", get_client)
    monkeypatch.setattr(incoming_service.MailingLogRepository, "get_mailing_for_neuro_reply", get_mailing)
    monkeypatch.setattr(incoming_service, "prepare_incoming_context", prepare)
    monkeypatch.setattr(incoming_service, "_neuro_reply_allowed_now", window_open)
    monkeypatch.setattr(incoming_service, "generate_reply_with_retries_and_fallback", llm)
    event = SimpleNamespace(
        is_private=True, sender_id=123, message=SimpleNamespace(message="hello"),
        get_sender=AsyncMock(return_value=None),
    )
    worker = SimpleNamespace(client=object(), account=account)
    asyncio.run(incoming_service.handle_incoming(worker, event))
    assert llm.await_count == (1 if close_before_retry else 0)
