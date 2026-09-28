"""Operator-specific unread state is persistent, scoped and monotonic."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import sessionmaker

from control_plane.business.db import get_bot_db
from control_plane.business.schemas import DialogMarkReadIn
from control_plane.deps import get_current_user
from control_plane.routes.business import mark_dialog_read, router
from database.models import Account, Base, DialogReadCursor, NeuroChatMessage
from database.repository import Database
from database.sqlite_pragmas import register_sqlite_pragmas


def _setup(tmp_path):
    path = tmp_path / "unread.db"
    engine = create_engine(
        f"sqlite:///{path}", connect_args={"timeout": 30, "check_same_thread": False},
    )
    register_sqlite_pragmas(engine)
    Base.metadata.create_all(engine)
    maker = sessionmaker(engine, expire_on_commit=False)
    with maker.begin() as db:
        db.add_all([
            Account(phone="+10000000011", session_name="inbox-a"),
            Account(phone="+10000000012", session_name="inbox-b"),
        ])
        db.flush()
        db.add_all([
            NeuroChatMessage(account_id=1, peer_user_id=101, role="user", content="one"),
            NeuroChatMessage(account_id=1, peer_user_id=101, role="assistant", content="reply"),
            NeuroChatMessage(account_id=1, peer_user_id=101, role="user", content="two"),
            NeuroChatMessage(account_id=1, peer_user_id=102, role="user", content="question"),
            NeuroChatMessage(account_id=1, peer_user_id=102, role="assistant", content="answer"),
            NeuroChatMessage(account_id=2, peer_user_id=101, role="user", content="other account"),
        ])
    app = FastAPI()
    app.include_router(router)

    def test_db():
        with maker() as db:
            yield db

    app.dependency_overrides[get_bot_db] = test_db
    return path, engine, maker, app


def test_unread_is_per_operator_account_and_incoming_message(tmp_path):
    _path, engine, maker, app = _setup(tmp_path)
    owner = SimpleNamespace(id=10, username="owner", role="tenant_admin")
    viewer = SimpleNamespace(id=11, username="viewer", role="tenant_viewer")
    app.dependency_overrides[get_current_user] = lambda: owner
    try:
        with TestClient(app) as client:
            base = "/business/accounts/1/dialogs"
            initial = {item["peer_user_id"]: item for item in client.get(base).json()}
            assert initial[101]["unread_count"] == 2
            assert initial[102]["unread_count"] == 1
            assert initial[102]["waiting_for_reply"] is False

            with maker() as db:
                incoming_ids = list(db.execute(
                    select(NeuroChatMessage.id).where(
                        NeuroChatMessage.account_id == 1,
                        NeuroChatMessage.peer_user_id == 101,
                        NeuroChatMessage.role == "user",
                    ).order_by(NeuroChatMessage.id)
                ).scalars())
            marked = client.post(
                f"{base}/101/read", json={"through_message_id": incoming_ids[0]},
            )
            assert marked.status_code == 200
            assert marked.json()["unread_count"] == 1
            assert marked.json()["last_read_message_id"] == incoming_ids[0]
            assert client.post(f"{base}/101/read", json={"through_message_id": 2}).status_code == 400

            unread = {item["peer_user_id"]: item for item in client.get(
                base, params={"unread_only": "true"},
            ).json()}
            assert set(unread) == {101, 102}
            assert unread[101]["unread_count"] == 1
            assert [item["peer_user_id"] for item in client.get(
                base, params={"unread_only": "true", "waiting_only": "true"},
            ).json()] == [101]

            app.dependency_overrides[get_current_user] = lambda: viewer
            assert client.get(base).json()[0]["unread_count"] >= 1
            assert client.post(f"{base}/101/read").status_code == 403
            assert client.get(f"{base}/101/messages").status_code == 200

            app.dependency_overrides[get_current_user] = lambda: owner
            assert client.post(f"{base}/101/read").json()["unread_count"] == 0
            with maker.begin() as db:
                db.add(NeuroChatMessage(
                    account_id=1, peer_user_id=101, role="user", content="new after read",
                ))
            assert {item["peer_user_id"]: item["unread_count"] for item in client.get(
                base, params={"unread_only": "true"},
            ).json()} == {101: 1, 102: 1}
            assert client.get("/business/accounts/2/dialogs").json()[0]["unread_count"] == 1

        with maker() as db:
            cursors = db.execute(select(DialogReadCursor)).scalars().all()
            assert len(cursors) == 1
            assert (cursors[0].operator_user_id, cursors[0].account_id, cursors[0].peer_user_id) == (
                10, 1, 101,
            )
    finally:
        engine.dispose()


def test_parallel_read_updates_never_move_cursor_backward(tmp_path):
    _path, engine, maker, _app = _setup(tmp_path)
    user = SimpleNamespace(id=10, username="owner", role="tenant_admin")
    with maker() as db:
        inbound_ids = db.execute(select(NeuroChatMessage.id).where(
            NeuroChatMessage.account_id == 1,
            NeuroChatMessage.peer_user_id == 101,
            NeuroChatMessage.role == "user",
        ).order_by(NeuroChatMessage.id)).scalars().all()
    barrier = Barrier(2)

    def mark(target_id):
        with maker() as db:
            barrier.wait(timeout=5)
            return mark_dialog_read(
                1, 101, DialogMarkReadIn(through_message_id=target_id), db, user,
            )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(mark, [inbound_ids[0], inbound_ids[-1]]))
        assert len(results) == 2
        with maker() as db:
            cursor = db.get(DialogReadCursor, (10, 1, 101))
            assert cursor.last_read_message_id == inbound_ids[-1]
        with maker() as db:
            assert mark_dialog_read(
                1, 101, DialogMarkReadIn(through_message_id=inbound_ids[0]), db, user,
            ).last_read_message_id == inbound_ids[-1]
    finally:
        engine.dispose()


def test_existing_database_adds_read_cursor_table_idempotently(tmp_path):
    path, engine, _maker, _app = _setup(tmp_path)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE dialog_read_cursors"))
    engine.dispose()

    async def migrate_twice():
        repository = Database(f"sqlite+aiosqlite:///{path}")
        repository.engine = create_async_engine(repository.url)
        try:
            await repository._run_migrations()
            await repository._run_migrations()
            async with repository.engine.connect() as conn:
                columns = (await conn.execute(text("PRAGMA table_info(dialog_read_cursors)"))).all()
            return {row[1] for row in columns}
        finally:
            await repository.engine.dispose()

    columns = asyncio.run(migrate_twice())
    assert {"operator_user_id", "account_id", "peer_user_id", "last_read_message_id"} <= columns
