"""The primary worker obeys the same proxy and session rules as import/parser."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from database.models import ProxyType
from workers import manager


class FakeClient:
    def __init__(self):
        self.connected = False

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def is_user_authorized(self):
        return True

    async def get_me(self):
        return SimpleNamespace(id=10, username="test", phone="+10000000000")


@pytest.mark.parametrize("proxy_type", [None, ProxyType.HTTP, ProxyType.MTProxy])
def test_worker_never_connects_without_socks5(tmp_path: Path, monkeypatch, proxy_type):
    created = []
    monkeypatch.setattr(manager, "create_telethon_client", lambda *_a, **_k: created.append(1))
    account = SimpleNamespace(id=1, session_name="account")
    proxy = None if proxy_type is None else SimpleNamespace(proxy_type=proxy_type)
    worker = manager.Worker(account, tmp_path / "account.session", proxy)

    assert asyncio.run(worker.connect()) is False
    assert created == []


def test_primary_workers_exclusively_own_session(tmp_path: Path, monkeypatch):
    clients = []

    def create_client(*_args, **_kwargs):
        client = FakeClient()
        clients.append(client)
        return client

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(manager, "create_telethon_client", create_client)
    monkeypatch.setattr(manager.telemetry_emitter, "emit_event", no_op)
    account = SimpleNamespace(id=1, session_name="account")
    proxy = SimpleNamespace(
        proxy_type=ProxyType.SOCKS5,
        host="127.0.0.1",
        port=1080,
        username=None,
        password=None,
        name="local",
    )
    first = manager.Worker(account, tmp_path / "account.session", proxy)
    second = manager.Worker(account, tmp_path / "account.session", proxy)
    monkeypatch.setattr(first, "_update_account_info", no_op)
    monkeypatch.setattr(second, "_update_account_info", no_op)
    monkeypatch.setattr(first, "_register_neuro_handler", lambda: None)
    monkeypatch.setattr(second, "_register_neuro_handler", lambda: None)

    async def scenario():
        assert await first.connect()
        monkeypatch.setitem(manager.worker_manager.workers, 1, first)
        borrowed = manager.account_worker_for_action(account, tmp_path / "account.session", proxy)
        assert borrowed is not first
        assert await borrowed.connect()
        assert await borrowed.disconnect()
        assert first.is_connected
        assert not await second.connect()
        assert len(clients) == 1
        assert await first.disconnect()
        assert await second.connect()
        assert await second.disconnect()

    asyncio.run(scenario())


def test_telethon_factory_rejects_direct_connection(tmp_path: Path):
    with pytest.raises(ValueError, match="SOCKS5"):
        manager.create_telethon_client(tmp_path / "account.session", None)
