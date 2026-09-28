"""2FA menu must verify Telegram state and never retain submitted secrets."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bot.handlers.accounts import twofa
from database.models import ProxyType


class FakeClient:
    def __init__(self, has_password=False, *, accepted_current="old-password"):
        self.has_password = has_password
        self.accepted_current = accepted_current
        self.edits = []
        self.authorized = True

    async def is_user_authorized(self):
        return self.authorized

    async def __call__(self, request):
        assert type(request).__name__ == "GetPasswordRequest"
        return SimpleNamespace(has_password=self.has_password)

    async def edit_2fa(self, **kwargs):
        self.edits.append(kwargs)
        if self.has_password and kwargs.get("current_password") != self.accepted_current:
            raise PasswordHashInvalidError()
        self.has_password = True
        return True


class PasswordHashInvalidError(Exception):
    pass


class FakeWorker:
    def __init__(self, client):
        self.client = client
        self.disconnected = False

    async def connect(self, *, quiet=False):
        return True

    async def disconnect(self):
        self.disconnected = True


def account(tmp_path, *, proxy=True):
    (tmp_path / "session.session").write_bytes(b"x")
    return SimpleNamespace(
        id=7,
        display_title="Account",
        session_name="session",
        proxy=SimpleNamespace(proxy_type=ProxyType.SOCKS5, is_active=True, is_working=True)
        if proxy else None,
    )


def test_live_status_and_rotation_require_current_password(tmp_path, monkeypatch):
    acc = account(tmp_path)
    client = FakeClient(has_password=True)
    worker = FakeWorker(client)
    monkeypatch.setattr(twofa, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(
        "workers.manager.account_worker_for_action",
        lambda *_args: worker,
    )
    assert asyncio.run(twofa._probe_account(acc)) == ("✅", "включена")
    skipped = asyncio.run(twofa._apply_account(acc, "new-password", None))
    assert skipped.outcome == "skipped"
    assert client.edits == []

    wrong = asyncio.run(twofa._apply_account(acc, "new-password", "wrong"))
    assert wrong.outcome == "error"
    assert wrong.detail == "текущий пароль не подошёл"

    changed = asyncio.run(twofa._apply_account(acc, "new-password", "old-password"))
    assert changed.outcome == "updated"
    assert client.edits[-1]["current_password"] == "old-password"
    assert worker.disconnected


def test_new_2fa_requires_proof_after_telegram_edit(tmp_path, monkeypatch):
    acc = account(tmp_path)
    client = FakeClient()
    monkeypatch.setattr(twofa, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(
        "workers.manager.account_worker_for_action",
        lambda *_args: FakeWorker(client),
    )
    changed = asyncio.run(twofa._apply_account(acc, "new-password", None))
    assert changed.outcome == "updated"
    assert "current_password" not in client.edits[0]
    assert asyncio.run(twofa._probe_account(acc))[0] == "✅"


def test_telegram_without_post_edit_confirmation_is_error(tmp_path, monkeypatch):
    acc = account(tmp_path)
    client = FakeClient()

    async def edit_without_status(**kwargs):
        client.edits.append(kwargs)
        return True

    client.edit_2fa = edit_without_status
    monkeypatch.setattr(twofa, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(
        "workers.manager.account_worker_for_action",
        lambda *_args: FakeWorker(client),
    )
    result = asyncio.run(twofa._apply_account(acc, "new-password", None))
    assert result.outcome == "error"


def test_missing_proxy_blocks_telegram_action(tmp_path, monkeypatch):
    acc = account(tmp_path, proxy=False)
    called = []
    monkeypatch.setattr(twofa, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(
        "workers.manager.account_worker_for_action",
        lambda *_args: called.append(True),
    )
    result = asyncio.run(twofa._apply_account(acc, "new-password", None))
    assert result.outcome == "error"
    assert called == []
    assert asyncio.run(twofa._probe_account(acc))[0] == "⚠️"


class FakeState:
    def __init__(self, data):
        self.data = data
        self.cleared = False

    async def get_data(self):
        return self.data.copy()

    async def set_state(self, _state):
        return None

    async def update_data(self, **values):
        self.data.update(values)

    async def clear(self):
        self.data = {}
        self.cleared = True


class FakeMessage:
    def __init__(self, text=None, *, delete_fails=False):
        self.answers = []
        self.photo = None
        self.caption = None
        self.text = text
        self.from_user = SimpleNamespace(id=1)
        self.delete_fails = delete_fails
        self.deleted = False

    async def delete(self):
        if self.delete_fails:
            raise RuntimeError("Telegram refused deletion")
        self.deleted = True

    async def answer(self, text, **kwargs):
        self.answers.append(text)
        return self

    async def edit_text(self, text, **kwargs):
        self.answers.append(text)


def test_list_pages_have_ten_accounts_and_live_status(monkeypatch):
    async def accounts():
        return [
            SimpleNamespace(id=i, display_title=f"Account {i}")
            for i in range(1, 13)
        ]

    async def probe(acc):
        return ("✅" if acc.id % 2 else "❌", "checked")

    class Callback:
        data = "twofa_list_p_0"
        from_user = SimpleNamespace(id=1)
        message = FakeMessage()

        async def answer(self, *args, **kwargs):
            return None

    monkeypatch.setattr(twofa, "is_authorized_user", lambda uid: uid == 1)
    monkeypatch.setattr(twofa, "_accounts", accounts)
    monkeypatch.setattr(twofa, "_probe_account", probe)
    captured = []

    async def fake_edit(_message, _text, *, reply_markup):
        captured.append(reply_markup)

    monkeypatch.setattr(twofa, "safe_edit_message", fake_edit)
    callback = Callback()
    asyncio.run(twofa.cb_twofa_list(callback))
    rows = captured[-1].inline_keyboard
    assert len([row for row in rows if row[0].callback_data.startswith("twofa_pick_")]) == 10
    assert rows[0][0].text.startswith("✅")
    assert rows[1][0].text.startswith("❌")
    assert any(button.callback_data == "twofa_list_p_1" for row in rows for button in row)


def test_fsm_secrets_cleared_even_when_account_query_fails(monkeypatch):
    state = FakeState({"twofa_mode": "all", "twofa_new_password": "secret-123"})
    message = FakeMessage()

    async def fail_accounts():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(twofa, "_accounts", fail_accounts)
    asyncio.run(twofa._run_change(message, state, current_password=None))
    assert state.cleared
    assert state.data == {}
    assert "secret-123" not in " ".join(message.answers)
    assert any("Не удалось завершить" in answer for answer in message.answers)


@pytest.mark.parametrize(
    ("handler", "state_data", "submitted"),
    [
        (twofa.process_new_password, {"twofa_mode": "all"}, "new-secret-123"),
        (
            twofa.process_confirm_password,
            {"twofa_mode": "all", "twofa_new_password": "new-secret-123"},
            "new-secret-123",
        ),
        (
            twofa.process_current_password,
            {"twofa_mode": "unified", "twofa_new_password": "new-secret-123"},
            "old-secret-123",
        ),
    ],
)
def test_failed_message_deletion_aborts_and_clears_secrets(
    monkeypatch, handler, state_data, submitted
):
    monkeypatch.setattr(twofa, "is_authorized_user", lambda _uid: True)
    state = FakeState(state_data)
    message = FakeMessage(submitted, delete_fails=True)
    asyncio.run(handler(message, state))
    assert state.cleared
    assert state.data == {}
    assert len(message.answers) == 1
    assert submitted not in message.answers[0]
    assert "удалите сообщение вручную" in message.answers[0]


def test_password_whitespace_is_preserved_through_confirmation_and_rotation(monkeypatch):
    monkeypatch.setattr(twofa, "is_authorized_user", lambda _uid: True)
    state = FakeState({"twofa_mode": "unified"})
    new_message = FakeMessage("  new-secret-123  ")
    asyncio.run(twofa.process_new_password(new_message, state))
    assert new_message.deleted
    assert state.data["twofa_new_password"] == "  new-secret-123  "

    confirmation = FakeMessage("  new-secret-123  ")
    asyncio.run(twofa.process_confirm_password(confirmation, state))
    assert confirmation.deleted

    captured = []

    async def run_change(_message, _state, *, current_password):
        captured.append(current_password)

    monkeypatch.setattr(twofa, "_run_change", run_change)
    current = FakeMessage("  old-secret-123  ")
    asyncio.run(twofa.process_current_password(current, state))
    assert current.deleted
    assert captured == ["  old-secret-123  "]


@pytest.mark.parametrize("cleanup_failure", [False, True])
def test_successful_rpc_remains_updated_when_cleanup_fails(tmp_path, monkeypatch, cleanup_failure):
    acc = account(tmp_path)
    client = FakeClient()
    worker = FakeWorker(client)

    async def disconnect():
        if cleanup_failure:
            raise RuntimeError("cleanup failed")
        return False

    worker.disconnect = disconnect
    monkeypatch.setattr(twofa, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr("workers.manager.account_worker_for_action", lambda *_args: worker)
    result = asyncio.run(twofa._apply_account(acc, "new-password", None))
    assert result.outcome == "updated"
    assert client.has_password


def test_invalid_fsm_mode_cannot_change_fleet_and_secrets_are_cleared(monkeypatch):
    state = FakeState({"twofa_mode": "bogus", "twofa_new_password": "new-secret-123"})
    message = FakeMessage()
    called = []

    async def accounts():
        called.append(True)
        return []

    monkeypatch.setattr(twofa, "_accounts", accounts)
    asyncio.run(twofa._run_change(message, state, current_password=None))
    assert called == []
    assert state.cleared
    assert state.data == {}


@pytest.mark.parametrize(
    ("mode", "expected_outcomes", "expected_edits"),
    [
        ("all", (1, 2, 0), (1, 0, 0)),
        ("unified", (2, 0, 1), (1, 1, 1)),
    ],
)
def test_fleet_modes_report_partial_results(
    tmp_path, monkeypatch, mode, expected_outcomes, expected_edits
):
    clients = {
        1: FakeClient(has_password=False),
        2: FakeClient(has_password=True, accepted_current="old-secret"),
        3: FakeClient(has_password=True, accepted_current="different-secret"),
    }
    proxy = SimpleNamespace(proxy_type=ProxyType.SOCKS5, is_active=True, is_working=True)
    accounts = [
        SimpleNamespace(id=i, display_title=f"Account {i}", session_name=f"session-{i}", proxy=proxy)
        for i in clients
    ]
    for acc in accounts:
        (tmp_path / f"{acc.session_name}.session").write_bytes(b"x")

    async def get_accounts():
        return accounts

    monkeypatch.setattr(twofa, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(twofa, "_accounts", get_accounts)
    monkeypatch.setattr(
        "workers.manager.account_worker_for_action",
        lambda acc, *_args: FakeWorker(clients[acc.id]),
    )
    message = FakeMessage()
    state = FakeState({"twofa_mode": mode, "twofa_new_password": "new-secret"})
    asyncio.run(twofa._run_change(message, state, current_password="old-secret" if mode == "unified" else None))

    updated, skipped, errors = expected_outcomes
    assert f"Обновлено: {updated}" in message.answers[-1]
    assert f"Пропущено: {skipped}" in message.answers[-1]
    assert f"Ошибки: {errors}" in message.answers[-1]
    assert tuple(len(client.edits) for client in clients.values()) == expected_edits
    assert state.data == {}
    assert all("old-secret" not in answer and "new-secret" not in answer for answer in message.answers)


def test_borrowed_client_blocks_disconnect_and_send_during_2fa(tmp_path, monkeypatch):
    from workers.manager import _BorrowedWorker

    acc = account(tmp_path)
    client = FakeClient()
    owner = FakeWorker(client)
    owner.is_connected = True
    owner._connection_lock = asyncio.Lock()
    owner._send_lock = asyncio.Lock()
    monkeypatch.setattr(twofa, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(
        "workers.manager.account_worker_for_action",
        lambda *_args: _BorrowedWorker(owner),
    )

    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()
        events = []

        async def action(_client):
            entered.set()
            await release.wait()
            events.append("2fa-finished")
            return "done"

        async def competing_disconnect():
            async with owner._connection_lock:
                owner.is_connected = False
                events.append("disconnected")

        async def competing_send():
            async with owner._send_lock:
                events.append("sent")

        mutation = asyncio.create_task(twofa._with_client(acc, action))
        await asyncio.wait_for(entered.wait(), timeout=1)
        disconnect = asyncio.create_task(competing_disconnect())
        send = asyncio.create_task(competing_send())
        await asyncio.sleep(0)
        assert not disconnect.done()
        assert not send.done()
        release.set()
        assert await asyncio.wait_for(mutation, timeout=1) == "done"
        await asyncio.wait_for(asyncio.gather(disconnect, send), timeout=1)
        assert events[0] == "2fa-finished"
        assert set(events[1:]) == {"disconnected", "sent"}
        assert not owner.disconnected  # Borrowed wrapper never closes the owner's client.

    asyncio.run(scenario())


def test_borrowed_client_respects_send_then_connection_lock_order(tmp_path, monkeypatch):
    from workers.manager import _BorrowedWorker

    acc = account(tmp_path)
    owner = FakeWorker(FakeClient())
    owner.is_connected = True
    owner._send_lock = asyncio.Lock()
    owner._connection_lock = asyncio.Lock()
    monkeypatch.setattr(twofa, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(
        "workers.manager.account_worker_for_action",
        lambda *_args: _BorrowedWorker(owner),
    )

    async def scenario():
        send_started = asyncio.Event()
        finish_send = asyncio.Event()

        async def send_then_disconnect():
            async with owner._send_lock:
                send_started.set()
                await finish_send.wait()
                async with owner._connection_lock:
                    return "send-complete"

        async def action(_client):
            return "2fa-complete"

        sending = asyncio.create_task(send_then_disconnect())
        await send_started.wait()
        mutation = asyncio.create_task(twofa._with_client(acc, action))
        await asyncio.sleep(0)
        finish_send.set()
        assert await asyncio.wait_for(sending, timeout=1) == "send-complete"
        assert await asyncio.wait_for(mutation, timeout=1) == "2fa-complete"

    asyncio.run(scenario())
