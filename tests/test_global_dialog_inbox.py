"""The global inbox keeps account and operator state separate."""

from datetime import datetime, timedelta
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from control_plane.business.db import get_bot_db
from control_plane.deps import get_current_user
from control_plane.routes.business import router
from database.models import (
    Account, Base, Client, DialogReadCursor, NeuroChatMessage, OutboundQueue,
)


def test_global_inbox_search_order_filters_and_access(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'global-inbox.db'}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(engine)
    now = datetime(2026, 9, 26, 12, 0, 0)
    with maker.begin() as db:
        db.add_all([
            Account(phone="+10000000101", session_name="global-a"),
            Account(phone="+10000000102", session_name="global-b"),
            Client(username="alice", telegram_user_id=101),
            Client(username="bob", telegram_user_id=102),
        ])
        db.flush()
        db.add_all([
            NeuroChatMessage(account_id=1, peer_user_id=101, role="user", content="old needle %_", created_at=now),
            NeuroChatMessage(account_id=1, peer_user_id=101, role="assistant", content="answered", created_at=now + timedelta(minutes=1)),
            NeuroChatMessage(account_id=2, peer_user_id=101, role="user", content="new question", created_at=now + timedelta(minutes=3)),
            NeuroChatMessage(account_id=1, peer_user_id=102, role="user", content="waiting", created_at=now + timedelta(minutes=2)),
            NeuroChatMessage(account_id=2, peer_user_id=102, role="user", content="queued", created_at=now + timedelta(minutes=4)),
            OutboundQueue(account_id=2, peer_user_id=102, text="reply", status="uncertain"),
        ])
        db.flush()
        read_id = db.execute(select(NeuroChatMessage.id).where(
            NeuroChatMessage.account_id == 2, NeuroChatMessage.peer_user_id == 101,
        )).scalar_one()
        db.add(DialogReadCursor(operator_user_id=10, account_id=2, peer_user_id=101,
                                last_read_message_id=read_id))

    app = FastAPI()
    app.include_router(router)

    def test_db():
        with maker() as db:
            yield db

    app.dependency_overrides[get_bot_db] = test_db
    owner = SimpleNamespace(id=10, username="owner", role="tenant_admin")
    viewer = SimpleNamespace(id=11, username="viewer", role="tenant_viewer")
    app.dependency_overrides[get_current_user] = lambda: owner
    try:
        with TestClient(app) as client:
            path = "/business/dialogs"
            result = client.get(path)
            assert result.status_code == 200
            assert result.headers["cache-control"] == "no-store"
            rows = result.json()
            assert [(r["account_id"], r["peer_user_id"]) for r in rows] == [
                (2, 102), (2, 101), (1, 102), (1, 101),
            ]
            first_page = client.get(path, params={"limit": 2})
            second_page = client.get(path, params={"limit": 2, "offset": 2})
            assert first_page.status_code == second_page.status_code == 200
            assert first_page.headers["cache-control"] == "no-store"
            assert second_page.headers["cache-control"] == "no-store"
            assert first_page.json() == rows[:2]
            assert second_page.json() == rows[2:]
            assert {r["account_id"] for r in first_page.json()} == {2}
            assert not ({(r["account_id"], r["peer_user_id"]) for r in first_page.json()}
                        & {(r["account_id"], r["peer_user_id"]) for r in second_page.json()})
            assert client.get(path, params={"limit": 2, "offset": 4}).json() == []
            assert rows[-1]["messages_count"] == 2
            assert rows[-1]["client_username"] == "alice"
            assert [(r["account_id"], r["peer_user_id"]) for r in client.get(
                path, params={"q": "needle"},
            ).json()] == [(1, 101)]
            assert len(client.get(path, params={"q": "@ALI"}).json()) == 2
            assert len(client.get(path, params={"q": "102"}).json()) == 2
            assert [(r["account_id"], r["peer_user_id"]) for r in client.get(
                path, params={"q": "%_"},
            ).json()] == [(1, 101)]
            assert client.get(path, params={"q": "%missing_"}).json() == []
            assert [(r["account_id"], r["peer_user_id"]) for r in client.get(
                path, params={"waiting_only": True},
            ).json()] == [(2, 101), (1, 102)]
            assert [(r["account_id"], r["peer_user_id"]) for r in client.get(
                path, params={"unread_only": True},
            ).json()] == [(2, 102), (1, 102), (1, 101)]
            assert [(r["account_id"], r["peer_user_id"]) for r in client.get(
                path, params={"waiting_only": True, "unread_only": True},
            ).json()] == [(1, 102)]
            assert [(r["account_id"], r["peer_user_id"]) for r in client.get(
                path, params={"waiting_only": True, "limit": 1, "offset": 1},
            ).json()] == [(1, 102)]
            assert [(r["account_id"], r["peer_user_id"]) for r in client.get(
                path, params={"unread_only": True, "limit": 1, "offset": 1},
            ).json()] == [(1, 102)]
            assert client.get(path, params={"q": "needle", "offset": 1}).json() == []
            for status in ("pending", "sending"):
                with maker.begin() as db:
                    db.execute(select(OutboundQueue)).scalar_one().status = status
                assert [(r["account_id"], r["peer_user_id"]) for r in client.get(
                    path, params={"waiting_only": True},
                ).json()] == [(2, 101), (1, 102)]
            app.dependency_overrides[get_current_user] = lambda: viewer
            viewer_rows = client.get(path, params={"unread_only": True}).json()
            assert (2, 101) in {(r["account_id"], r["peer_user_id"]) for r in viewer_rows}
            assert client.get(path, params={"limit": 1}).json() == rows[:1]
            assert client.get(path, params={"limit": 0}).status_code == 422
            assert client.get(path, params={"limit": 201}).status_code == 422
            assert client.get(path, params={"offset": -1}).status_code == 422
            assert client.get(path, params={"offset": 10001}).status_code == 422
            assert client.get(path, params={"q": "x" * 101}).status_code == 422
            app.dependency_overrides.pop(get_current_user)
            assert client.get(path).status_code in (401, 403)
    finally:
        engine.dispose()
