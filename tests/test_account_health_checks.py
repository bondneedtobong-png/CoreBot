"""Read-only account diagnostics run through the bot-owned worker and persist facts."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from control_plane.business.account_safety import router
from control_plane.business import account_safety as safety_module
from control_plane.business.db import get_bot_db
from control_plane.deps import get_current_user, require_operator_write
from database.models import (
    Account, AccountHealthCheck, AccountSafetyState, AccountStatus, Base, BotCommand, Proxy,
)
from workers import bot_command_consumer as consumer_module


def _environment(tmp_path):
    path = tmp_path / "health.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(engine, expire_on_commit=False)
    with maker() as db:
        proxy = Proxy(name="assigned", host="127.0.0.1", port=1080)
        db.add(proxy)
        db.flush()
        db.add(Account(
            phone="+15559990002", session_name="health-check", proxy_id=proxy.id,
            status=AccountStatus.ACTIVE, daily_limit=10,
        ))
        db.flush()
        account_id = db.execute(select(Account.id)).scalar_one()
        db.add(AccountSafetyState(
            account_id=account_id, state="review_required",
            reason_code="manual_pause", source="operator",
        ))
        db.commit()

    app = FastAPI()
    app.include_router(router)

    def test_db():
        with maker() as db:
            yield db

    user = SimpleNamespace(username="operator", role="operator")
    app.dependency_overrides[get_bot_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[require_operator_write] = lambda: user
    return path, engine, maker, app, account_id


def _run_check(path, monkeypatch, command_id, manager, proxy_check):
    async def scenario():
        async_engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        async_maker = async_sessionmaker(async_engine, expire_on_commit=False)

        @asynccontextmanager
        async def scope():
            async with async_maker() as session:
                yield session

        monkeypatch.setattr(consumer_module, "session_scope", scope)
        monkeypatch.setattr(consumer_module, "_check_assigned_proxy", proxy_check)
        try:
            async with async_maker() as session:
                command = await session.get(BotCommand, command_id)
                await consumer_module.BotCommandConsumer()._perform_account_health_check(
                    command, json.loads(command.args_json), manager,
                )
        finally:
            await async_engine.dispose()

    asyncio.run(scenario())


def test_health_history_keeps_proxy_auth_and_local_gate_separate(tmp_path, monkeypatch):
    path, engine, maker, app, account_id = _environment(tmp_path)
    with maker() as db:
        proxy = db.get(Account, account_id).proxy

    class Client:
        authorized = True
        calls = 0

        async def is_user_authorized(self):
            self.calls += 1
            return self.authorized

        async def get_me(self):
            self.calls += 1
            return SimpleNamespace(id=42)

    client_obj = Client()
    worker = SimpleNamespace(proxy=proxy, is_connected=True, client=client_obj)
    manager = SimpleNamespace(workers={account_id: worker})

    async def working_proxy(_config):
        return True

    async def broken_proxy(_config):
        return False

    try:
        with TestClient(app) as client:
            queued = client.post(f"/business/account-safety/{account_id}/health-checks")
            assert queued.status_code == 202
            first_id = queued.json()["id"]
            first_command = queued.json()["command_id"]
            assert queued.json()["proxy_state"] == "unknown"
            assert queued.json()["auth_state"] == "unknown"
            assert client.post(f"/business/account-safety/{account_id}/health-checks").status_code == 409
            pending = client.get(f"/business/account-safety/{account_id}/health-checks").json()
            assert pending[0]["status"] == "pending"

        with maker() as db:
            db.get(BotCommand, first_command).status = "processing"
            db.commit()
        _run_check(path, monkeypatch, first_command, manager, working_proxy)
        with maker() as db:
            check = db.get(AccountHealthCheck, first_id)
            assert (check.status, check.proxy_state, check.auth_state) == ("completed", "ok", "ok")
            assert check.reason_code == "auth_proxy_verified"
            assert check.safety_state == "review_required"
            assert check.safety_reason_code == "manual_pause"
            assert db.get(AccountSafetyState, account_id).state == "review_required"
            assert db.get(BotCommand, first_command).status == "done"
        assert client_obj.calls == 2

        with TestClient(app) as client:
            second = client.post(f"/business/account-safety/{account_id}/health-checks")
            second_command = second.json()["command_id"]
        with maker() as db:
            db.get(BotCommand, second_command).status = "processing"
            db.commit()
        _run_check(path, monkeypatch, second_command, manager, broken_proxy)
        assert client_obj.calls == 2  # Proxy failure never reaches Telegram auth.

        with TestClient(app) as client:
            third = client.post(f"/business/account-safety/{account_id}/health-checks")
            third_command = third.json()["command_id"]
        client_obj.authorized = False
        with maker() as db:
            db.get(BotCommand, third_command).status = "processing"
            db.commit()
        _run_check(path, monkeypatch, third_command, manager, working_proxy)

        with TestClient(app) as client:
            history = client.get(f"/business/account-safety/{account_id}/health-checks").json()
        assert [item["reason_code"] for item in history] == [
            "auth_invalid", "proxy_unavailable", "auth_proxy_verified",
        ]
        assert history[0]["proxy_state"] == "ok" and history[0]["auth_state"] == "invalid"
        assert history[1]["proxy_state"] == "unavailable" and history[1]["auth_state"] == "unknown"
        assert all(item["safety_source"] == "local_corebot_state" for item in history)
    finally:
        engine.dispose()


def test_health_check_restart_and_error_leave_explainable_history(tmp_path, monkeypatch):
    path, engine, maker, app, account_id = _environment(tmp_path)
    try:
        with TestClient(app) as client:
            first = client.post(f"/business/account-safety/{account_id}/health-checks").json()

        async def scenario():
            async_engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
            async_maker = async_sessionmaker(async_engine, expire_on_commit=False)

            @asynccontextmanager
            async def scope():
                async with async_maker() as session:
                    yield session

            monkeypatch.setattr(consumer_module, "session_scope", scope)
            try:
                await consumer_module.BotCommandConsumer()._recover_health_checks_after_restart()
            finally:
                await async_engine.dispose()

        asyncio.run(scenario())
        with maker() as db:
            assert db.get(AccountHealthCheck, first["id"]).status == "pending"
            assert db.get(BotCommand, first["command_id"]).status == "pending"
            db.get(BotCommand, first["command_id"]).status = "processing"
            db.get(AccountHealthCheck, first["id"]).status = "processing"
            db.commit()
        asyncio.run(scenario())
        with maker() as db:
            check = db.get(AccountHealthCheck, first["id"])
            command = db.get(BotCommand, first["command_id"])
            assert (check.status, check.reason_code, check.finished_at is not None) == (
                "interrupted", "check_interrupted", True,
            )
            assert (command.status, command.error) == ("failed", "check_interrupted")

        with TestClient(app) as client:
            second = client.post(f"/business/account-safety/{account_id}/health-checks").json()
        with maker() as db:
            db.get(BotCommand, second["command_id"]).status = "processing"
            db.commit()

        async def working_proxy(_config):
            return True

        class BrokenManager:
            workers = {}

            async def ensure_worker(self, _account_id):
                raise RuntimeError("sensitive transport detail")

        async def error_scenario():
            async_engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
            async_maker = async_sessionmaker(async_engine, expire_on_commit=False)

            @asynccontextmanager
            async def scope():
                async with async_maker() as session:
                    yield session

            monkeypatch.setattr(consumer_module, "session_scope", scope)
            monkeypatch.setattr(consumer_module, "_check_assigned_proxy", working_proxy)
            monkeypatch.setattr(__import__("workers.manager", fromlist=["worker_manager"]),
                                "worker_manager", BrokenManager())
            try:
                async with async_maker() as session:
                    command = await session.get(BotCommand, second["command_id"])
                    await consumer_module.BotCommandConsumer()._process_one(command)
            finally:
                await async_engine.dispose()

        asyncio.run(error_scenario())
        with maker() as db:
            check = db.get(AccountHealthCheck, second["id"])
            command = db.get(BotCommand, second["command_id"])
            assert (check.status, check.reason_code) == ("failed", "check_error")
            assert (command.status, command.error) == ("failed", "check_error")
            assert check.finished_at is not None
            assert "sensitive" not in str(check.reason_code) + str(command.error)
    finally:
        engine.dispose()


def test_health_request_does_not_mask_unrelated_integrity_error(tmp_path, monkeypatch):
    _path, engine, _maker, app, account_id = _environment(tmp_path)

    def unrelated_failure(_db, *, op_name):
        raise IntegrityError("insert", {}, Exception("CHECK constraint failed: unrelated"))

    monkeypatch.setattr(safety_module, "commit_sync", unrelated_failure)
    try:
        with TestClient(app) as client, pytest.raises(IntegrityError):
            client.post(f"/business/account-safety/{account_id}/health-checks")
    finally:
        engine.dispose()
