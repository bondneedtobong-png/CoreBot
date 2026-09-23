# -*- coding: utf-8 -*-
"""Импорт клиентов: POST /business/clients/import (@username, как в боте)."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from control_plane.business import clients as clients_mod
from control_plane.business.db import get_bot_db
from control_plane.deps import get_current_user, require_operator_write
from database.models import Base, Client


def _client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    def _override_db():
        db = maker()
        try:
            yield db
        finally:
            db.close()

    admin = SimpleNamespace(role="super_admin")
    app = FastAPI()
    app.include_router(clients_mod.router)
    app.dependency_overrides[get_bot_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: admin
    app.dependency_overrides[require_operator_write] = lambda: admin
    return TestClient(app, raise_server_exceptions=False), maker


def test_import_usernames_with_and_without_at():
    client, maker = _client()
    r = client.post(
        "/business/clients/import",
        json={"usernames": ["@Alice", "bob_123", "@Alice", "мусор!!!", "botfather"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added"] == 2
    assert body["skipped_duplicates"] == 1
    assert body["invalid"] == 2
    with maker() as db:
        names = sorted(c.username for c in db.query(Client).all())
    assert names == ["alice", "bob_123"]


def test_import_is_idempotent():
    client, maker = _client()
    payload = {"usernames": ["@carol"]}
    assert client.post("/business/clients/import", json=payload).json()["added"] == 1
    second = client.post("/business/clients/import", json=payload).json()
    assert second["added"] == 0 and second["skipped_duplicates"] == 1
    with maker() as db:
        assert db.query(Client).count() == 1
