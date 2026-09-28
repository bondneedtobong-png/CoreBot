"""Local bulk metadata edits validate the whole batch before committing."""

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from control_plane.business.db import get_bot_db
from control_plane.deps import get_current_user
from control_plane.routes.business import router
from database.models import Account, Base, Group, account_groups
from database.sqlite_pragmas import register_sqlite_pragmas


def _app(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'bulk-metadata.db'}",
        connect_args={"check_same_thread": False},
    )
    register_sqlite_pragmas(engine)
    Base.metadata.create_all(engine)
    maker = sessionmaker(engine, expire_on_commit=False)
    with maker.begin() as db:
        db.add_all([
            Account(phone="+10000000401", session_name="bulk-1", tags=",USA,"),
            Account(phone="+10000000402", session_name="bulk-2", tags="," + "x" * 490 + ","),
            Group(name="one"), Group(name="two"),
        ])

    app = FastAPI()
    app.include_router(router)

    def test_db():
        with maker() as db:
            yield db

    app.dependency_overrides[get_bot_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=1, username="owner", role="tenant_admin",
    )
    return app, engine, maker


def test_bulk_metadata_add_remove_idempotent_and_scoped(tmp_path):
    app, engine, maker = _app(tmp_path)
    endpoint = "/business/accounts/bulk-metadata"
    try:
        with TestClient(app) as client:
            body = {"account_ids": [1, 1], "action": "add", "tags": ["main", "MAIN"],
                    "group_ids": [1, 1]}
            first = client.post(endpoint, json=body)
            assert first.status_code == 200, first.text
            assert first.json()["accounts_changed"] == 1
            assert first.json()["tags_changed_accounts"] == 1
            assert first.json()["group_memberships_added"] == 1
            again = client.post(endpoint, json=body)
            assert again.status_code == 200
            assert again.json()["accounts_changed"] == 0
            assert again.json()["group_memberships_added"] == 0

            removed = client.post(endpoint, json={
                "account_ids": [1], "action": "remove", "tags": ["usa"],
                "group_ids": [1],
            })
            assert removed.status_code == 200
            assert removed.json()["group_memberships_removed"] == 1
            with maker() as db:
                assert db.get(Account, 1).tags == ",main,"
                assert not db.execute(select(account_groups).where(
                    account_groups.c.account_id == 1,
                )).all()
    finally:
        engine.dispose()


def test_bulk_metadata_rejects_unknown_ids_and_rolls_back_overflow(tmp_path):
    app, engine, maker = _app(tmp_path)
    endpoint = "/business/accounts/bulk-metadata"
    try:
        with TestClient(app) as client:
            assert client.post(endpoint, json={
                "account_ids": [1, 999], "action": "add", "tags": ["main"],
            }).status_code == 404
            assert client.post(endpoint, json={
                "account_ids": [1], "action": "add", "group_ids": [999],
            }).status_code == 404
            assert client.post(endpoint, json={
                "account_ids": [1], "action": "add", "tags": ["bad,tag"],
            }).status_code == 400
            assert client.post(endpoint, json={
                "account_ids": [1], "action": "add",
            }).status_code == 400
            overflow = client.post(endpoint, json={
                "account_ids": [1, 2], "action": "add",
                "tags": ["more-than-ten"], "group_ids": [1],
            })
            assert overflow.status_code == 400
            with maker() as db:
                assert db.get(Account, 1).tags == ",USA,"
                assert db.get(Account, 2).tags == "," + "x" * 490 + ","
                assert not db.execute(select(account_groups)).all()

            app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
                id=2, username="viewer", role="tenant_viewer",
            )
            assert client.post(endpoint, json={
                "account_ids": [1], "action": "add", "tags": ["main"],
            }).status_code == 403
    finally:
        engine.dispose()
