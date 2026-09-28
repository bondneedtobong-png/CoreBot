"""Bounded, operator-scoped discovery of managed group messages."""
import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker
from telethon.tl.types import Channel, ChannelParticipant, ChannelParticipantCreator

import control_plane.business.engagement as api
import workers.bot_command_consumer as consumer
from control_plane.business.db import get_bot_db
from control_plane.deps import get_current_user, require_operator_write
from database.models import Account, AccountStatus, Base, BotCommand, Proxy, ProxyType
from services.engagement_guard import (
    EngagementTargetError, discover_managed_messages, parse_group_ref,
)


def _channel(*, admin=True, megagroup=True, username="managedgroup"):
    return Channel(id=123, title="Managed", photo=None, date=None,
                   megagroup=megagroup, broadcast=False, username=username)


class TelegramClient:
    def __init__(self, *, admin=True):
        self.admin = admin
        self.limit = None

    async def get_entity(self, peer):
        assert peer in ("managedgroup", -100123)
        return _channel()

    async def __call__(self, _request):
        member = (ChannelParticipantCreator(user_id=1, admin_rights=None) if self.admin
                  else ChannelParticipant(user_id=1, date=None))
        return SimpleNamespace(participant=member)

    async def get_me(self):
        return SimpleNamespace(id=1)

    async def is_user_authorized(self):
        return True

    async def get_messages(self, _entity, *, limit):
        self.limit = limit
        messages = [SimpleNamespace(id=i, sender_id=i + 10, out=False,
                                    raw_text=f"Human message {i}",
                                    sender=SimpleNamespace(bot=False))
                    for i in range(25, 0, -1)]
        messages[0].out = True
        messages[1].sender = SimpleNamespace(bot=True)
        messages[2].raw_text = ""
        messages[3].sender_id = 1
        return messages[:limit]


def test_group_ref_and_bounded_candidate_filtering():
    assert parse_group_ref("@managedgroup") == "managedgroup"
    assert parse_group_ref("https://t.me/c/123") == -100123
    for ref in ("https://t.me/othergroup", "https://t.me/c/123/5", "@x", "t.me/c/123"):
        with pytest.raises(EngagementTargetError, match="invalid_group_ref"):
            parse_group_ref(ref)
    client = TelegramClient()
    rows = asyncio.run(discover_managed_messages(client, "@managedgroup"))
    assert client.limit == 20
    assert len(rows) == 10
    assert rows[0] == {"message_id": 21, "source_text": "Human message 21",
                       "message_link": "https://t.me/managedgroup/21"}
    assert all(row["message_id"] not in (25, 24, 23, 22) for row in rows)


def test_discovery_denies_non_admin_and_hides_telegram_errors():
    with pytest.raises(EngagementTargetError, match="admin_required"):
        asyncio.run(discover_managed_messages(TelegramClient(admin=False), "@managedgroup"))

    class Broken(TelegramClient):
        async def get_entity(self, _peer):
            raise RuntimeError("secret proxy password")

    with pytest.raises(EngagementTargetError) as caught:
        asyncio.run(discover_managed_messages(Broken(), "@managedgroup"))
    assert str(caught.value) == "target_unavailable"


def test_discovery_api_and_worker_proxy_gate(tmp_path, monkeypatch):
    path = tmp_path / "discovery.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(engine, expire_on_commit=False)
    with maker() as db:
        db.add(Proxy(name="route", host="127.0.0.1", port=1080,
                     proxy_type=ProxyType.SOCKS5))
        db.flush()
        db.add(Account(phone="+15551112223", session_name="discovery-account",
                       status=AccountStatus.ACTIVE, daily_limit=10, proxy_id=1))
        db.commit()

    app = FastAPI()
    app.include_router(api.router)

    def test_db():
        with maker() as db:
            yield db

    current_user = SimpleNamespace(username="alice", role="tenant_admin")
    app.dependency_overrides[get_bot_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: current_user
    app.dependency_overrides[require_operator_write] = lambda: current_user
    with TestClient(app) as client:
        assert client.post("/business/engagement/discoveries", json={
            "account_id": 1, "group_ref": "https://t.me/c/123/5",
        }).status_code == 422
        response = client.post("/business/engagement/discoveries", json={
            "account_id": 1, "group_ref": "@managedgroup",
        })
        assert response.status_code == 202
        command_id = response.json()["id"]
        assert client.get(f"/business/engagement/discoveries/{command_id}").json()["messages"] == []
        current_user.username = "bob"
        assert client.get(f"/business/engagement/discoveries/{command_id}").status_code == 404
        current_user.username = "alice"

    async def scenario():
        async_engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        async_maker = async_sessionmaker(async_engine, expire_on_commit=False)

        @asynccontextmanager
        async def scope():
            async with async_maker() as db:
                yield db

        monkeypatch.setattr(consumer, "session_scope", scope)

        async def gate(*_args, **_kwargs):
            return True, "ok"

        async def proxy_check(_config):
            return True

        monkeypatch.setattr(consumer, "check_account_gate", gate)
        monkeypatch.setattr(consumer, "_check_assigned_proxy", proxy_check)
        telegram = TelegramClient()
        worker = SimpleNamespace(is_connected=True, client=telegram,
                                 proxy=SimpleNamespace(id=1, proxy_type=ProxyType.SOCKS5,
                                                       host="127.0.0.1", port=1080,
                                                       username=None, password=None))
        manager = SimpleNamespace(workers={1: worker})
        try:
            async with async_maker() as db:
                command = await db.get(BotCommand, command_id)
                command.status = "processing"
                await db.commit()
                await consumer.BotCommandConsumer()._discover_engagement(command, {
                    "account_id": 1, "group_ref": "@managedgroup",
                }, manager)
            with maker() as db:
                stored = db.get(BotCommand, command_id)
                assert stored.status == "done"
                assert len(json.loads(stored.args_json)["messages"]) == 10
            worker.proxy.id = 999
            with maker() as db:
                stored = db.get(BotCommand, command_id)
                stored.status = "processing"
                db.commit()
            async with async_maker() as db:
                command = await db.get(BotCommand, command_id)
                await consumer.BotCommandConsumer()._discover_engagement(command, {
                    "account_id": 1, "group_ref": "@managedgroup",
                }, manager)
            with maker() as db:
                assert db.get(BotCommand, command_id).error == "proxy_assignment_changed"
        finally:
            await async_engine.dispose()

    asyncio.run(scenario())
    with TestClient(app) as client:
        failed = client.get(f"/business/engagement/discoveries/{command_id}")
        assert failed.json() == {"id": command_id, "status": "failed",
                                 "error_code": "proxy_assignment_changed", "messages": []}
        with maker() as db:
            stored = db.get(BotCommand, command_id)
            stored.status = "done"
            stored.error = None
            db.commit()
        done = client.get(f"/business/engagement/discoveries/{command_id}")
        assert len(done.json()["messages"]) == 10
        assert done.json()["messages"][0]["message_link"] == "https://t.me/managedgroup/21"
    engine.dispose()
