"""AI menu navigation and secret-handling smoke tests."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from cryptography.fernet import Fernet

from bot.handlers import ai_provider_menu as menu
from bot.keyboards.main import get_ai_keyboard


class State:
    def __init__(self):
        self.values = {}
        self.current = None

    async def clear(self):
        self.values = {}
        self.current = None

    async def set_state(self, value):
        self.current = value

    async def update_data(self, **values):
        self.values.update(values)

    async def get_data(self):
        return dict(self.values)


class TelegramMessage:
    def __init__(self, text=""):
        self.from_user = SimpleNamespace(id=1)
        self.text = text
        self.deleted = False
        self.answers = []
        self.edits = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))

    async def delete(self):
        self.deleted = True


class Callback:
    def __init__(self, data):
        self.data = data
        self.from_user = SimpleNamespace(id=1)
        self.message = TelegramMessage()
        self.answered = False

    async def answer(self, *_args, **_kwargs):
        self.answered = True


def _callbacks(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def test_ai_menu_navigation(monkeypatch):
    monkeypatch.setattr(menu, "is_authorized_user", lambda user_id: user_id == 1)
    state = State()
    assert "ai_model_settings" in _callbacks(get_ai_keyboard())
    assert "ai_prompt_settings" in _callbacks(get_ai_keyboard())

    model = Callback("ai_model_settings")
    asyncio.run(menu.model_settings(model, state))
    assert model.answered
    assert "ai_api_keys" in _callbacks(model.message.edits[-1][1]["reply_markup"])

    keys = Callback("ai_api_keys")
    asyncio.run(menu.api_keys(keys, state))
    assert {"ai_builtin_openrouter", "ai_builtin_openai", "ai_builtin_deepseek", "ai_custom_menu"}.issubset(
        _callbacks(keys.message.edits[-1][1]["reply_markup"])
    )

    custom = Callback("ai_custom_menu")
    asyncio.run(menu.custom_menu(custom, state))
    assert {"ai_provider_new", "ai_provider_list_0"}.issubset(
        _callbacks(custom.message.edits[-1][1]["reply_markup"])
    )

    sampling = Callback("ai_sampling_intro")
    asyncio.run(menu.sampling_intro(sampling, state))
    assert "Temperature" in sampling.message.edits[-1][0]
    assert "ai_sampling_mailings_0" in _callbacks(sampling.message.edits[-1][1]["reply_markup"])

    prompt = Callback("ai_prompt_settings")
    asyncio.run(menu.prompt_settings(prompt, state))
    assert "ai_prompt_mailings_0" in _callbacks(prompt.message.edits[-1][1]["reply_markup"])


def test_provider_creation_deletes_key_message_and_never_echoes_it(monkeypatch):
    monkeypatch.setattr(menu, "is_authorized_user", lambda user_id: user_id == 1)
    monkeypatch.setenv("AI_PROVIDER_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    captured = {}

    @asynccontextmanager
    async def session_scope():
        yield object()

    async def create_provider(_session, **fields):
        captured.update(fields)
        return SimpleNamespace(id=17)

    monkeypatch.setattr(menu, "session_scope", session_scope)
    monkeypatch.setattr(menu, "create_provider", create_provider)
    state = State()
    asyncio.run(menu.provider_create(Callback("ai_provider_create_openai"), state))
    assert state.values["kind"] == "openai"
    asyncio.run(menu.provider_model(TelegramMessage("gpt-test"), state))
    secret = "sk-secret-for-test"
    message = TelegramMessage(secret)
    asyncio.run(menu.provider_key(message, state))
    assert message.deleted
    assert captured["api_key"] == secret
    assert captured["default_model"] == "gpt-test"
    assert all(secret not in text for text, _ in message.answers)
    assert "ai_prov_view_17" in _callbacks(message.answers[-1][1]["reply_markup"])


def test_provider_edit_and_campaign_selection(monkeypatch):
    monkeypatch.setattr(menu, "is_authorized_user", lambda user_id: user_id == 1)
    calls = []

    @asynccontextmanager
    async def session_scope():
        yield object()

    async def update_provider(_session, provider_id, **kwargs):
        calls.append(("edit", provider_id, kwargs))

    async def select_mailing_provider(_session, mailing_id, provider_id):
        calls.append(("mailing", mailing_id, provider_id))

    async def select_default_provider(_session, provider_id):
        calls.append(("default", provider_id))

    monkeypatch.setattr(menu, "session_scope", session_scope)
    monkeypatch.setattr(menu, "update_provider", update_provider)
    monkeypatch.setattr(menu, "select_mailing_provider", select_mailing_provider)
    monkeypatch.setattr(menu, "select_default_provider", select_default_provider)

    state = State()
    asyncio.run(state.update_data(provider_id=7, field="key"))
    key_message = TelegramMessage("new-secret")
    asyncio.run(menu.provider_save_edit(key_message, state))
    assert key_message.deleted
    assert calls[-1] == ("edit", 7, {"api_key": "new-secret"})
    assert all("new-secret" not in text for text, _ in key_message.answers)

    callback = Callback("ai_mailing_set_11_7")
    asyncio.run(menu.mailing_set(callback, state))
    assert calls[-1] == ("mailing", 11, 7)
    assert "ai_mailing_providers_11_0" in _callbacks(callback.message.edits[-1][1]["reply_markup"])

    callback = Callback("ai_default_set_0")
    asyncio.run(menu.default_set(callback, state))
    assert calls[-1] == ("default", None)
