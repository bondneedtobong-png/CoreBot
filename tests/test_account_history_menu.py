import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.handlers.accounts import list_card
from bot.keyboards.main import get_account_card_keyboard


def test_account_card_has_history_button_and_account_specific_backlink():
    keyboard = get_account_card_keyboard(SimpleNamespace(id=17))
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]

    assert "account_history_17" in callbacks


def test_history_is_limited_to_20_and_only_renders_safe_metadata():
    now = datetime(2026, 9, 26, 12, 0)

    class FakeSession:
        async def scalars(self, query):
            assert query._limit_clause.value == 20
            model = query.column_descriptions[0]["entity"]
            rows = {
                list_card.AccountHealthCheck: [SimpleNamespace(requested_at=now, status="pending")],
                list_card.AccountSafetyEvent: [SimpleNamespace(created_at=now - timedelta(minutes=1), event_type="private-secret")],
                list_card.AccountImportEvent: [SimpleNamespace(created_at=now - timedelta(minutes=2))],
                list_card.MailingLog: [SimpleNamespace(sent_at=now - timedelta(minutes=3), success=False)],
            }

            class Result:
                def all(self):
                    return rows[model]

            return Result()

    events = asyncio.run(list_card._load_account_history(FakeSession(), 17))
    assert [(kind, status) for _, kind, status in events] == [
        ("Проверка", "ожидает"),
        ("Безопасность", "—"),
        ("Импорт", "аккаунт добавлен"),
        ("Рассылка", "ошибка"),
    ]
    assert list_card.ACCOUNT_HISTORY_LIMIT == 20
    assert list_card._history_status("pending") == "ожидает"
    assert list_card._history_status("private-secret") == "—"
    assert list_card._format_history_time(now) == "26.09.2026 12:00 UTC"


def test_history_rejects_malformed_account_id_before_database_access(monkeypatch):
    monkeypatch.setattr(list_card, "is_authorized_user", lambda _user_id: True)
    answer = AsyncMock()
    callback = SimpleNamespace(
        data="account_history_1_2", from_user=SimpleNamespace(id=1), answer=answer,
        message=SimpleNamespace(),
    )
    scope_called = False

    def forbidden_scope():
        nonlocal scope_called
        scope_called = True
        raise AssertionError("database should not be opened")

    monkeypatch.setattr(list_card, "session_scope", forbidden_scope)
    asyncio.run(list_card.cb_account_history(callback))

    assert not scope_called
    answer.assert_awaited_once_with("Некорректный аккаунт", show_alert=True)


def test_history_denies_non_owner_before_parsing_or_database_access(monkeypatch):
    monkeypatch.setattr(list_card, "is_authorized_user", lambda _user_id: False)
    answer = AsyncMock()
    callback = SimpleNamespace(
        data="account_history_17", from_user=SimpleNamespace(id=5), answer=answer,
        message=SimpleNamespace(),
    )

    def forbidden_scope():
        raise AssertionError("database should not be opened")

    monkeypatch.setattr(list_card, "session_scope", forbidden_scope)
    asyncio.run(list_card.cb_account_history(callback))

    answer.assert_awaited_once_with("⛔ Доступ запрещён", show_alert=True)
