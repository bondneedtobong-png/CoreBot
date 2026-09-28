"""Inbox waiting state reflects the last delivered message and queued replies."""

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from control_plane.business.db import get_bot_db
from control_plane.deps import get_current_user
from control_plane.routes.business import router
from database.models import Account, Base, Client, NeuroChatMessage, OutboundQueue


def test_waiting_filter_excludes_dialogs_with_an_outgoing_reply(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'dialogs.db'}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(engine)
    with maker.begin() as session:
        session.add(Account(phone="+10000000001", session_name="dialog-waiting"))
        session.flush()
        session.add_all([
            Client(username="alice", telegram_user_id=11),
            NeuroChatMessage(account_id=1, peer_user_id=11, role="user", content="Need reply"),
            NeuroChatMessage(account_id=1, peer_user_id=12, role="user", content="Question"),
            NeuroChatMessage(account_id=1, peer_user_id=12, role="assistant", content="Answered"),
            NeuroChatMessage(account_id=1, peer_user_id=13, role="user", content="In queue"),
            OutboundQueue(account_id=1, peer_user_id=13, text="Queued reply", status="pending"),
        ])

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
            path = "/business/accounts/1/dialogs"
            response = client.get(path)
            assert response.status_code == 200
            states = {row["peer_user_id"]: row["waiting_for_reply"] for row in response.json()}
            assert states == {11: True, 12: False, 13: False}
            assert [row["peer_user_id"] for row in client.get(
                path, params={"waiting_only": "true"},
            ).json()] == [11]
            assert [row["peer_user_id"] for row in client.get(
                path, params={"q": "@ALI"},
            ).json()] == [11]
            assert [row["peer_user_id"] for row in client.get(
                path, params={"q": "12"},
            ).json()] == [12]
            assert [row["peer_user_id"] for row in client.get(
                path, params={"q": "answered"},
            ).json()] == [12]
            assert client.get(path, params={"q": "Question"}).json() == []
            assert client.get(path, params={"q": "%_"}).json() == []
            assert [row["peer_user_id"] for row in client.get(
                path, params={"q": "Need", "waiting_only": "true"},
            ).json()] == [11]
            assert client.get(path, params={"q": "x" * 101}).status_code == 422

            with maker.begin() as session:
                queued = session.execute(select(OutboundQueue)).scalar_one()
                queued.status = "failed"
            assert {row["peer_user_id"] for row in client.get(
                path, params={"waiting_only": "true"},
            ).json()} == {11, 13}
    finally:
        engine.dispose()
