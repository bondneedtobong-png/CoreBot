"""A mailing run cannot silently acquire new CRM contacts during execution."""

import asyncio
from contextlib import asynccontextmanager

from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from database.models import (
    Base, Client, ClientContactPermission, ClientStatus, Mailing,
    MailingRun, MailingStatus, MailingTestRecipient,
)
from database.repositories import ClientRepository, MailingRepository


def test_regular_run_freezes_membership_but_honors_later_opt_out(tmp_path):
    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'runs.db'}")
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with maker() as session:
                session.add_all([
                    Client(username="first", status=ClientStatus.NEW),
                    Client(username="second", status=ClientStatus.NEW),
                    Mailing(name="campaign", message_text="hello", status=MailingStatus.DRAFT),
                ])
                await session.commit()
                session.add_all([
                    ClientContactPermission(client_id=1, state="opt_in", source="signup", actor="owner"),
                    ClientContactPermission(client_id=2, state="opt_in", source="signup", actor="owner"),
                ])
                await session.commit()
                mailing = await session.get(Mailing, 1)
                run = await MailingRepository.create_run(session, mailing)
                assert run.audience_count == 2
                original_hash = run.config_sha256

                session.add(Client(username="late", status=ClientStatus.NEW))
                await session.commit()
                session.add(ClientContactPermission(
                    client_id=3, state="opt_in", source="signup", actor="owner",
                ))
                permission = await session.get(ClientContactPermission, 2)
                permission.state = "opt_out"
                mailing.message_text = "changed after run"
                await session.commit()

                queued = await ClientRepository.get_mailing_queue(session, mailing, run_id=run.id)
                assert [row.username for row in queued] == ["first"]
                assert (await session.get(MailingRun, run.id)).config_sha256 == original_hash
                await MailingRepository.record_run_result(session, run.id, success=True)
                await MailingRepository.record_run_result(session, run.id, success=False)
                await MailingRepository.finish_run(session, run.id, "completed")
                await session.refresh(run)
                assert (run.messages_sent, run.messages_failed, run.status) == (1, 1, "completed")
                next_run = await MailingRepository.create_run(session, mailing)
                assert next_run.audience_count == 2
                assert next_run.config_sha256 != original_hash
                next_queue = await ClientRepository.get_mailing_queue(
                    session, mailing, run_id=next_run.id,
                )
                assert [row.username for row in next_queue] == ["first", "late"]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_test_mode_run_freezes_membership_and_checks_permission(tmp_path):
    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test-runs.db'}")
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with maker() as session:
                session.add_all([
                    Client(username="owned_one", status=ClientStatus.NEW),
                    Client(username="owned_two", status=ClientStatus.NEW),
                    Mailing(name="test", message_text="hello", audience_mode="test"),
                ])
                await session.commit()
                session.add(MailingTestRecipient(mailing_id=1, client_id=1, username="owned_one"))
                session.add_all([
                    ClientContactPermission(client_id=1, state="opt_in", source="owned", actor="owner"),
                    ClientContactPermission(client_id=2, state="opt_in", source="owned", actor="owner"),
                ])
                await session.commit()
                mailing = await session.get(Mailing, 1)
                run = await MailingRepository.create_run(session, mailing)
                assert run.audience_count == 1
                session.add(MailingTestRecipient(mailing_id=1, client_id=2, username="owned_two"))
                await session.commit()
                assert [c.id for c in await ClientRepository.get_test_recipients_all(
                    session, 1, run_id=run.id,
                )] == [1]
                permission = await session.get(ClientContactPermission, 1)
                permission.state = "opt_out"
                await session.commit()
                assert await ClientRepository.get_test_recipients_all(
                    session, 1, run_id=run.id,
                ) == []
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_run_history_endpoint_uses_stored_snapshot():
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.pool import StaticPool
    from control_plane.business.db import get_bot_db
    from control_plane.business.mailings import router
    from control_plane.deps import get_current_user

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(engine, expire_on_commit=False)
    with maker() as db:
        db.add(Mailing(name="history", message_text="hello"))
        db.commit()
        db.add(MailingRun(
            mailing_id=1, audience_mode="classes", config_json="{}",
            config_sha256="a" * 64, audience_count=1, status="completed",
        ))
        db.commit()

    app = FastAPI()
    app.include_router(router)

    def test_db():
        with maker() as db:
            yield db

    app.dependency_overrides[get_bot_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(username="owner")
    try:
        with TestClient(app) as client:
            rows = client.get("/business/mailings/1/runs")
            assert rows.status_code == 200
            assert rows.json()[0]["audience_count"] == 1
            assert rows.json()[0]["config_sha256"] == "a" * 64
            assert client.get("/business/mailings/1/runs/1").status_code == 200
            assert client.get("/business/mailings/1/runs/999").status_code == 404
    finally:
        engine.dispose()


def test_restart_marks_unknown_run_interrupted_without_replaying(tmp_path, monkeypatch):
    import workers.bot_command_consumer as consumer_module

    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'recovery.db'}")
        maker = async_sessionmaker(engine, expire_on_commit=False)

        @asynccontextmanager
        async def scope():
            async with maker() as session:
                yield session

        monkeypatch.setattr(consumer_module, "session_scope", scope)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with maker() as session:
                session.add(Mailing(name="orphan", message_text="hello", status=MailingStatus.RUNNING))
                await session.commit()
                session.add(MailingRun(
                    mailing_id=1, audience_mode="classes", config_json="{}",
                    config_sha256="b" * 64, audience_count=1, status="running",
                ))
                await session.commit()

            await consumer_module.BotCommandConsumer()._recover_mailing_runs_after_restart()
            async with maker() as session:
                run = await session.get(MailingRun, 1)
                mailing = await session.get(Mailing, 1)
                assert run.status == "interrupted"
                assert run.finished_at is not None
                assert mailing.status == MailingStatus.ERROR
                assert run.messages_sent == 0
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_queued_start_freezes_preview_and_can_be_cancelled(tmp_path):
    import json
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from control_plane.business.db import get_bot_db
    from control_plane.business.mailings import router
    from control_plane.deps import get_current_user, require_operator_write
    from database.models import BotCommand, MailingRunRecipient

    engine = create_engine(f"sqlite:///{tmp_path / 'queued.db'}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(engine, expire_on_commit=False)
    with maker() as db:
        db.add_all([
            Client(username="first", status=ClientStatus.NEW),
            Client(username="second", status=ClientStatus.NEW),
            Mailing(name="campaign", message_text="first version", status=MailingStatus.DRAFT),
        ])
        db.commit()
        db.add_all([
            ClientContactPermission(client_id=1, state="opt_in", source="owned", actor="owner"),
            ClientContactPermission(client_id=2, state="opt_in", source="owned", actor="owner"),
        ])
        db.commit()

    app = FastAPI()
    app.include_router(router)

    def test_db():
        with maker() as db:
            yield db

    app.dependency_overrides[get_bot_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(username="owner")
    app.dependency_overrides[require_operator_write] = lambda: SimpleNamespace(username="owner")
    try:
        with TestClient(app) as client:
            started = client.post("/business/mailings/1/start")
            assert started.status_code == 200, started.text
            assert started.json()["status"] == "queued"
            command_id = started.json()["command_id"]
            with maker() as db:
                command = db.get(BotCommand, command_id)
                run_id = json.loads(command.args_json)["run_id"]
                run = db.get(MailingRun, run_id)
                assert run.status == "queued"
                assert run.audience_count == 2
                assert json.loads(run.config_json)["message_text"] == "first version"
                assert set(db.execute(select(MailingRunRecipient.client_id).where(
                    MailingRunRecipient.run_id == run_id
                )).scalars()) == {1, 2}
                db.add(Client(username="late", status=ClientStatus.NEW))
                db.commit()
                db.add(ClientContactPermission(
                    client_id=3, state="opt_in", source="owned", actor="owner",
                ))
                db.commit()
            assert client.get("/business/mailings/1").json()["queued_start"] is True
            assert client.post("/business/mailings/1/start").status_code == 409
            assert client.patch("/business/mailings/1", json={"message_text": "changed"}).status_code == 409
            cancelled = client.post("/business/mailings/1/cancel-start")
            assert cancelled.status_code == 200
            with maker() as db:
                assert db.get(BotCommand, command_id).status == "cancelled"
                assert db.get(MailingRun, run_id).status == "cancelled"
            assert client.get("/business/mailings/1").json()["queued_start"] is False
            assert client.post("/business/mailings/1/cancel-start").status_code == 409
            scheduled = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
            future = client.post("/business/mailings/1/start", json={"scheduled_at": scheduled})
            assert future.status_code == 200, future.text
            with maker() as db:
                next_command = db.get(BotCommand, future.json()["command_id"])
                next_run = db.get(MailingRun, json.loads(next_command.args_json)["run_id"])
                assert next_command.not_before is not None
                assert next_run.scheduled_at == next_command.not_before
                assert next_run.audience_count == 3
            assert client.post("/business/mailings/1/cancel-start").status_code == 200
            assert client.post("/business/mailings/1/start", json={"scheduled_at": "2026-01-01T00:00:00Z"}).status_code == 400
            assert client.post("/business/mailings/1/start", json={"scheduled_at": "2030-01-01T00:00:00"}).status_code == 400
    finally:
        engine.dispose()


def test_worker_rejects_changed_queued_config_before_telegram(tmp_path, monkeypatch):
    import workers.manager as manager_module

    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'changed.db'}")
        maker = async_sessionmaker(engine, expire_on_commit=False)

        @asynccontextmanager
        async def scope():
            async with maker() as session:
                yield session

        monkeypatch.setattr(manager_module, "session_scope", scope)
        async def no_telemetry(*args, **kwargs):
            return None
        monkeypatch.setattr(manager_module.telemetry_emitter, "emit_event", no_telemetry)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with maker() as session:
                mailing = Mailing(name="changed", message_text="old", status=MailingStatus.DRAFT)
                session.add(mailing)
                await session.commit()
                config = MailingRepository.run_config_json(mailing)
                import hashlib
                run = MailingRun(
                    mailing_id=mailing.id, audience_mode="classes", config_json=config,
                    config_sha256=hashlib.sha256(config.encode()).hexdigest(),
                    audience_count=0, status="queued",
                )
                session.add(run)
                await session.commit()
                run_id = run.id
                mailing.message_text = "new"
                await session.commit()
            manager = manager_module.WorkerManager()
            await manager.start_mailing(1, queued_run_id=run_id)
            async with maker() as session:
                assert (await session.get(MailingRun, run_id)).status == "rejected"
                assert (await session.get(Mailing, 1)).status == MailingStatus.DRAFT
            assert manager.is_mailing_busy() is False
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_restart_keeps_pending_snapshot_but_marks_claimed_start_interrupted(tmp_path, monkeypatch):
    import json
    import workers.bot_command_consumer as consumer_module
    from database.models import BotCommand

    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'queued-recovery.db'}")
        maker = async_sessionmaker(engine, expire_on_commit=False)

        @asynccontextmanager
        async def scope():
            async with maker() as session:
                yield session

        monkeypatch.setattr(consumer_module, "session_scope", scope)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with maker() as session:
                session.add_all([
                    Mailing(name="pending", message_text="hello"),
                    Mailing(name="claimed", message_text="hello"),
                ])
                await session.commit()
                session.add_all([
                    MailingRun(mailing_id=2, audience_mode="classes", config_json="{}",
                               config_sha256="a" * 64, status="queued"),
                    MailingRun(mailing_id=1, audience_mode="classes", config_json="{}",
                               config_sha256="b" * 64, status="queued"),
                ])
                await session.commit()
                session.add_all([
                    BotCommand(command="mailing.start", args_json=json.dumps({"mailing_id": 1, "run_id": 1}),
                               status="pending"),
                    BotCommand(command="mailing.start", args_json=json.dumps({"mailing_id": 2, "run_id": 2}),
                               status="processing"),
                ])
                await session.commit()
            await consumer_module.BotCommandConsumer()._recover_mailing_runs_after_restart()
            async with maker() as session:
                assert (await session.get(MailingRun, 1)).status == "queued"
                assert (await session.get(BotCommand, 1)).status == "pending"
                assert (await session.get(MailingRun, 2)).status == "interrupted"
                command = await session.get(BotCommand, 2)
                assert command.status == "failed"
                assert command.error == "interrupted_before_start"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_consumer_reports_changed_queued_config_as_failed_command(tmp_path, monkeypatch):
    import hashlib
    import json
    import workers.bot_command_consumer as consumer_module
    import workers.manager as manager_module
    from database.models import BotCommand

    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'claim-check.db'}")
        maker = async_sessionmaker(engine, expire_on_commit=False)

        @asynccontextmanager
        async def scope():
            async with maker() as session:
                yield session

        monkeypatch.setattr(consumer_module, "session_scope", scope)
        class FakeManager:
            def is_mailing_busy(self):
                raise AssertionError("changed config must not reach worker scheduling")
        monkeypatch.setattr(manager_module, "worker_manager", FakeManager())
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with maker() as session:
                mailing = Mailing(name="claim", message_text="original")
                session.add(mailing)
                await session.commit()
                config = MailingRepository.run_config_json(mailing)
                run = MailingRun(
                    mailing_id=1, audience_mode="classes", config_json=config,
                    config_sha256=hashlib.sha256(config.encode()).hexdigest(), status="queued",
                )
                session.add(run)
                await session.commit()
                command = BotCommand(command="mailing.start", status="processing",
                                     args_json=json.dumps({"mailing_id": 1, "run_id": run.id}))
                session.add(command)
                await session.commit()
                mailing.message_text = "changed"
                await session.commit()
            await consumer_module.BotCommandConsumer()._process_one(command)
            async with maker() as session:
                assert (await session.get(BotCommand, command.id)).status == "failed"
                assert (await session.get(BotCommand, command.id)).error == "config_changed_after_queue"
                assert (await session.get(MailingRun, run.id)).status == "rejected"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_scheduled_command_is_not_claimed_early(tmp_path, monkeypatch):
    from datetime import timedelta
    import workers.bot_command_consumer as consumer_module
    from database.models import BotCommand
    from utils.time import utcnow_naive

    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'scheduled.db'}")
        maker = async_sessionmaker(engine, expire_on_commit=False)

        @asynccontextmanager
        async def scope():
            async with maker() as session:
                yield session

        monkeypatch.setattr(consumer_module, "session_scope", scope)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with maker() as session:
                session.add(BotCommand(
                    command="mailing.start", args_json='{"mailing_id": 1}',
                    status="pending", not_before=utcnow_naive() + timedelta(hours=1),
                ))
                await session.commit()
            consumer = consumer_module.BotCommandConsumer()
            consumer._did_recover_safety = True
            consumer._did_recover_mailings = True
            consumer._did_recover_engagement = True
            assert await consumer._tick() == 0
            async with maker() as session:
                assert (await session.get(BotCommand, 1)).status == "pending"
        finally:
            await engine.dispose()

    asyncio.run(scenario())
