import asyncio
from types import SimpleNamespace

from bot.handlers.managed_reactions import (
    EMOJIS,
    PAGE_SIZE,
    _account_keyboard,
    _emoji_keyboard,
    _is_message_link,
)
from bot.keyboards.main import get_warmup_menu_keyboard


def test_warmup_menu_has_managed_reaction_entry():
    buttons = [button for row in get_warmup_menu_keyboard().inline_keyboard for button in row]
    assert any(button.callback_data == "managed_reaction_start" for button in buttons)


def test_account_keyboard_paginates_by_ten_and_offers_cancel():
    accounts = [SimpleNamespace(id=i, list_label=None, username=f"user{i}", phone="") for i in range(1, 13)]
    first = _account_keyboard(accounts, 0).inline_keyboard
    assert len([row for row in first if row[0].callback_data.startswith("managed_reaction_account_")]) == PAGE_SIZE
    assert "managed_reaction_page_1" in [button.callback_data for button in first[-3]]
    assert first[-2][0].callback_data == "managed_reaction_history"
    assert first[-1][0].callback_data == "managed_reaction_cancel"

    second = _account_keyboard(accounts, 1).inline_keyboard
    account_buttons = [row[0] for row in second if row[0].callback_data.startswith("managed_reaction_account_")]
    assert len(account_buttons) == 2
    assert account_buttons[0].callback_data == "managed_reaction_account_11"


def test_emoji_menu_is_restricted_to_whitelist():
    buttons = _emoji_keyboard().inline_keyboard[0]
    assert [button.text for button in buttons] == list(EMOJIS)
    assert [button.callback_data for button in buttons] == [
        "managed_reaction_emoji_0",
        "managed_reaction_emoji_1",
        "managed_reaction_emoji_2",
    ]


def test_message_link_requires_direct_tme_message_path():
    assert _is_message_link("https://t.me/mychannel/123")
    assert _is_message_link("https://t.me/c/123456789/321")
    assert not _is_message_link("https://t.me/mychannel")
    assert not _is_message_link("https://t.me/mychannel/123?single")
    assert not _is_message_link("http://t.me/mychannel/123")
    assert not _is_message_link("https://t.me/abcd/123")
    assert not _is_message_link("https://example.com/mychannel/123")


class State:
    def __init__(self):
        self.values = {}
        self.current = None

    async def clear(self):
        self.values = {}
        self.current = None

    async def set_state(self, value):
        self.current = getattr(value, "state", value)

    async def get_state(self):
        return self.current

    async def update_data(self, **values):
        self.values.update(values)

    async def get_data(self):
        return dict(self.values)


class TelegramMessage:
    def __init__(self):
        self.from_user = SimpleNamespace(id=7)
        self.edits = []

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


class Callback:
    def __init__(self, data, user_id=7):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = TelegramMessage()
        self.answers = []

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))


def test_preview_requires_confirmation_before_single_send(monkeypatch):
    from bot.handlers import managed_reactions as flow

    calls = []

    async def preview(account_id, link, emoji):
        calls.append(("preview", account_id, link, emoji))
        return {
            "chat_title": "My channel", "peer_id": 987, "message_id": 12,
            "text_excerpt": "Hello", "account_name": "acct", "link": link,
            "emoji": emoji,
        }

    async def send(account_id, link, emoji, actor_id, *, expected_peer_id=None):
        calls.append(("send", account_id, link, emoji, actor_id, expected_peer_id))
        return {"status": "sent", "reason": "ok"}

    monkeypatch.setattr(flow, "is_authorized_user", lambda user_id: user_id == 7)
    monkeypatch.setattr(flow, "preview_managed_reaction", preview)
    monkeypatch.setattr(flow, "send_managed_reaction", send)
    state = State()
    state.current = flow.ManagedReactionFlow.waiting_emoji.state
    state.values = {"account_id": 4, "link": "https://t.me/mychannel/12"}

    choice = Callback("managed_reaction_emoji_1")
    asyncio.run(flow.preview_managed_reaction_choice(choice, state))
    assert calls == [("preview", 4, "https://t.me/mychannel/12", "❤️")]
    assert state.current == flow.ManagedReactionFlow.waiting_confirmation.state
    assert "My channel" in choice.message.edits[-1][0]
    assert "❤️" in choice.message.edits[-1][0]

    confirm = Callback("managed_reaction_confirm")
    asyncio.run(flow.confirm_managed_reaction(confirm, state))
    assert calls[-1] == ("send", 4, "https://t.me/mychannel/12", "❤️", 7, 987)
    assert state.current is None
    assert "Одна реакция отправлена" in confirm.message.edits[-1][0]


def test_managed_reaction_denies_unauthorized_user(monkeypatch):
    from bot.handlers import managed_reactions as flow

    called = False

    async def preview(*_args):
        nonlocal called
        called = True

    monkeypatch.setattr(flow, "is_authorized_user", lambda _user_id: False)
    monkeypatch.setattr(flow, "preview_managed_reaction", preview)
    state = State()
    state.current = flow.ManagedReactionFlow.waiting_emoji.state
    state.values = {"account_id": 4, "link": "https://t.me/mychannel/12"}
    callback = Callback("managed_reaction_emoji_0", user_id=999)

    asyncio.run(flow.preview_managed_reaction_choice(callback, state))
    assert not called
    assert callback.answers[-1][1]["show_alert"] is True


def test_prior_attempt_stops_before_confirmation(monkeypatch):
    from bot.handlers import managed_reactions as flow

    async def preview(account_id, link, emoji):
        return {
            "chat_title": "Owned group", "message_id": 12,
            "text_excerpt": "Post", "account_name": "acct",
            "link": link, "emoji": emoji, "already_attempted": True,
        }

    monkeypatch.setattr(flow, "is_authorized_user", lambda _user_id: True)
    monkeypatch.setattr(flow, "preview_managed_reaction", preview)
    state = State()
    state.current = flow.ManagedReactionFlow.waiting_emoji.state
    state.values = {"account_id": 4, "link": "https://t.me/mychannel/12"}
    callback = Callback("managed_reaction_emoji_0")
    asyncio.run(flow.preview_managed_reaction_choice(callback, state))
    assert state.current is None
    assert "Повторная отправка заблокирована" in callback.message.edits[-1][0]


def test_history_is_read_only_and_escapes_stored_text(monkeypatch):
    from bot.handlers import managed_reactions as flow

    async def history(*, limit):
        assert limit == 20
        return [{
            "account_name": "<unsafe>", "link": "https://t.me/owned/12",
            "emoji": "👍", "status": "sent", "reason": None,
            "created_at": "2026-09-26T12:00:00",
        }]

    monkeypatch.setattr(flow, "is_authorized_user", lambda _user_id: True)
    monkeypatch.setattr(flow, "list_managed_reaction_history", history)
    state = State()
    callback = Callback("managed_reaction_history")
    asyncio.run(flow.managed_reaction_history(callback, state))
    body = callback.message.edits[-1][0]
    assert "&lt;unsafe&gt;" in body
    assert "<unsafe>" not in body
    assert "https://t.me/owned/12" in body
