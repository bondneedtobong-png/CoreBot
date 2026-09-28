"""Safety stops and limits survive sessions and concurrent consumers."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database.models import (
    Account, AccountSafetyEvent, AccountSafetyState, AccountStatus, Base, BotCommand,
    OutboundQueue, Proxy,
)
from database.repositories import OutboundQueueRepository
from services.account_safety import pause_account, reserve_send
from workers import outbound_consumer
from control_plane.business.account_safety import router as safety_router
from control_plane.business.db import get_bot_db
from control_plane.deps import get_current_user, require_admin, require_operator_write


def test_shared_budget_and_persistent_stop(tmp_path):
    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'safety.db'}")
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with maker() as session:
                session.add(Account(
                    phone="+15550000001", session_name="safety-one",
                    status=AccountStatus.ACTIVE, daily_limit=2,
                ))
                await session.commit()
                account_id = await session.scalar(select(Account.id))

            async def reserve():
                async with maker() as session:
                    return await reserve_send(session, account_id, source="test")

            results = await asyncio.gather(reserve(), reserve(), reserve())
            assert sorted(result[0] for result in results) == [False, True, True]
            assert [reason for allowed, reason in results if not allowed] == ["daily_limit"]

            async with maker() as session:
                await pause_account(session, account_id, reason_code="peer_flood", source="mailing")
            async with maker() as session:
                assert await reserve_send(session, account_id, source="manual_queue") == (False, "peer_flood")
                state = await session.get(AccountSafetyState, account_id)
                assert state.state == "review_required"
                assert state.attempts_today == 2
                events = (await session.execute(select(AccountSafetyEvent))).scalars().all()
                assert len(events) == 1
                assert events[0].reason_code == "peer_flood"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_operator_pause_and_admin_resume_api():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(engine, expire_on_commit=False)
    with maker() as db:
        proxy = Proxy(name="safety-proxy", host="127.0.0.1", port=1080)
        db.add(proxy)
        db.flush()
        db.add(Account(
            phone="+15550000003", session_name="api-safety",
            status=AccountStatus.ACTIVE, daily_limit=5, proxy_id=proxy.id,
        ))
        db.commit()

    app = FastAPI()
    app.include_router(safety_router)

    def test_db():
        with maker() as db:
            yield db

    user = SimpleNamespace(username="operator", role="tenant_admin")
    app.dependency_overrides[get_bot_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[require_operator_write] = lambda: user
    app.dependency_overrides[require_admin] = lambda: user
    try:
        with TestClient(app) as client:
            assert client.get("/business/account-safety").json()[0]["state"] == "ready"
            paused = client.post("/business/account-safety/1/pause")
            assert paused.status_code == 200
            assert paused.json()["state"] == "review_required"
            assert client.get("/business/account-safety/1/events").json()[0]["event_type"] == "paused"
            resumed = client.post("/business/account-safety/1/resume")
            assert resumed.status_code == 200
            assert resumed.json()["state"] == "verifying"
            command_id = resumed.json()["command_id"]
            assert client.get(f"/business/account-safety/resume-commands/{command_id}").json()["status"] == "pending"
            assert client.post("/business/account-safety/1/resume").status_code == 409
            with maker() as db:
                assert db.get(BotCommand, command_id).command == "account_safety.resume"
    finally:
        engine.dispose()


def test_safety_view_does_not_call_exhausted_account_ready():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(engine, expire_on_commit=False)
    with maker() as db:
        db.add(Account(
            phone="+15550000004", session_name="no-budget",
            status=AccountStatus.ACTIVE, daily_limit=0,
        ))
        db.commit()

    app = FastAPI()
    app.include_router(safety_router)

    def test_db():
        with maker() as db:
            yield db

    app.dependency_overrides[get_bot_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(username="reader")
    try:
        with TestClient(app) as client:
            row = client.get("/business/account-safety").json()[0]
            assert row["state"] == "unavailable"
            assert row["reason_code"] == "daily_limit"
    finally:
        engine.dispose()


def test_observed_health_explains_score_and_7_30_day_counts():
    from datetime import timedelta
    from database.models import Client, Mailing, MailingLog
    from utils.time import utcnow_naive

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(engine, expire_on_commit=False)
    now = utcnow_naive()
    with maker() as db:
        db.add(Account(phone="+15550000024", session_name="health", status=AccountStatus.ACTIVE,
                       daily_limit=5))
        db.add(Client(username="owned_health"))
        db.add(Mailing(name="health sample", message_text="hello"))
        db.commit()
        db.add_all([
            MailingLog(mailing_id=1, account_id=1, client_id=1, success=True, sent_at=now),
            MailingLog(mailing_id=1, account_id=1, client_id=1, success=False,
                       sent_at=now - timedelta(days=10)),
            AccountSafetyEvent(account_id=1, event_type="paused", reason_code="flood_wait",
                               source="mailing", actor="system", created_at=now),
        ])
        db.commit()

    app = FastAPI()
    app.include_router(safety_router)

    def test_db():
        with maker() as db:
            yield db

    app.dependency_overrides[get_bot_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(username="reader")
    try:
        with TestClient(app) as client:
            health = client.get("/business/account-safety/1/health")
            assert health.status_code == 200
            body = health.json()
            assert body["readiness_score_1_10"] == 8
            assert body["score_deductions"] == {"proxy_not_assigned": 2}
            assert body["windows"]["7d"]["mailing_sent"] == 1
            assert body["windows"]["7d"]["mailing_failed"] == 0
            assert body["windows"]["30d"]["mailing_failed"] == 1
            assert body["windows"]["7d"]["safety_events_by_reason"] == {"flood_wait": 1}
            assert body["score_kind"] == "local_operational_readiness"
            assert client.get("/business/account-safety/999/health").status_code == 404
    finally:
        engine.dispose()


def test_queue_marks_gate_denial_as_failed_without_unknown_delivery(tmp_path, monkeypatch):
    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'queue-safety.db'}")
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with maker() as session:
                session.add(Account(phone="+15550000002", session_name="queue-safety"))
                await session.commit()
                account_id = await session.scalar(select(Account.id))
                queued = await OutboundQueueRepository.enqueue(
                    session, account_id=account_id, peer_user_id=123, text="hello"
                )
                queue_id = queued.id

            @asynccontextmanager
            async def scope():
                async with maker() as session:
                    yield session

            monkeypatch.setattr(outbound_consumer, "session_scope", scope)

            class Worker:
                is_connected = True

                async def send_message_with_typing(self, *_args, **_kwargs):
                    return False, None, "SAFETY_STOP: manual_pause", None

            manager = SimpleNamespace(workers={account_id: Worker()})
            async with maker() as session:
                row = await session.get(OutboundQueue, queue_id)
                await outbound_consumer.OutboundConsumer()._process_one(row, manager)
            async with maker() as session:
                row = await session.get(OutboundQueue, queue_id)
                assert row.status == "failed"
                assert row.error == "SAFETY_STOP: manual_pause"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_flood_wait_until_is_persisted_and_requires_review(tmp_path):
    async def scenario():
        from datetime import timedelta
        from services.account_safety import pause_account, reserve_send
        from utils.time import utcnow_naive

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'flood.db'}")
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with maker() as session:
                session.add(Account(
                    phone="+15550000005", session_name="flood", status=AccountStatus.ACTIVE,
                    daily_limit=10,
                ))
                await session.commit()
            until = utcnow_naive() + timedelta(seconds=60)
            async with maker() as session:
                await pause_account(
                    session, 1, reason_code="flood_wait", source="mailing",
                    state="cooling_down", resume_at=until,
                )
            async with maker() as session:
                state = await session.get(AccountSafetyState, 1)
                assert state.state == "cooling_down"
                assert state.resume_at == until
                assert await reserve_send(session, 1, source="manual_queue") == (False, "flood_wait")
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_slow_mode_blocks_only_affected_chat_and_survives_restart(tmp_path):
    async def scenario():
        from datetime import timedelta
        from database.models import AccountChatCooldown
        from services.account_safety import record_chat_cooldown
        from utils.time import utcnow_naive

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'slow.db'}")
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with maker() as session:
                session.add(Account(
                    phone="+15550000006", session_name="slow", status=AccountStatus.ACTIVE,
                    daily_limit=10,
                ))
                await session.commit()
            until = utcnow_naive() + timedelta(seconds=30)
            async with maker() as session:
                await record_chat_cooldown(session, 1, peer_ref="-10042", resume_at=until, source="chat")
            async with maker() as session:
                row = await session.get(AccountChatCooldown, (1, "-10042"))
                assert row.resume_at == until
                assert await reserve_send(session, 1, source="chat", peer_ref="-10042") == (False, "slow_mode")
            async with maker() as session:
                assert await reserve_send(session, 1, source="chat", peer_ref="-10043") == (True, "ok")
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_worker_serializes_flood_wait_and_blocks_second_rpc(tmp_path, monkeypatch):
    from pathlib import Path
    from telethon.errors import FloodWaitError
    import workers.manager as manager_module

    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'worker-flood.db'}")
        maker = async_sessionmaker(engine, expire_on_commit=False)

        @asynccontextmanager
        async def scope():
            async with maker() as session:
                yield session

        monkeypatch.setattr(manager_module, "session_scope", scope)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with maker() as session:
                account = Account(
                    phone="+15550000007", session_name="worker-flood",
                    status=AccountStatus.ACTIVE, daily_limit=10,
                )
                session.add(account)
                await session.commit()
                account_id = account.id

            class Client:
                calls = 0

                async def get_input_entity(self, peer):
                    return peer

                async def send_message(self, *_args, **_kwargs):
                    self.calls += 1
                    raise FloodWaitError(None, 60)

                async def disconnect(self):
                    pass

            worker = manager_module.Worker(account, Path("unused.session"))
            client = Client()
            worker.client = client
            worker.is_connected = True
            first, second = await asyncio.gather(
                worker.send_message_with_typing("@owned_chat", "one", use_typing=False),
                worker.send_message_with_typing("@owned_chat", "two", use_typing=False),
            )
            assert "FloodWait" in first[2]
            assert second[2] == "SAFETY_STOP: flood_wait"
            assert client.calls == 1
            assert worker.client is None
            async with maker() as session:
                state = await session.get(AccountSafetyState, account_id)
                assert state.state == "cooling_down"
                assert state.resume_at is not None
                assert state.resume_at > state.updated_at
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_existing_safety_tables_migrate_without_losing_events(tmp_path):
    from sqlalchemy import text
    from database.repository import Database

    async def scenario():
        path = tmp_path / "legacy-safety.db"
        url = f"sqlite+aiosqlite:///{path}"
        legacy = create_async_engine(url)
        try:
            async with legacy.begin() as conn:
                await conn.execute(text(
                    "CREATE TABLE account_safety_state (account_id INTEGER PRIMARY KEY, "
                    "state VARCHAR(20) NOT NULL, reason_code VARCHAR(64), source VARCHAR(32), "
                    "day_utc VARCHAR(10), attempts_today INTEGER NOT NULL, updated_at DATETIME NOT NULL, "
                    "reviewed_by VARCHAR(120), reviewed_at DATETIME)"
                ))
                await conn.execute(text(
                    "CREATE TABLE account_safety_events (id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, "
                    "event_type VARCHAR(24) NOT NULL, reason_code VARCHAR(64) NOT NULL, "
                    "source VARCHAR(32) NOT NULL, actor VARCHAR(120), created_at DATETIME NOT NULL)"
                ))
                await conn.execute(text(
                    "INSERT INTO account_safety_events VALUES "
                    "(1, 7, 'paused', 'manual_pause', 'operator', 'tester', '2026-09-25 10:00:00')"
                ))
        finally:
            await legacy.dispose()
        database = Database(url)
        try:
            await database.connect()
            await database._run_migrations()
            async with database.engine.connect() as conn:
                columns = {row[1] for row in (await conn.execute(text("PRAGMA table_info(account_safety_events)"))).all()}
                assert {"peer_ref", "resume_at"} <= columns
                row = (await conn.execute(text(
                    "SELECT reason_code, actor FROM account_safety_events WHERE id=1"
                ))).one()
                assert row == ("manual_pause", "tester")
                backup = (await conn.execute(text(
                    "SELECT count(*) FROM account_safety_events_pre_scoped_stop_backup"
                ))).scalar_one()
                assert backup == 1
        finally:
            await database.disconnect()

    asyncio.run(scenario())


def test_worker_slow_mode_does_not_stop_other_chat(tmp_path, monkeypatch):
    from pathlib import Path
    from telethon.errors import SlowModeWaitError
    import workers.manager as manager_module

    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'worker-slow.db'}")
        maker = async_sessionmaker(engine, expire_on_commit=False)

        @asynccontextmanager
        async def scope():
            async with maker() as session:
                yield session

        monkeypatch.setattr(manager_module, "session_scope", scope)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with maker() as session:
                account = Account(
                    phone="+15550000008", session_name="worker-slow",
                    status=AccountStatus.ACTIVE, daily_limit=10,
                )
                session.add(account)
                await session.commit()

            class Client:
                calls = []

                async def get_input_entity(self, peer):
                    return peer

                async def send_message(self, peer, *_args, **_kwargs):
                    self.calls.append(peer)
                    if peer == -10042:
                        raise SlowModeWaitError(None, 30)
                    return SimpleNamespace(id=77, peer_id=None)

            client = Client()
            worker = manager_module.Worker(account, Path("unused.session"))
            worker.client = client
            worker.is_connected = True
            first = await worker.send_message_with_typing(-10042, "one", use_typing=False)
            second = await worker.send_message_with_typing(-10042, "two", use_typing=False)
            other = await worker.send_message_with_typing(-10043, "three", use_typing=False)
            assert first[2] == "SLOWMODE_WAIT: 30 сек"
            assert second[2] == "SAFETY_STOP: slow_mode"
            assert other[:2] == (True, 77)
            assert client.calls == [-10042, -10043]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_invalid_session_during_peer_resolution_stops_account(tmp_path, monkeypatch):
    from pathlib import Path
    from telethon.errors import AuthKeyDuplicatedError
    import workers.manager as manager_module

    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'worker-auth.db'}")
        maker = async_sessionmaker(engine, expire_on_commit=False)

        @asynccontextmanager
        async def scope():
            async with maker() as session:
                yield session

        monkeypatch.setattr(manager_module, "session_scope", scope)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with maker() as session:
                account = Account(
                    phone="+15550000009", session_name="worker-auth",
                    status=AccountStatus.ACTIVE, daily_limit=10,
                )
                session.add(account)
                await session.commit()

            class Client:
                async def get_input_entity(self, _peer):
                    raise AuthKeyDuplicatedError(None)

                async def disconnect(self):
                    pass

            worker = manager_module.Worker(account, Path("unused.session"))
            worker.client = Client()
            worker.is_connected = True
            result = await worker.send_message_with_typing("@owned_chat", "test", use_typing=False)
            assert result[2] == "AUTH_INVALID: AuthKeyDuplicatedError"
            async with maker() as session:
                state = await session.get(AccountSafetyState, account.id)
                assert (state.state, state.reason_code) == ("needs_reauth", "auth_invalid")
            assert not worker.is_connected
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_operator_cannot_resume_before_cooldown_and_sees_chat_scope():
    from datetime import timedelta
    from database.models import AccountChatCooldown
    from utils.time import utcnow_naive

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(engine, expire_on_commit=False)
    until = utcnow_naive() + timedelta(seconds=60)
    with maker() as db:
        db.add(Account(
            phone="+15550000010", session_name="api-cooldown",
            status=AccountStatus.ACTIVE, daily_limit=5,
        ))
        db.flush()
        db.add(AccountSafetyState(
            account_id=1, state="cooling_down", reason_code="flood_wait",
            source="mailing", resume_at=until,
        ))
        db.add(AccountChatCooldown(
            account_id=1, peer_ref="-10042", reason_code="slow_mode",
            source="chat", resume_at=until,
        ))
        db.commit()

    app = FastAPI()
    app.include_router(safety_router)

    def test_db():
        with maker() as db:
            yield db

    user = SimpleNamespace(username="admin", role="tenant_admin")
    app.dependency_overrides[get_bot_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[require_admin] = lambda: user
    try:
        with TestClient(app) as client:
            row = client.get("/business/account-safety").json()[0]
            assert row["resume_at"] is not None
            cooldowns = client.get("/business/account-safety/1/cooldowns").json()
            assert cooldowns[0]["peer_ref"] == "-10042"
            assert cooldowns[0]["reason_code"] == "slow_mode"
            assert client.post("/business/account-safety/1/resume").status_code == 409
    finally:
        engine.dispose()
