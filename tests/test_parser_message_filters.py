"""Message filters affect visible authors and preserve evidence, not members."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker
from telethon.tl.types import User

from control_plane.business.db import get_bot_db
from control_plane.business.parsing import router
from control_plane.deps import get_current_user
from database.models import (
    Account, Base, ParsedUser, ParsedUserSource, ParsingFilterReasonCount, ParsingTask,
)
from workers.parser import collect_users, filters


class FakeMessage:
    def __init__(self, message_id, sender, date, text):
        self.id = message_id
        self.sender_id = sender.id
        self.date = date
        self.raw_text = text
        self.sender = sender

    async def get_sender(self):
        return self.sender


class FakeClient:
    def __init__(self, *, members=(), messages=(), posts=(), comments=()):
        self.members = members
        self.messages = messages
        self.posts = posts
        self.comments = comments

    async def iter_participants(self, entity, **kwargs):
        for user in self.members:
            yield user

    async def iter_messages(self, entity, *, limit, reply_to=None):
        items = self.comments if reply_to is not None else self.messages
        for message in items[:limit]:
            yield message

    async def get_entity(self, peer):
        return SimpleNamespace(id=700, megagroup=True, broadcast=False)


class FakePool:
    def __init__(self, client):
        self.client = client

    async def next_client(self):
        return 1, self.client


async def silent_log(*args, **kwargs):
    return None


def test_message_filter_normalizes_keywords_and_timezones():
    normalized, errors = filters.normalize_message_filters({
        "keywords": ["  Cats  ", "cats", "DOGS"],
        "from_at": "2026-09-20T12:00:00+03:00",
        "to_at": "2026-09-20T09:30:00Z",
    })
    assert errors == []
    assert normalized == {
        "keywords": ["Cats", "DOGS"],
        "from_at": "2026-09-20T09:00:00+00:00",
        "to_at": "2026-09-20T09:30:00+00:00",
    }
    assert filters.message_passes_filters(
        "big DOGS", datetime(2026, 9, 20, 9, tzinfo=timezone.utc), normalized,
    ) == (True, "")
    assert filters.message_passes_filters(
        "birds", datetime(2026, 9, 20, 9, tzinfo=timezone.utc), normalized,
    ) == (False, "message_keyword_no_match")
    assert filters.message_passes_filters(
        "cats", datetime(2026, 9, 20, 9, 30), normalized,
    ) == (True, "")
    assert filters.message_passes_filters(
        "cats", datetime(2026, 9, 20, 9, 30, 1, tzinfo=timezone.utc), normalized,
    ) == (False, "message_after_to")
    assert filters.message_passes_filters(
        "cats", datetime(2026, 9, 20, 8, 59, tzinfo=timezone.utc), normalized,
    ) == (False, "message_before_from")
    assert filters.message_passes_filters(
        "cats", None, normalized,
    ) == (False, "message_date_missing")


@pytest.mark.parametrize("raw,reason", [
    ({"keywords": "cats"}, "invalid_type"),
    ({"keywords": [""]}, "empty_keyword"),
    ({"keywords": ["x" * 81]}, "keyword_too_long"),
    ({"from_at": "2026-09-20T12:00:00"}, "timezone_required"),
    ({"from_at": "not-a-date"}, "invalid_datetime"),
    ({"from_at": "2026-09-21T00:00:00Z", "to_at": "2026-09-20T00:00:00Z"}, "invalid_range"),
    ({"other": True}, "unknown_field"),
])
def test_invalid_message_filter_config_is_reported(raw, reason):
    _, errors = filters.normalize_message_filters(raw)
    assert any(error["reason"] == reason for error in errors)


def test_group_active_filters_message_authors_but_not_members(tmp_path, monkeypatch):
    db_path = tmp_path / "message-filter.db"

    class FakeChannel:
        def __init__(self):
            self.id = 700
            self.megagroup = True
            self.broadcast = False

    monkeypatch.setattr(collect_users, "Channel", FakeChannel)

    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with maker() as session:
                task = ParsingTask(kind="users", status="running", params_json={
                    "user_inputs_text": "group_one",
                    "source_options": {"group_members": True, "group_active": True},
                    "message_filters": {
                        "keywords": ["cats"],
                        "from_at": "2026-09-20T09:00:00+00:00",
                    },
                })
                session.add(task)
                await session.commit()
                member = User(id=501, first_name="Member", username="member_one")
                author = User(id=502, first_name="Author", username="author_one")
                client = FakeClient(
                    members=[member],
                    messages=[
                        FakeMessage(10, author, datetime(2026, 9, 20, 8, tzinfo=timezone.utc), "cats"),
                        FakeMessage(11, author, datetime(2026, 9, 20, 9, tzinfo=timezone.utc), "birds"),
                        FakeMessage(12, author, datetime(2026, 9, 20, 9, tzinfo=timezone.utc), "CATS"),
                    ],
                )
                async def get_group(peer):
                    return FakeChannel()
                client.get_entity = get_group
                await collect_users.run_users_task(session, task, FakePool(client), silent_log)
                await session.refresh(task)
                assert task.error_count == 0
                assert task.found_count == 2
                assert task.filtered_count == 2
                users = (await session.execute(select(ParsedUser))).scalars().all()
                assert {user.telegram_id for user in users} == {501, 502}
                edges = (await session.execute(select(ParsedUserSource))).scalars().all()
                assert {(edge.source_kind, edge.message_id) for edge in edges} == {
                    ("member", None), ("active", 12),
                }
                active = next(edge for edge in edges if edge.source_kind == "active")
                assert active.message_at == datetime(2026, 9, 20, 9)
                reasons = (await session.execute(select(ParsingFilterReasonCount))).scalars().all()
                assert {row.reason: row.count for row in reasons} == {
                    "message_before_from": 1, "message_keyword_no_match": 1,
                }
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_comment_filter_preserves_accepted_comment_event(tmp_path):
    db_path = tmp_path / "comment-filter.db"

    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with maker() as session:
                task = ParsingTask(kind="users", status="running")
                session.add(task)
                await session.commit()
                author = User(id=503, first_name="Comment", username="comment_one")
                class CommentClient(FakeClient):
                    async def iter_messages(self, entity, *, limit, reply_to=None):
                        if reply_to is None:
                            yield SimpleNamespace(id=80)
                        else:
                            yield FakeMessage(81, author, datetime(2026, 9, 20, tzinfo=timezone.utc), "birds")
                            yield FakeMessage(82, author, datetime(2026, 9, 21, tzinfo=timezone.utc), "cats")
                count = await collect_users._collect_channel_commenters_from_posts(
                    CommentClient(), object(), task_id=task.id, account_id=1,
                    source_entity_id=800, session=session, user_flt={}, log=silent_log,
                    message_flt={"keywords": ["cats"]},
                )
                assert count == 1
                edge = (await session.execute(select(ParsedUserSource))).scalars().one()
                assert (edge.source_kind, edge.message_id, edge.post_id) == ("commenter", 82, 80)
                reason = await session.get(ParsingFilterReasonCount, (task.id, "message_keyword_no_match"))
                assert reason.count == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_create_task_validates_and_normalizes_message_filters(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'message-filter-api.db'}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(engine)
    with maker.begin() as session:
        session.add(Account(phone="+10000000001", session_name="message-filter-test"))
    app = FastAPI()
    app.include_router(router)

    def test_db():
        with maker() as session:
            yield session

    app.dependency_overrides[get_bot_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        username="owner", id=1, role="tenant_admin",
    )
    try:
        with TestClient(app) as client:
            payload = {"kind": "users", "account_ids": [1], "params": {
                "user_inputs_text": "@group_one",
                "message_filters": {"keywords": [" Cats ", "cats"],
                                    "from_at": "2026-09-20T12:00:00+03:00"},
            }}
            accepted = client.post("/business/parsing/tasks", json=payload)
            assert accepted.status_code == 201
            assert accepted.json()["params"]["message_filters"] == {
                "keywords": ["Cats"], "from_at": "2026-09-20T09:00:00+00:00",
            }
            payload["params"]["message_filters"]["from_at"] = "2026-09-20T12:00:00"
            rejected = client.post("/business/parsing/tasks", json=payload)
            assert rejected.status_code == 422
            assert rejected.json()["detail"]["errors"][0]["reason"] == "timezone_required"
            with maker() as session:
                assert len(session.execute(select(ParsingTask)).scalars().all()) == 1
    finally:
        engine.dispose()
