"""Bot UI for the read-only, owned-community link check."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from bot.handlers import community_link_check as flow
from bot.keyboards.main import get_accounts_manage_keyboard


class State:
    def __init__(self):
        self.data = {}
        self.current = None

    async def clear(self):
        self.data = {}
        self.current = None

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def get_data(self):
        return dict(self.data)

    async def get_state(self):
        return self.current

    async def set_state(self, value):
        self.current = value.state


class Message:
    def __init__(self, text="", user_id=7):
        self.text = text
        self.document = None
        self.bot = None
        self.from_user = SimpleNamespace(id=user_id)
        self.sent = []

    async def answer(self, text, **kwargs):
        self.sent.append((text, kwargs))

    async def edit_text(self, text, **kwargs):
        self.sent.append((text, kwargs))


class Callback:
    def __init__(self, user_id=7):
        self.from_user = SimpleNamespace(id=user_id)
        self.message = Message()
        self.answers = []

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))


def test_entry_and_account_pagination():
    buttons = [button for row in get_accounts_manage_keyboard().inline_keyboard for button in row]
    assert any(button.callback_data == "community_check_start" for button in buttons)
    accounts = [SimpleNamespace(id=i, list_label=None, username=f"user{i}", phone="") for i in range(1, 13)]
    first = flow._accounts_keyboard(accounts, 0).inline_keyboard
    assert sum(row[0].callback_data.startswith("community_check_account_") for row in first) == 10
    assert any(button.callback_data == "community_check_page_1" for row in first for button in row)
    assert any(row[0].callback_data == "community_check_history" for row in first)
    assert any(row[0].callback_data == "community_check_jobs" for row in first)
    second = flow._accounts_keyboard(accounts, 1).inline_keyboard
    assert sum(row[0].callback_data.startswith("community_check_account_") for row in second) == 2


def test_link_result_is_read_only_and_escaped(monkeypatch):
    called = []

    async def check(account_id, link, actor_id=None):
        called.append((account_id, link, actor_id))
        return {
            "status": "ok", "kind": "channel", "title": "<unsafe>",
            "username": "owned", "account_name": "<account>",
        }

    monkeypatch.setattr(flow, "is_authorized_user", lambda user_id: user_id == 7)
    monkeypatch.setattr(flow, "check_owned_community_link", check)
    state = State()
    state.data = {"account_id": 4}
    message = Message("https://t.me/owned")
    asyncio.run(flow.community_check_link(message, state))
    assert called == [(4, "https://t.me/owned", 7)]
    assert state.data == {}
    assert "&lt;unsafe&gt;" in message.sent[-1][0]
    assert "<unsafe>" not in message.sent[-1][0]
    assert "&lt;account&gt;" in message.sent[-1][0]


def test_unauthorized_user_does_not_check(monkeypatch):
    called = False

    async def check(*_args):
        nonlocal called
        called = True

    monkeypatch.setattr(flow, "is_authorized_user", lambda _user_id: False)
    monkeypatch.setattr(flow, "check_owned_community_link", check)
    state = State()
    state.data = {"account_id": 4}
    message = Message("https://t.me/owned", user_id=999)
    asyncio.run(flow.community_check_link(message, state))
    assert not called
    assert state.data == {"account_id": 4}


def test_history_shows_owned_title_and_escapes_data(monkeypatch):
    async def history(*, limit, actor_id):
        assert limit == 20
        assert actor_id == 7
        return [{
            "account_id": 4, "canonical_link": "https://t.me/owned_news",
            "title": "<Owned>", "status": "ok",
            "checked_at": "2026-09-27T10:20:00+00:00",
        }]

    monkeypatch.setattr(flow, "is_authorized_user", lambda _user_id: True)
    monkeypatch.setattr(flow, "list_owned_community_link_checks", history)
    callback = Callback()
    asyncio.run(flow.community_check_history(callback, State()))
    body = callback.message.sent[-1][0]
    assert "&lt;Owned&gt;" in body
    assert "<Owned>" not in body
    assert "https://t.me/owned_news" in body


def test_batch_rejects_invalid_without_check(monkeypatch):
    called = []

    async def check(*args, **kwargs):
        called.append((args, kwargs))

    monkeypatch.setattr(flow, "is_authorized_user", lambda _user_id: True)
    monkeypatch.setattr(flow, "check_owned_community_link", check)
    state = State()
    state.data = {"account_id": 4}
    message = Message("https://t.me/owned_news\nhttps://t.me/+invite")
    asyncio.run(flow.community_check_batch_input(message, state))
    assert not called
    assert state.current is None
    assert "строках: 2" in message.sent[-1][0]


def test_batch_confirm_checks_unique_links_and_escapes_titles(monkeypatch):
    called = []

    async def accounts():
        return [SimpleNamespace(id=4)]

    async def check(account_id, link, actor_id):
        called.append((account_id, link, actor_id))
        if len(called) == 1:
            return {"status": "ok", "title": "<Owned>"}
        return {"status": "failed", "title": "secret"}

    monkeypatch.setattr(flow, "is_authorized_user", lambda _user_id: True)
    monkeypatch.setattr(flow, "_active_accounts", accounts)
    monkeypatch.setattr(flow, "check_owned_community_link", check)
    state = State()
    state.data = {"account_id": 4, "batch_raw": (
        "https://t.me/owned_news\nhttps://t.me/OWNED_NEWS\nhttps://t.me/other_news"
    )}
    state.current = flow.CommunityCheckFlow.batch_preview.state
    callback = Callback()
    asyncio.run(flow.community_check_batch_confirm(callback, state))
    assert called == [
        (4, "https://t.me/owned_news", 7),
        (4, "https://t.me/other_news", 7),
    ]
    assert "&lt;Owned&gt;" in callback.message.sent[-1][0]
    assert "secret" not in callback.message.sent[-1][0]
    assert state.data == {}
    assert 7 not in flow._active_batches


def test_batch_stop_between_links(monkeypatch):
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()
        called = []

        async def accounts():
            return [SimpleNamespace(id=4)]

        async def check(account_id, link, actor_id):
            called.append(link)
            started.set()
            await release.wait()
            return {"status": "failed"}

        monkeypatch.setattr(flow, "is_authorized_user", lambda _user_id: True)
        monkeypatch.setattr(flow, "_active_accounts", accounts)
        monkeypatch.setattr(flow, "check_owned_community_link", check)
        state = State()
        state.data = {"account_id": 4, "batch_raw": "https://t.me/first_one\nhttps://t.me/second_one"}
        state.current = flow.CommunityCheckFlow.batch_preview.state
        callback = Callback()
        task = asyncio.create_task(flow.community_check_batch_confirm(callback, state))
        await started.wait()
        stop = Callback()
        await flow.community_check_batch_stop(stop)
        release.set()
        await task
        assert called == ["https://t.me/first_one"]
        assert "остановлена" in callback.message.sent[-1][0]
        assert 7 not in flow._active_batches

    asyncio.run(scenario())


def test_batch_txt_size_and_encoding(monkeypatch):
    class Bot:
        def __init__(self, payload):
            self.payload = payload
            self.downloaded = False

        async def download(self, _document, destination):
            self.downloaded = True
            destination.write(self.payload)

    monkeypatch.setattr(flow, "is_authorized_user", lambda _user_id: True)
    state = State()
    state.data = {"account_id": 4}
    message = Message()
    message.document = SimpleNamespace(file_name="links.txt", file_size=16385)
    message.bot = Bot(b"")
    asyncio.run(flow.community_check_batch_input(message, state))
    assert not message.bot.downloaded
    assert "слишком большой" in message.sent[-1][0]
    message.document.file_size = 2
    message.bot = Bot(b"\xff\xfe")
    asyncio.run(flow.community_check_batch_input(message, state))
    assert "UTF-8" in message.sent[-1][0]
    message.bot = Bot(b"https://t.me/owned_news\n")
    asyncio.run(flow.community_check_batch_input(message, state))
    assert state.data["batch_raw"] == "https://t.me/owned_news\n"
    assert "1 уникальных" in message.sent[-1][0]


def test_batch_confirm_revalidates_before_check(monkeypatch):
    called = []

    async def accounts():
        return [SimpleNamespace(id=4)]

    async def check(*args, **kwargs):
        called.append((args, kwargs))

    monkeypatch.setattr(flow, "is_authorized_user", lambda _user_id: True)
    monkeypatch.setattr(flow, "_active_accounts", accounts)
    monkeypatch.setattr(flow, "check_owned_community_link", check)
    state = State()
    state.data = {"account_id": 4, "batch_raw": "https://t.me/+invite"}
    state.current = flow.CommunityCheckFlow.batch_preview.state
    callback = Callback()
    asyncio.run(flow.community_check_batch_confirm(callback, state))
    assert not called
    assert callback.answers[-1][1]["show_alert"] is True


def test_batch_schedule_persists_due_job_without_running_checks(monkeypatch):
    scheduled = []
    checked = []

    async def accounts():
        return [SimpleNamespace(id=4)]

    async def enqueue(account_id, actor_id, links, delay_seconds):
        scheduled.append((account_id, actor_id, links, delay_seconds))
        return {
            "id": 21, "status": "pending", "due_at": "2026-09-27T12:00:00",
            "checked": 0, "total": 2, "owned_count": 0, "other_count": 0,
            "owned_titles": [],
        }

    async def check(*args, **kwargs):
        checked.append((args, kwargs))

    monkeypatch.setattr(flow, "is_authorized_user", lambda _user_id: True)
    monkeypatch.setattr(flow, "_active_accounts", accounts)
    monkeypatch.setattr(flow, "enqueue_owned_community_batch", enqueue)
    monkeypatch.setattr(flow, "check_owned_community_link", check)
    state = State()
    state.data = {"account_id": 4, "batch_raw": "https://t.me/first_one\nhttps://t.me/second_one"}
    state.current = flow.CommunityCheckFlow.batch_preview.state
    callback = Callback()
    callback.data = "community_check_batch_schedule_600"
    asyncio.run(flow.community_check_batch_schedule(callback, state))
    assert scheduled == [(4, 7, ["https://t.me/first_one", "https://t.me/second_one"], 600)]
    assert checked == []
    assert state.data == {}
    assert "Пакетная проверка #21" in callback.message.sent[-1][0]
    assert "community_check_job_cancel_21" in str(callback.message.sent[-1][1]["reply_markup"])


def test_custom_utc_schedule_uses_preview_and_keeps_invalid_date_for_retry(monkeypatch):
    scheduled = []

    async def accounts():
        return [SimpleNamespace(id=4)]

    async def enqueue(account_id, actor_id, links, delay_seconds=None, *, start_at_utc=None):
        scheduled.append((account_id, actor_id, links, delay_seconds, start_at_utc))
        return {
            "id": 23, "status": "pending", "due_at": "2026-10-01T12:30:00+00:00",
            "checked": 0, "total": 1, "owned_count": 0, "other_count": 0,
            "owned_titles": [],
        }

    monkeypatch.setattr(flow, "is_authorized_user", lambda _user_id: True)
    monkeypatch.setattr(flow, "_active_accounts", accounts)
    monkeypatch.setattr(flow, "enqueue_owned_community_batch", enqueue)
    state = State()
    state.data = {"account_id": 4, "batch_raw": "https://t.me/owned_news"}
    state.current = flow.CommunityCheckFlow.batch_preview.state
    callback = Callback()
    asyncio.run(flow.community_check_batch_schedule_custom(callback, state))
    assert state.current == flow.CommunityCheckFlow.waiting_schedule_at.state
    assert "UTC" in callback.message.sent[-1][0]
    invalid = Message("2026-02-30 12:30")
    asyncio.run(flow.community_check_batch_schedule_at(invalid, state))
    assert state.current == flow.CommunityCheckFlow.waiting_schedule_at.state
    assert not scheduled
    valid = Message("2026-10-01 12:30")
    asyncio.run(flow.community_check_batch_schedule_at(valid, state))
    assert scheduled == [(
        4, 7, ["https://t.me/owned_news"], None,
        datetime(2026, 10, 1, 12, 30, tzinfo=timezone.utc),
    )]
    assert state.data == {}
    assert "Пакетная проверка #23" in valid.sent[-1][0]


def test_batch_job_status_and_cancel_are_actor_scoped_and_redacted(monkeypatch):
    seen = []
    job = {
        "id": 21, "status": "processing", "checked": 1, "total": 2,
        "owned_count": 1, "other_count": 0, "owned_titles": ["<Mine>"],
        "links": ["https://t.me/foreign_news"], "foreign_title": "Secret foreign",
    }

    async def get_job(job_id, actor_id):
        seen.append(("get", job_id, actor_id))
        return job if actor_id == 7 else None

    async def cancel_job(job_id, actor_id):
        seen.append(("cancel", job_id, actor_id))
        return {**job, "status": "cancelled"} if actor_id == 7 else None

    monkeypatch.setattr(flow, "is_authorized_user", lambda _user_id: True)
    monkeypatch.setattr(flow, "get_owned_community_batch", get_job)
    monkeypatch.setattr(flow, "cancel_owned_community_batch", cancel_job)
    callback = Callback()
    callback.data = "community_check_job_show_21"
    asyncio.run(flow.community_check_job_show(callback))
    body = callback.message.sent[-1][0]
    assert "&lt;Mine&gt;" in body
    assert "foreign_news" not in body and "Secret foreign" not in body
    callback.data = "community_check_job_cancel_21"
    asyncio.run(flow.community_check_job_cancel(callback))
    assert seen == [("get", 21, 7), ("cancel", 21, 7)]
    assert "отменено" in callback.message.sent[-1][0]


def test_batch_jobs_history_is_actor_scoped_and_hides_targets(monkeypatch):
    seen = []

    async def listing(actor_id, *, limit):
        seen.append((actor_id, limit))
        return [{
            "id": 23, "status": "pending", "created_at": "2026-09-27T10:00:00+00:00",
            "checked": 0, "total": 2, "links": ["https://t.me/foreign_group"],
            "owned_titles": [],
        }]

    monkeypatch.setattr(flow, "is_authorized_user", lambda _user_id: True)
    monkeypatch.setattr(flow, "list_owned_community_batches", listing)
    callback = Callback()
    asyncio.run(flow.community_check_jobs(callback))
    body = callback.message.sent[-1][0]
    assert seen == [(7, 20)]
    assert "#23" in body and "foreign_group" not in body
    assert "community_check_job_show_23" in str(callback.message.sent[-1][1]["reply_markup"])


def test_stale_batch_preview_cannot_start_or_schedule(monkeypatch):
    called = []

    async def forbidden(*args, **kwargs):
        called.append((args, kwargs))

    monkeypatch.setattr(flow, "is_authorized_user", lambda _user_id: True)
    monkeypatch.setattr(flow, "check_owned_community_link", forbidden)
    monkeypatch.setattr(flow, "enqueue_owned_community_batch", forbidden)
    state = State()
    state.data = {"account_id": 4, "batch_raw": "https://t.me/owned_news"}
    state.current = flow.CommunityCheckFlow.waiting_batch.state
    callback = Callback()
    asyncio.run(flow.community_check_batch_confirm(callback, state))
    callback.data = "community_check_batch_schedule_600"
    asyncio.run(flow.community_check_batch_schedule(callback, state))
    assert called == []
    assert len(callback.answers) == 2
