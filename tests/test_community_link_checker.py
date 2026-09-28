"""Offline contract tests for the public community ownership checker."""
import asyncio
from types import SimpleNamespace

import pytest
from contextlib import asynccontextmanager
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from database.models import Base, CommunityLinkCheck
from telethon.tl.types import (
    Channel,
    ChannelParticipantAdmin,
    ChannelParticipantCreator,
    ChannelParticipant,
)

from services import community_link_checker as checker
from database.models import AccountStatus


def test_parse_batch_mixed_rows_preserves_first_unique_order(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("pure batch parser must not access Telegram")

    monkeypatch.setattr(checker, "session_scope", forbidden)
    monkeypatch.setattr(checker, "account_worker_for_action", forbidden)
    result = checker.parse_community_link_batch(
        "https://t.me/PublicNews\nnot a link\nhttps://t.me/publicnews\n"
        "https://t.me/SecondGroup\nhttps://t.me/+invite"
    )
    assert result == {
        "links": ["https://t.me/PublicNews", "https://t.me/SecondGroup"],
        "errors": [
            {"line": 2, "reason": "invalid_link"},
            {"line": 5, "reason": "invalid_link"},
        ],
        "duplicates": [{"line": 3, "reason": "duplicate_link"}],
    }


@pytest.mark.parametrize("raw", ["\n".join(["bad"] * 21), "x" * 4097])
def test_parse_batch_rejects_limits_before_parsing(monkeypatch, raw):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("limit must be checked before row parsing")

    monkeypatch.setattr(checker, "_parse_public_link", forbidden)
    with pytest.raises(ValueError):
        checker.parse_community_link_batch(raw)


def test_parse_batch_accepts_exact_limits():
    raw = "\n".join(f"https://t.me/group_{index:02d}" for index in range(20))
    raw = raw + " " * (4096 - len(raw))
    result = checker.parse_community_link_batch(raw)
    assert len(result["links"]) == 20
    assert result["errors"] == []
    assert result["duplicates"] == []


class FakeClient:
    def __init__(self, entity, participant=None, error=None):
        self.entity = entity
        self.participant = participant
        self.error = error
        self.calls = []

    async def get_entity(self, username):
        self.calls.append(("get_entity", username))
        if self.error:
            raise self.error
        return self.entity

    async def __call__(self, request):
        self.calls.append(("participant", request))
        if self.error:
            raise self.error
        return SimpleNamespace(participant=self.participant)


class FakeWorker:
    def __init__(self, client):
        self.client = client
        self.is_connected = False
        self.disconnected = False

    async def connect(self, *, quiet=False):
        self.is_connected = True
        return True

    async def disconnect(self):
        self.disconnected = True
        self.is_connected = False
        return True


@pytest.fixture
def setup_checker(tmp_path, monkeypatch):
    session_file = tmp_path / "acc.session"
    session_file.touch()
    account = SimpleNamespace(
        id=7, session_name="acc", proxy=object(), status=AccountStatus.ACTIVE,
        display_title="Example account",
        username="owner",
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async def initialize():
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    asyncio.run(initialize())

    @asynccontextmanager
    async def scope():
        async with maker() as session:
            yield session

    async def get_account(_session, account_id):
        return account if account_id == 7 else None

    monkeypatch.setattr(checker, "session_scope", scope)
    monkeypatch.setattr(checker.AccountRepository, "get_by_id", get_account)
    monkeypatch.setattr(checker, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(checker, "account_worker_for_action", lambda *_args: worker)

    worker = None
    return account, session_file, maker


@pytest.mark.parametrize(
    ("entity", "participant", "expected_kind"),
    [
        (Channel(id=1, title="Owned channel", username="public_news", photo=None,
                 date=None, creator=False, left=False, broadcast=True, megagroup=False,
                 gigagroup=False, scam=False, has_link=False, forum=False),
         ChannelParticipantAdmin(user_id=1, inviter_id=1, date=None, can_edit=True,
                                 admin_rights=None, promoted_by=1), "channel"),
        (Channel(id=2, title="Owned group", username="public_group", photo=None,
                 date=None, creator=False, left=False, broadcast=False, megagroup=True,
                 gigagroup=False, scam=False, has_link=False, forum=False),
         ChannelParticipantCreator(user_id=1, admin_rights=None, rank=None), "supergroup"),
    ],
)
def test_owned_channel_and_supergroup_succeed(setup_checker, monkeypatch, entity, participant, expected_kind):
    _account, _session_file, _maker = setup_checker
    client = FakeClient(entity, participant)
    worker = FakeWorker(client)
    monkeypatch.setattr(checker, "account_worker_for_action", lambda *_args: worker)

    result = asyncio.run(checker.check_owned_community_link(7, f"https://t.me/{entity.username}"))

    assert result["status"] == "ok"
    assert result["reason"] == "ok"
    assert result["canonical_link"] == f"https://t.me/{entity.username}"
    assert result["title"] == entity.title
    assert result["kind"] == expected_kind
    assert result["username"] == entity.username
    assert result["account_name"] == "Example account"
    assert result["checked_at"]
    assert [call[0] for call in client.calls] == ["get_entity", "participant"]
    assert worker.disconnected


def test_non_admin_fails(setup_checker, monkeypatch):
    _account, _session_file, _maker = setup_checker
    entity = Channel(id=3, title="Other", username="other_group", photo=None, date=None,
                     creator=False, left=False, broadcast=False, megagroup=True,
                     gigagroup=False, scam=False, has_link=False, forum=False)
    client = FakeClient(entity, ChannelParticipant(user_id=1, date=None))
    monkeypatch.setattr(checker, "account_worker_for_action", lambda *_args: FakeWorker(client))

    result = asyncio.run(checker.check_owned_community_link(7, "https://t.me/other_group"))

    assert result["status"] == "failed"
    assert result["reason"] == "unavailable_or_not_owned"
    assert result["kind"] is None
    assert result["title"] is None


@pytest.mark.parametrize("link", [
    "http://t.me/public_news", "https://telegram.me/public_news", "https://t.me/abcd",
    "https://t.me/" + "a" * 33, "https://t.me/+invitecode", "https://t.me/joinchat/abcde",
    "https://t.me/user/123", "https://t.me/+79990000000", "https://t.me/public_news?x=1",
    "https://t.me/public_news?", "https://t.me/public_news#x", "https://t.me/public_news#",
    "https://t.me/public_news/", "https://evil.test/t.me/public_news",
    "https://t.me/joinchat",
])
def test_invalid_link_rejected_before_database_or_telegram(monkeypatch, link):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("invalid link must be rejected before account access")

    monkeypatch.setattr(checker, "session_scope", forbidden)
    result = asyncio.run(checker.check_owned_community_link(7, link))
    assert result["status"] == "failed"
    assert result["reason"] == "invalid_link"


def test_unavailable_target_fails_without_sensitive_exception_text(setup_checker, monkeypatch):
    _account, _session_file, _maker = setup_checker
    client = FakeClient(None, error=RuntimeError("private exception detail"))
    worker = FakeWorker(client)
    monkeypatch.setattr(checker, "account_worker_for_action", lambda *_args: worker)

    result = asyncio.run(checker.check_owned_community_link(7, "https://www.t.me/public_news"))

    assert result["status"] == "failed"
    assert result["reason"] == "unavailable_or_not_owned"
    assert "private exception detail" not in str(result)
    assert worker.disconnected


def test_history_persists_success_and_failure_without_foreign_channel_data(setup_checker, monkeypatch):
    _account, _session_file, maker = setup_checker
    entity = Channel(id=9, title="Secret чужого канала", username="foreign_news", photo=None,
                     date=None, creator=False, left=False, broadcast=True, megagroup=False,
                     gigagroup=False, scam=False, has_link=False, forum=False)
    client = FakeClient(entity, ChannelParticipant(user_id=4, date=None))
    monkeypatch.setattr(checker, "account_worker_for_action", lambda *_args: FakeWorker(client))

    result = asyncio.run(checker.check_owned_community_link(7, "https://t.me/foreign_news", actor_id=55))
    history = asyncio.run(checker.list_owned_community_link_checks())

    assert result["status"] == "failed"
    assert len(history) == 1
    assert history[0]["account_id"] == 7
    assert history[0]["actor_id"] == 55
    assert history[0]["canonical_link"] == "https://t.me/foreign_news"
    assert history[0]["reason"] == "unavailable_or_not_owned"
    assert history[0]["title"] is None and history[0]["kind"] is None
    assert "Secret чужого канала" not in str(history)


def test_history_filter_order_and_bounded_limit(setup_checker, monkeypatch):
    _account, _session_file, maker = setup_checker
    entity = Channel(id=1, title="Mine", username="public_news", photo=None,
                     date=None, creator=False, left=False, broadcast=True, megagroup=False,
                     gigagroup=False, scam=False, has_link=False, forum=False)
    monkeypatch.setattr(checker, "account_worker_for_action", lambda *_args: FakeWorker(
        FakeClient(entity, ChannelParticipantAdmin(user_id=1, inviter_id=1, date=None,
                   can_edit=True, admin_rights=None, promoted_by=1))))
    async def add(account, link, actor=None):
        async with maker() as session:
            session.add(CommunityLinkCheck(account_id=account, actor_id=actor, canonical_link=link,
                status="failed", reason="unavailable_or_not_owned"))
            await session.commit()
    asyncio.run(add(8, "https://t.me/other_news", actor=8))
    asyncio.run(checker.check_owned_community_link(7, "https://t.me/public_news", actor_id=7))
    asyncio.run(checker.check_owned_community_link(7, "https://t.me/public_news", actor_id=7))
    filtered = asyncio.run(checker.list_owned_community_link_checks(limit=500, account_id=7))
    scoped = asyncio.run(checker.list_owned_community_link_checks(actor_id=7))
    other = asyncio.run(checker.list_owned_community_link_checks(actor_id=8))
    limited = asyncio.run(checker.list_owned_community_link_checks(limit=1))
    assert len(filtered) == 2
    assert len(scoped) == 2 and all(row["actor_id"] == 7 for row in scoped)
    assert len(other) == 1 and other[0]["canonical_link"] == "https://t.me/other_news"
    assert all(row["account_id"] == 7 for row in filtered)
    assert filtered[0]["title"] == "Mine" and filtered[0]["kind"] == "channel"
    assert filtered[0]["id"] > filtered[1]["id"]
    assert len(limited) == 1
    assert limited[0]["id"] == filtered[0]["id"]
