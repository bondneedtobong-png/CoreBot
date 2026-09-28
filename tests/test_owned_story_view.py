"""The one-channel, one-story path uses fake clients; no Telegram traffic."""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from telethon import errors
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.tl.functions.stories import GetStoriesByIDRequest, IncrementStoryViewsRequest
from telethon.tl.types import Channel, ChannelParticipantAdmin, ChannelParticipantSelf, StoryItem

from database.models import Account, AccountSafetyState, AccountStatus, Base, OwnedStoryViewAttempt
from database.repository import migrate_owned_story_view_attempts
from services import owned_story_view as service
from utils.time import utcnow_naive


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'story.db'}")
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
            session.add(Account(id=7, phone="+70000000007", session_name="seven",
                                username="owner", status=AccountStatus.ACTIVE,
                                daily_limit=5, is_spam_blocked=False))
            await session.commit()

    asyncio.run(setup())
    yield scope
    asyncio.run(engine.dispose())


def _worker(monkeypatch, *, expires=None, admin=True, view_result=True):
    calls = []
    if expires is None:
        expires = utcnow_naive() + timedelta(hours=2)

    class Client:
        async def get_entity(self, username):
            assert username == "ownedchannel"
            return Channel(id=123, title="Owned channel", photo=None, date=utcnow_naive(), broadcast=True,
                           megagroup=False, access_hash=456)

        async def __call__(self, request):
            calls.append(request)
            if isinstance(request, GetParticipantRequest):
                participant = (ChannelParticipantAdmin(user_id=7, inviter_id=7,
                               promoted_by=7, date=utcnow_naive(), admin_rights=None, rank="")
                               if admin else ChannelParticipantSelf(user_id=7, inviter_id=7,
                                                                     date=utcnow_naive()))
                return SimpleNamespace(participant=participant)
            if isinstance(request, GetStoriesByIDRequest):
                return SimpleNamespace(stories=[StoryItem(id=42, date=utcnow_naive(),
                                                           expire_date=expires, media=None)])
            if isinstance(request, IncrementStoryViewsRequest):
                if isinstance(view_result, Exception):
                    raise view_result
                return view_result
            raise AssertionError(type(request))

    class Worker:
        def __init__(self):
            self.client = Client()
            self._send_lock = asyncio.Lock()
            self._connection_lock = asyncio.Lock()

        async def disconnect(self):
            return True

    async def get_worker(_account):
        return Worker()

    monkeypatch.setattr(service, "_worker_for", get_worker)
    return calls


def test_link_is_exact_public_story_only():
    assert service._parse_link("t.me/ownedchannel/s/42") == (
        "ownedchannel", 42, "https://t.me/ownedchannel/s/42")
    for link in ("https://evil.example/ownedchannel/s/42", "http://t.me/ownedchannel/s/42",
                 "https://t.me/ownedchannel/s/42?q=1", "t.me/ownedchannel/42",
                 "t.me/c/123/s/42", "t.me/ownedchannel/s/0"):
        with pytest.raises(service.OwnedStoryViewError):
            service._parse_link(link)


def test_preview_is_read_only_and_exact_story_rpc(db, monkeypatch):
    calls = _worker(monkeypatch)

    async def scenario():
        preview = await service.preview_owned_story_view(7, "t.me/ownedchannel/s/42")
        assert preview["peer_id"] == 123 and preview["story_id"] == 42
        assert preview["already_attempted"] is False
        async with db() as session:
            assert await session.get(AccountSafetyState, 7) is None
            assert await session.scalar(select(OwnedStoryViewAttempt.id)) is None

    asyncio.run(scenario())
    assert [type(x) for x in calls] == [GetParticipantRequest, GetStoriesByIDRequest]
    assert calls[-1].id == [42]


def test_single_view_is_accepted_and_deduped_durably(db, monkeypatch):
    calls = _worker(monkeypatch)

    async def scenario():
        first = await service.view_owned_story(7, "t.me/ownedchannel/s/42", 999,
                                               expected_peer_id=123)
        assert first["status"] == "accepted"
        second = await service.view_owned_story(7, "t.me/ownedchannel/s/42", 999,
                                                expected_peer_id=123)
        assert second["status"] == "skipped" and second["reason"] == "already_attempted"
        assert (await service.preview_owned_story_view(7, "t.me/ownedchannel/s/42"))["already_attempted"]
        history = await service.list_owned_story_view_history(999)
        assert len(history) == 1 and history[0]["status"] == "accepted"
        assert await service.list_owned_story_view_history(1) == []
        async with db() as session:
            assert (await session.get(AccountSafetyState, 7)).attempts_today == 1

    asyncio.run(scenario())
    views = [x for x in calls if isinstance(x, IncrementStoryViewsRequest)]
    assert len(views) == 1 and views[0].id == [42]


def test_foreign_or_expired_story_cannot_be_viewed(db, monkeypatch):
    calls = _worker(monkeypatch, admin=False)
    with pytest.raises(service.OwnedStoryViewError, match="admin_required"):
        asyncio.run(service.preview_owned_story_view(7, "t.me/ownedchannel/s/42"))
    assert not any(isinstance(x, IncrementStoryViewsRequest) for x in calls)

    calls = _worker(monkeypatch, expires=utcnow_naive() - timedelta(seconds=1))
    result = asyncio.run(service.view_owned_story(7, "t.me/ownedchannel/s/42", 999,
                                                  expected_peer_id=123))
    assert result["reason"] == "story_expired"
    assert not any(isinstance(x, IncrementStoryViewsRequest) for x in calls)


def test_changed_target_and_uncertain_rpc_do_not_retry(db, monkeypatch):
    calls = _worker(monkeypatch, view_result=TimeoutError("uncertain"))

    async def scenario():
        changed = await service.view_owned_story(7, "t.me/ownedchannel/s/42", 999,
                                                 expected_peer_id=999)
        assert changed == {"status": "failed", "reason": "target_changed"}
        first = await service.view_owned_story(7, "t.me/ownedchannel/s/42", 999,
                                               expected_peer_id=123)
        assert first["reason"] == "outcome_uncertain"
        second = await service.view_owned_story(7, "t.me/ownedchannel/s/42", 999,
                                                expected_peer_id=123)
        assert second["reason"] == "already_attempted"
        async with db() as session:
            row = await session.scalar(select(OwnedStoryViewAttempt))
            assert row.status == "uncertain"

    asyncio.run(scenario())
    assert len([x for x in calls if isinstance(x, IncrementStoryViewsRequest)]) == 1


def test_flood_wait_pauses_account(db, monkeypatch):
    calls = _worker(monkeypatch, view_result=errors.FloodWaitError(None, 45))

    async def scenario():
        result = await service.view_owned_story(7, "t.me/ownedchannel/s/42", 999,
                                                expected_peer_id=123)
        assert result["reason"] == "flood_wait" and result["retry_after"] == 45
        async with db() as session:
            assert (await session.get(AccountSafetyState, 7)).state == "cooling_down"

    asyncio.run(scenario())
    assert len([x for x in calls if isinstance(x, IncrementStoryViewsRequest)]) == 1


def test_migration_is_additive_and_idempotent(tmp_path):
    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'migration.db'}")
        try:
            async with engine.begin() as conn:
                await migrate_owned_story_view_attempts(conn)
                await migrate_owned_story_view_attempts(conn)
                columns = (await conn.execute(text(
                    "PRAGMA table_info(owned_story_view_attempts)"
                ))).all()
                assert {row[1] for row in columns} >= {
                    "account_id", "peer_id", "story_id", "expires_at", "status",
                }
        finally:
            await engine.dispose()

    asyncio.run(scenario())
