"""Scheduled owned-chat posts use a durable, no-replay command per chat."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from telethon import errors

from database.models import Account, AccountChatCooldown, AccountSafetyState, AccountStatus, Base, BotCommand
from services import owned_chat_campaign as service


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'owned-chat.db'}")
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
                                status=AccountStatus.ACTIVE, daily_limit=5,
                                is_spam_blocked=False))
            await session.commit()

    asyncio.run(setup())
    yield scope
    asyncio.run(engine.dispose())


def _worker(monkeypatch, *, failure=None):
    calls = []

    class Client:
        async def send_message(self, entity, text):
            calls.append((entity.id, text))
            if failure:
                raise failure
            return SimpleNamespace(id=321)

    class Worker:
        def __init__(self):
            self.client = Client()
            self._send_lock = asyncio.Lock()

        async def connect(self, quiet=False):
            return True

        async def disconnect(self):
            return True

    async def resolve(_client, link, expected_peer_id=None):
        peer_id = 101 if link.endswith("ownedone") else 102
        if expected_peer_id is not None and expected_peer_id != peer_id:
            raise service.CampaignError("target_changed")
        return {"peer_id": peer_id, "title": "Owned", "entity": SimpleNamespace(id=peer_id)}

    monkeypatch.setattr(service, "account_worker_for_action", lambda *_args: Worker())
    monkeypatch.setattr(service, "_resolve_owned", resolve)
    return calls


def test_links_are_bounded_and_public_only():
    assert service.parse_links("https://t.me/ownedone\nhttps://t.me/ownedtwo") == [
        "https://t.me/ownedone", "https://t.me/ownedtwo",
    ]
    for raw in ("https://t.me/c/123", "https://t.me/ownedone\nhttps://t.me/ownedone",
                "https://t.me/one\n" * 6, "https://evil.example/ownedone"):
        with pytest.raises(service.CampaignError):
            service.parse_links(raw)
    with pytest.raises(service.CampaignError):
        service.validate_text("x" * 4001)


def test_ownership_check_rejects_non_admin_and_changed_target(monkeypatch):
    class FakeChannel:
        def __init__(self):
            self.id = 101
            self.title = "Owned"
            self.megagroup = True
            self.broadcast = False

    class FakeAdmin:
        pass

    class Client:
        def __init__(self, participant):
            self.participant = participant

        async def get_entity(self, username):
            assert username == "ownedone"
            return FakeChannel()

        async def __call__(self, _request):
            return SimpleNamespace(participant=self.participant)

    monkeypatch.setattr(service, "Channel", FakeChannel)
    monkeypatch.setattr(service, "ChannelParticipantAdmin", FakeAdmin)

    async def scenario():
        with pytest.raises(service.CampaignError, match="admin_required"):
            await service._resolve_owned(Client(object()), "https://t.me/ownedone")
        with pytest.raises(service.CampaignError, match="target_changed"):
            await service._resolve_owned(Client(FakeAdmin()), "https://t.me/ownedone", 999)
        assert (await service._resolve_owned(Client(FakeAdmin()), "https://t.me/ownedone", 101))["peer_id"] == 101

    asyncio.run(scenario())


def test_broadcast_admin_needs_post_rights_but_creator_is_allowed(monkeypatch):
    class FakeChannel:
        id = 101
        title = "Owned channel"
        megagroup = False
        broadcast = True

    class FakeAdmin:
        def __init__(self, post_messages):
            self.admin_rights = SimpleNamespace(post_messages=post_messages)

    class FakeCreator:
        pass

    class Client:
        def __init__(self, member):
            self.member = member

        async def get_entity(self, _username):
            return FakeChannel()

        async def __call__(self, _request):
            return SimpleNamespace(participant=self.member)

    monkeypatch.setattr(service, "Channel", FakeChannel)
    monkeypatch.setattr(service, "ChannelParticipantAdmin", FakeAdmin)
    monkeypatch.setattr(service, "ChannelParticipantCreator", FakeCreator)

    async def scenario():
        with pytest.raises(service.CampaignError, match="posting_rights_required"):
            await service._resolve_owned(Client(FakeAdmin(False)), "https://t.me/ownedone")
        assert (await service._resolve_owned(Client(FakeAdmin(True)), "https://t.me/ownedone"))["peer_id"] == 101
        assert (await service._resolve_owned(Client(FakeCreator()), "https://t.me/ownedone"))["peer_id"] == 101

    asyncio.run(scenario())


def test_preview_queue_stop_and_per_chat_history(db, monkeypatch):
    calls = _worker(monkeypatch)

    async def scenario():
        links = service.parse_links("https://t.me/ownedone\nhttps://t.me/ownedtwo")
        preview = await service.preview_campaign(7, links, "Hello owned chats")
        assert [item["peer_id"] for item in preview] == [101, 102]
        async with db() as session:
            assert await session.get(AccountSafetyState, 7) is None
        ids = await service.enqueue_campaign(7, 90, preview, "Hello owned chats", 3600)
        assert len(ids) == 2
        assert await service.stop_campaign(ids[0], 91) == 0
        assert await service.stop_campaign(ids[0], 90) == 2
        history = await service.campaign_history(90)
        assert len(history) == 2 and {row["status"] for row in history} == {"cancelled"}
        assert all(row["campaign_id"] == ids[0] for row in history)
        assert await service.campaign_history(91) == []

    asyncio.run(scenario())
    assert calls == []


def test_claimed_send_is_once_only_and_recovery_does_not_replay(db, monkeypatch):
    calls = _worker(monkeypatch, failure=TimeoutError("unknown Telegram outcome"))

    async def scenario():
        preview = [{"link": "https://t.me/ownedone", "peer_id": 101, "title": "Owned"}]
        ids = await service.enqueue_campaign(7, 90, preview, "One post", 0)
        command_id = ids[0]
        async with db() as session:
            row = await session.get(BotCommand, command_id)
            row.status = "processing"
            await session.commit()
        await service.process_campaign_send(command_id)
        await service.process_campaign_send(command_id)
        async with db() as session:
            row = await session.get(BotCommand, command_id)
            assert row.status == "failed" and row.error == "outcome_uncertain"
            state = await session.get(AccountSafetyState, 7)
            assert state.attempts_today == 1
        # Simulate a process dying after Telegram accepted a post but before checkpoint.
        another = (await service.enqueue_campaign(7, 90, preview, "Second post", 0))[0]
        async with db() as session:
            row = await session.get(BotCommand, another)
            row.status = "processing"
            await session.commit()
        await service.recover_campaign_sends(scope_factory=db)
        await service.process_campaign_send(another)
        async with db() as session:
            row = await session.get(BotCommand, another)
            assert row.status == "failed" and row.error == "outcome_uncertain_after_restart"

    asyncio.run(scenario())
    assert calls == [(101, "One post")]


def test_send_rechecks_target_and_admin_before_budget(db, monkeypatch):
    calls = _worker(monkeypatch)

    async def changed(_client, _link, expected_peer_id=None):
        raise service.CampaignError("admin_required")

    async def scenario():
        preview = [{"link": "https://t.me/ownedone", "peer_id": 101, "title": "Owned"}]
        command_id = (await service.enqueue_campaign(7, 90, preview, "Message", 0))[0]
        async with db() as session:
            row = await session.get(BotCommand, command_id)
            row.status = "processing"
            await session.commit()
        monkeypatch.setattr(service, "_resolve_owned", changed)
        await service.process_campaign_send(command_id)
        async with db() as session:
            row = await session.get(BotCommand, command_id)
            assert row.status == "failed" and row.error == "admin_required"
            assert await session.get(AccountSafetyState, 7) is None

    asyncio.run(scenario())
    assert calls == []


def test_stop_claimed_send_before_rpc(db, monkeypatch):
    calls = _worker(monkeypatch)

    async def scenario():
        preview = [{"link": "https://t.me/ownedone", "peer_id": 101, "title": "Owned"}]
        command_id = (await service.enqueue_campaign(7, 90, preview, "Message", 0))[0]
        async with db() as session:
            row = await session.get(BotCommand, command_id)
            row.status = "processing"
            await session.commit()
        assert await service.stop_campaign(command_id, 90) == 1
        await service.process_campaign_send(command_id)
        async with db() as session:
            row = await session.get(BotCommand, command_id)
            assert row.status == "cancelled" and row.error == "stopped"
            assert await session.get(AccountSafetyState, 7) is None

    asyncio.run(scenario())
    assert calls == []


@pytest.mark.parametrize("failure,reason,gate_state", [
    (errors.SlowModeWaitError(None, 30), "slow_mode", None),
    (errors.PeerFloodError(None), "peer_flood", "review_required"),
    (errors.AuthKeyUnregisteredError(None), "auth_invalid", "needs_reauth"),
])
def test_rpc_restrictions_persist_without_replay(db, monkeypatch, failure, reason, gate_state):
    calls = _worker(monkeypatch, failure=failure)

    async def scenario():
        preview = [{"link": "https://t.me/ownedone", "peer_id": 101, "title": "Owned"}]
        command_id = (await service.enqueue_campaign(7, 90, preview, "Message", 0))[0]
        async with db() as session:
            row = await session.get(BotCommand, command_id)
            row.status = "processing"
            await session.commit()
        await service.process_campaign_send(command_id)
        await service.process_campaign_send(command_id)
        async with db() as session:
            row = await session.get(BotCommand, command_id)
            assert row.status == "failed" and row.error == reason
            state = await session.get(AccountSafetyState, 7)
            assert state.attempts_today == 1
            if gate_state:
                assert state.state == gate_state and state.reason_code == reason
            else:
                assert state.state == "ready"
                cooldown = await session.get(AccountChatCooldown, (7, "101"))
                assert cooldown.reason_code == "slow_mode"

    asyncio.run(scenario())
    assert calls == [(101, "Message")]


def test_gate_denial_finishes_claimed_command(db, monkeypatch):
    calls = _worker(monkeypatch)

    async def scenario():
        preview = [{"link": "https://t.me/ownedone", "peer_id": 101, "title": "Owned"}]
        command_id = (await service.enqueue_campaign(7, 90, preview, "Message", 0))[0]
        async with db() as session:
            row = await session.get(BotCommand, command_id)
            row.status = "processing"
            session.add(AccountSafetyState(account_id=7, state="review_required",
                                           reason_code="manual_pause"))
            await session.commit()
        await service.process_campaign_send(command_id)
        async with db() as session:
            row = await session.get(BotCommand, command_id)
            assert row.status == "failed" and row.error == "manual_pause"

    asyncio.run(scenario())
    assert calls == []
