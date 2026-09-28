"""Visible message/comment evidence survives parsing and is readable in the panel."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

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
    Base, ClientContactPermission, ParsedUser, ParsedUserSource,
    ParsingFilterReasonCount, ParsingTask,
)
from workers.parser.collect_users import (
    _collect_channel_commenters_from_posts, _collect_from_messages,
)


class FakeMessage:
    def __init__(self, message_id, sender, date):
        self.id = message_id
        self.sender_id = sender.id
        self.date = date
        self.sender = sender

    async def get_sender(self):
        return self.sender


class FakeClient:
    def __init__(self, posts, comments):
        self.posts = posts
        self.comments = comments

    async def iter_messages(self, entity, *, limit, reply_to=None):
        for message in self.comments if reply_to is not None else self.posts:
            yield message


def test_message_and_comment_sources_keep_event_provenance(tmp_path):
    db_path = tmp_path / "provenance.db"

    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with maker() as session:
                session.add(ParsingTask(kind="users", status="running"))
                await session.commit()
                person = User(id=500, first_name="Owned", username="owned_user")
                older = datetime(2026, 9, 20, tzinfo=timezone.utc)
                newer = datetime(2026, 9, 21, tzinfo=timezone.utc)
                async def log(*args, **kwargs):
                    return None
                client = FakeClient([FakeMessage(41, person, older)], [])
                count = await _collect_from_messages(
                    client, object(), task_id=1, account_id=1,
                    source_entity_id=700, source_entity_kind="group",
                    source_kind="active", session=session, user_flt={}, log=log,
                )
                assert count == 1
                client.posts = [FakeMessage(42, person, newer)]
                await _collect_from_messages(
                    client, object(), task_id=1, account_id=1,
                    source_entity_id=700, source_entity_kind="group",
                    source_kind="active", session=session, user_flt={}, log=log,
                )
                # A later crawl of older history must retain the newer event.
                client.posts = [FakeMessage(40, person, older)]
                await _collect_from_messages(
                    client, object(), task_id=1, account_id=1,
                    source_entity_id=700, source_entity_kind="group",
                    source_kind="active", session=session, user_flt={}, log=log,
                )
                post = SimpleNamespace(id=80)
                client.posts = [post]
                client.comments = [FakeMessage(81, person, newer)]
                await _collect_channel_commenters_from_posts(
                    client, object(), task_id=1, account_id=1,
                    source_entity_id=800, session=session, user_flt={}, log=log,
                )
                rows = (await session.execute(select(ParsedUserSource).order_by(
                    ParsedUserSource.source_entity_id
                ))).scalars().all()
                assert len(rows) == 2
                assert (rows[0].source_kind, rows[0].message_id, rows[0].message_at) == (
                    "active", 42, newer.replace(tzinfo=None),
                )
                assert (rows[1].source_kind, rows[1].message_id, rows[1].post_id) == (
                    "commenter", 81, 80,
                )
                assert rows[0].observed_at is not None
                assert (await session.execute(select(ParsedUser))).scalars().one().telegram_id == 500
                assert (await session.execute(select(ClientContactPermission))).scalars().all() == []
        finally:
            await engine.dispose()

    asyncio.run(scenario())

    sync_engine = create_engine(f"sqlite:///{db_path}")
    maker = sessionmaker(sync_engine, expire_on_commit=False)
    app = FastAPI()
    app.include_router(router)

    def test_db():
        with maker() as db:
            yield db

    app.dependency_overrides[get_bot_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(username="owner")
    try:
        with TestClient(app) as client:
            response = client.get("/business/parsing/results/users/1/sources")
            assert response.status_code == 200
            assert {row["source_kind"] for row in response.json()} == {"active", "commenter"}
            assert next(row for row in response.json() if row["source_kind"] == "commenter")["post_id"] == 80
            assert client.get("/business/parsing/results/users/999/sources").status_code == 404
    finally:
        sync_engine.dispose()


def test_existing_parser_and_mailing_tables_get_additive_columns(tmp_path):
    from sqlalchemy import text
    from database.repository import Database

    db_path = tmp_path / "migration.db"

    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await conn.execute(text("DROP INDEX uq_mailing_runs_one_queued"))
                for table, columns in {
                    "mailing_runs": ["scheduled_at"],
                    "bot_commands": ["not_before"],
                    "parsed_user_sources": ["message_id", "post_id", "message_at", "observed_at"],
                }.items():
                    for column in columns:
                        await conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
            database = Database(f"sqlite+aiosqlite:///{db_path}")
            database.engine = engine
            await database._run_migrations()
            await database._run_migrations()
            async with engine.connect() as conn:
                for table, columns in {
                    "mailing_runs": {"scheduled_at"},
                    "bot_commands": {"not_before"},
                    "parsed_user_sources": {"message_id", "post_id", "message_at", "observed_at"},
                }.items():
                    info = await conn.execute(text(f"PRAGMA table_info({table})"))
                    assert columns <= {row[1] for row in info.fetchall()}
                index = await conn.execute(text(
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND name='uq_mailing_runs_one_queued'"
                ))
                assert index.scalar_one() == "uq_mailing_runs_one_queued"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_user_filter_reasons_are_aggregated_and_exposed(tmp_path):
    db_path = tmp_path / "filters.db"

    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with maker() as session:
                session.add(ParsingTask(kind="users", status="running"))
                await session.commit()
                person = User(id=777, first_name="NoPhoto", username="no_photo")
                client = FakeClient([FakeMessage(
                    10, person, datetime(2026, 9, 21, tzinfo=timezone.utc),
                )], [])
                async def log(*args, **kwargs):
                    return None
                for _ in range(2):
                    assert await _collect_from_messages(
                        client, object(), task_id=1, account_id=1,
                        source_entity_id=700, source_entity_kind="group",
                        source_kind="active", session=session,
                        user_flt={"require_avatar": True}, log=log,
                    ) == 0
                reason = await session.get(ParsingFilterReasonCount, (1, "no_avatar"))
                assert reason.count == 2
                assert (await session.get(ParsingTask, 1)).filtered_count == 2
                assert (await session.execute(select(ParsedUser))).scalars().all() == []
        finally:
            await engine.dispose()

    asyncio.run(scenario())
    engine = create_engine(f"sqlite:///{db_path}")
    maker = sessionmaker(engine)
    app = FastAPI()
    app.include_router(router)

    def test_db():
        with maker() as db:
            yield db

    app.dependency_overrides[get_bot_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(username="owner")
    try:
        with TestClient(app) as client:
            assert client.get("/business/parsing/tasks/1/filter-reasons").json() == [
                {"reason": "no_avatar", "count": 2},
            ]
            assert client.get("/business/parsing/tasks/999/filter-reasons").status_code == 404
    finally:
        engine.dispose()
