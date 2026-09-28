"""Both configured owners can use the bot without widening access to others."""

import asyncio
from types import SimpleNamespace

from bot import config
from bot.handlers.accounts import navigation


def test_owner_allowlist_and_account_menu(monkeypatch):
    monkeypatch.setattr(config, "AUTHORIZED_OWNER_IDS", frozenset({111, 222}))
    assert config.is_authorized_user(111)
    assert config.is_authorized_user(222)
    assert not config.is_authorized_user(333)
    assert not config.is_authorized_user(None)

    calls = []

    async def edit(*_args, **_kwargs):
        calls.append("menu")

    class Callback:
        def __init__(self, user_id):
            self.from_user = SimpleNamespace(id=user_id)
            self.message = object()

        async def answer(self, *_args, **_kwargs):
            calls.append("answer")

    class State:
        async def clear(self):
            calls.append("clear")

    monkeypatch.setattr(navigation, "safe_edit_message", edit)
    asyncio.run(navigation.accounts_manage(Callback(222), State()))
    assert "menu" in calls

    calls.clear()
    asyncio.run(navigation.accounts_manage(Callback(333), State()))
    assert "menu" not in calls
