"""No live Telegram calls: controlled single-post reactions and durable audit."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from telethon import errors
from telethon.tl.types import Channel, ChannelParticipantAdmin, ChannelParticipantSelf

from database.models import Account, AccountSafetyState, AccountStatus, Base, ManagedReactionAttempt
from services import managed_reactions as service


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def scope():
        async with maker() as session:
            try:
                yield session
            except BaseException:
                await session.rollback()
                raise

    monkeypatch.setattr(service, "session_scope", scope)

    async def setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with scope() as session:
            session.add(Account(
                id=7, phone="+70000000007", session_name="seven",
                username="owner", status=AccountStatus.ACTIVE, daily_limit=5,
                is_spam_blocked=False,
            ))
            await session.commit()

    asyncio.run(setup())
    yield scope
    asyncio.run(engine.dispose())


def _fake_worker(monkeypatch, *, failure=None):
    calls = []

    class Client:
        async def __call__(self, request):
            calls.append(request)
            if failure:
                raise failure
            return object()

    class Worker:
        def __init__(self):
            self.client = Client()
            self._send_lock = asyncio.Lock()
            self._connection_lock = asyncio.Lock()

        async def disconnect(self):
            return True

    worker = Worker()

    async def get_worker(_account):
        return worker

    async def details(_client, _peer, message_id, link, emoji, account):
        return {
            "chat_title": "Owned chat", "message_id": message_id,
            "text_excerpt": "Exact post", "account_name": account.display_title,
            "link": link, "emoji": emoji,
        }, SimpleNamespace(id=123)

    monkeypatch.setattr(service, "_worker_for", get_worker)
    monkeypatch.setattr(service, "_target_details", details)
    return calls


def test_strict_link_and_emoji_validation():
    for link in ("https://evil.example/x/1", "https://t.me/x/1?foo=bar",
                 "https://t.me/c/0/1", "https://t.me/c/10/0",
                 "https://t.me/owner/1/extra", "http://t.me/owner/1"):
        with pytest.raises(service.ManagedReactionError):
            service._parse_target(link, "👍")
    with pytest.raises(service.ManagedReactionError, match="invalid_emoji"):
        service._parse_target("https://t.me/owner/12", "😈")
    assert service._parse_target("t.me/c/123/45", "❤️")[:2] == (-100123, 45)


def test_preview_does_not_spend_budget(db, monkeypatch):
    calls = _fake_worker(monkeypatch)

    async def scenario():
        result = await service.preview_managed_reaction(7, "t.me/owner/42", "🔥")
        assert result["message_id"] == 42
        assert result["already_attempted"] is False
        async with db() as session:
            assert await session.get(AccountSafetyState, 7) is None
            assert await session.scalar(select(ManagedReactionAttempt.id)) is None

    asyncio.run(scenario())
    assert calls == []


def test_duplicate_and_uncertain_rpc_are_never_retried(db, monkeypatch):
    calls = _fake_worker(monkeypatch, failure=TimeoutError("unknown outcome"))

    async def scenario():
        first = await service.send_managed_reaction(7, "t.me/owner/42", "👍", 999)
        second = await service.send_managed_reaction(7, "t.me/owner/42", "🔥", 999)
        assert first["status"] == "failed" and first["reason"] == "outcome_uncertain"
        assert second["status"] == "skipped" and second["reason"] == "already_attempted"
        async with db() as session:
            audit = await session.scalar(select(ManagedReactionAttempt))
            state = await session.get(AccountSafetyState, 7)
            assert audit.status == "uncertain" and audit.emoji == "👍"
            assert state.attempts_today == 1

    asyncio.run(scenario())
    assert len(calls) == 1


def test_target_change_after_preview_never_reserves_or_sends(db, monkeypatch):
    calls = _fake_worker(monkeypatch)

    async def scenario():
        result = await service.send_managed_reaction(
            7, "t.me/owner/42", "👍", 999, expected_peer_id=999,
        )
        assert result == {"status": "failed", "reason": "target_changed"}
        async with db() as session:
            assert await session.get(AccountSafetyState, 7) is None
            assert await session.scalar(select(ManagedReactionAttempt.id)) is None

    asyncio.run(scenario())
    assert calls == []


def test_success_is_audited(db, monkeypatch):
    calls = _fake_worker(monkeypatch)

    async def scenario():
        sent = await service.send_managed_reaction(7, "t.me/owner/50", "❤️", 999)
        assert sent["status"] == "sent"
        assert (await service.send_managed_reaction(7, "t.me/owner/50", "❤️", 999))["status"] == "skipped"
        assert (await service.preview_managed_reaction(7, "t.me/owner/50", "❤️"))["already_attempted"] is True
        history = await service.list_managed_reaction_history(limit=1)
        assert len(history) == 1
        assert history[0]["chat_title"] == "Owned chat"
        assert history[0]["account_name"] == "owner"
        assert history[0]["message_id"] == 50 and history[0]["status"] == "sent"
        async with db() as session:
            audit = await session.scalar(select(ManagedReactionAttempt))
            assert audit.status == "sent"
        history = await service.list_managed_reaction_history(limit=20)
        assert len(history) == 1
        assert history[0]["status"] == "sent"
        assert history[0]["link"] == "https://t.me/owner/50"
        assert history[0]["account_id"] == 7

    asyncio.run(scenario())
    assert len(calls) == 1


def test_flood_wait_pauses_account_and_blocks_retry(db, monkeypatch):
    calls = _fake_worker(monkeypatch, failure=errors.FloodWaitError(None, 45))

    async def scenario():
        result = await service.send_managed_reaction(7, "t.me/owner/55", "🔥", 999)
        assert result["status"] == "failed"
        assert result["reason"] == "flood_wait" and result["retry_after"] == 45
        assert (await service.send_managed_reaction(7, "t.me/owner/55", "🔥", 999))["reason"] == "already_attempted"
        async with db() as session:
            audit = await session.scalar(select(ManagedReactionAttempt))
            state = await session.get(AccountSafetyState, 7)
            assert audit.status == "failed" and audit.reason == "flood_wait"
            assert state.state == "cooling_down" and state.reason_code == "flood_wait"

    asyncio.run(scenario())
    assert len(calls) == 1


def test_explicit_telegram_denial_is_audited(db, monkeypatch):
    calls = _fake_worker(monkeypatch, failure=errors.ChatAdminRequiredError(None))

    async def scenario():
        result = await service.send_managed_reaction(7, "t.me/owner/56", "👍", 999)
        assert result["status"] == "failed" and result["reason"] == "telegram_denied"
        async with db() as session:
            audit = await session.scalar(select(ManagedReactionAttempt))
            assert audit.status == "failed" and audit.reason == "telegram_denied"

    asyncio.run(scenario())
    assert len(calls) == 1


def test_preview_requires_admin_and_exact_message(monkeypatch):
    account = SimpleNamespace(display_title="Owner")
    channel = Channel(id=123, title="My group", photo=None, date=None, megagroup=True)

    class Client:
        def __init__(self, participant, message):
            self.participant = participant
            self.message = message

        async def get_entity(self, _peer):
            return channel

        async def __call__(self, _request):
            return SimpleNamespace(participant=self.participant)

        async def get_messages(self, _entity, *, ids):
            return self.message

    async def scenario():
        with pytest.raises(service.ManagedReactionError, match="admin_required"):
            await service._target_details(
                Client(ChannelParticipantSelf(user_id=7, inviter_id=8, date=None), SimpleNamespace(id=42)),
                "owner", 42, "https://t.me/owner/42", "👍", account,
            )
        admin = ChannelParticipantAdmin(user_id=7, promoted_by=7, date=None, admin_rights=None)
        with pytest.raises(service.ManagedReactionError, match="message_missing"):
            await service._target_details(
                Client(admin, SimpleNamespace(id=41)),
                "owner", 42, "https://t.me/owner/42", "👍", account,
            )

    asyncio.run(scenario())
