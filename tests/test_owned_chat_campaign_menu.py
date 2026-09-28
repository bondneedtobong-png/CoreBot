"""The owned-chat flow is reachable from the mailing menu."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from bot.handlers import chat_campaign
from bot.keyboards.main import get_mailing_keyboard


class _State:
    def __init__(self):
        self.cleared = False

    async def clear(self):
        self.cleared = True


class _Callback:
    def __init__(self):
        self.from_user = SimpleNamespace(id=7)
        self.message = self
        self.edits = []
        self.answers = []

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


def test_owned_chat_campaign_opens_from_mailing_menu(monkeypatch):
    buttons = [button for row in get_mailing_keyboard().inline_keyboard for button in row]
    assert any(button.callback_data == "chat_campaign_open" for button in buttons)

    @asynccontextmanager
    async def scope():
        yield object()

    async def accounts(_session):
        return [SimpleNamespace(id=3, display_title="Own account")]

    monkeypatch.setattr(chat_campaign, "session_scope", scope)
    monkeypatch.setattr(chat_campaign.AccountRepository, "get_active", accounts)
    monkeypatch.setattr(chat_campaign, "is_authorized_user", lambda user_id: user_id == 7)
    callback = _Callback()
    state = _State()
    asyncio.run(chat_campaign.open_from_menu(callback, state))
    assert state.cleared
    assert "chat_campaign_account_3" in str(callback.edits[-1][1]["reply_markup"])

    callback.from_user.id = 8
    callback.edits.clear()
    asyncio.run(chat_campaign.open_from_menu(callback, _State()))
    assert callback.edits == []
    assert callback.answers[-1][1]["show_alert"] is True
