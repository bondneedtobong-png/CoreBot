"""Resume only after the bot process freshly checks auth through the assigned proxy."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from control_plane.business.account_safety import router
from control_plane.business.db import get_bot_db
from control_plane.deps import get_current_user, require_admin, require_operator_write
from database.models import (
    Account, AccountSafetyEvent, AccountSafetyState, AccountStatus, Base, BotCommand, Proxy,
)
from workers import bot_command_consumer as consumer_module


def test_resume_verifies_auth_proxy_then_opens_gate(tmp_path, monkeypatch):
    db_path = tmp_path / "resume.db"
    sync_engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(sync_engine)
    sync_maker = sessionmaker(sync_engine, expire_on_commit=False)
    with sync_maker() as db:
        proxy = Proxy(name="checked-proxy", host="127.0.0.1", port=1080)
        db.add(proxy)
        db.flush()
        db.add(Account(
            phone="+15559990001", session_name="resume-check", proxy_id=proxy.id,
            status=AccountStatus.ERROR, daily_limit=10,
        ))
        db.flush()
        db.add(AccountSafetyState(
            account_id=1, state="needs_reauth", reason_code="auth_invalid",
            source="mailing",
        ))
        db.commit()

    app = FastAPI()
    app.include_router(router)

    def test_db():
        with sync_maker() as db:
            yield db

    user = SimpleNamespace(username="owner", role="tenant_admin")
    app.dependency_overrides[get_bot_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[require_admin] = lambda: user
    app.dependency_overrides[require_operator_write] = lambda: user

    with TestClient(app) as client:
        queued = client.post("/business/account-safety/1/resume")
        assert queued.status_code == 200
        command_id = queued.json()["command_id"]
        assert queued.json()["state"] == "verifying"

    async def scenario():
        async_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        async_maker = async_sessionmaker(async_engine, expire_on_commit=False)

        @asynccontextmanager
        async def scope():
            async with async_maker() as session:
                yield session

        monkeypatch.setattr(consumer_module, "session_scope", scope)

        async def working_proxy(_config):
            return True

        monkeypatch.setattr(consumer_module, "_check_assigned_proxy", working_proxy)

        class FakeClient:
            authorized = True
            checked = 0

            async def is_user_authorized(self):
                self.checked += 1
                return self.authorized

            async def get_me(self):
                self.checked += 1
                return SimpleNamespace(id=42)

        fake_client = FakeClient()
        worker = SimpleNamespace(
            proxy=proxy, is_connected=True, client=fake_client,
        )
        manager = SimpleNamespace(workers={1: worker})
        try:
            async with async_maker() as session:
                command = await session.get(BotCommand, command_id)
                await consumer_module.BotCommandConsumer()._verify_account_resume(
                    command, json.loads(command.args_json), manager,
                )
            assert fake_client.checked == 2
            with sync_maker() as db:
                assert db.get(AccountSafetyState, 1).state == "ready"
                assert db.get(Account, 1).status == AccountStatus.ACTIVE
                assert db.get(BotCommand, command_id).status == "done"
                events = db.execute(select(AccountSafetyEvent.event_type)).scalars().all()
                assert events[-1] == "resumed"

            with TestClient(app) as client:
                assert client.post("/business/account-safety/1/pause").status_code == 200
                second_id = client.post("/business/account-safety/1/resume").json()["command_id"]
            fake_client.authorized = False
            async with async_maker() as session:
                command = await session.get(BotCommand, second_id)
                await consumer_module.BotCommandConsumer()._verify_account_resume(
                    command, json.loads(command.args_json), manager,
                )
            with sync_maker() as db:
                assert db.get(AccountSafetyState, 1).state == "needs_reauth"
                assert db.get(BotCommand, second_id).status == "failed"

            with TestClient(app) as client:
                third_id = client.post("/business/account-safety/1/resume").json()["command_id"]
                assert client.post("/business/account-safety/1/pause").status_code == 200
            fake_client.authorized = True
            async with async_maker() as session:
                command = await session.get(BotCommand, third_id)
                await consumer_module.BotCommandConsumer()._verify_account_resume(
                    command, json.loads(command.args_json), manager,
                )
            with sync_maker() as db:
                assert db.get(AccountSafetyState, 1).state == "review_required"
                assert db.get(BotCommand, third_id).status == "failed"

            with TestClient(app) as client:
                fourth_id = client.post("/business/account-safety/1/resume").json()["command_id"]
            with sync_maker() as db:
                command = db.get(BotCommand, fourth_id)
                command.status = "processing"
                db.commit()
            await consumer_module.BotCommandConsumer()._recover_resume_after_restart()
            with sync_maker() as db:
                assert db.get(AccountSafetyState, 1).state == "review_required"
                assert db.get(BotCommand, fourth_id).error == "verification_interrupted"

            with TestClient(app) as client:
                fifth_id = client.post("/business/account-safety/1/resume").json()["command_id"]

            async def broken_proxy(_config):
                return False

            monkeypatch.setattr(consumer_module, "_check_assigned_proxy", broken_proxy)
            checks_before = fake_client.checked
            async with async_maker() as session:
                command = await session.get(BotCommand, fifth_id)
                await consumer_module.BotCommandConsumer()._verify_account_resume(
                    command, json.loads(command.args_json), manager,
                )
            assert fake_client.checked == checks_before
            with sync_maker() as db:
                state = db.get(AccountSafetyState, 1)
                assert (state.state, state.reason_code) == ("review_required", "proxy_unavailable")
                assert db.get(BotCommand, fifth_id).status == "failed"
        finally:
            await async_engine.dispose()

    try:
        asyncio.run(scenario())
    finally:
        sync_engine.dispose()
