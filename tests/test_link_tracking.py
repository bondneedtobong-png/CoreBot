# -*- coding: utf-8 -*-
"""Трекинг-ссылки: CRUD + публичный редирект /r/{code} с записью хитов."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from control_plane.business import links as links_mod
from control_plane.business.db import get_bot_db
from control_plane.deps import get_current_user, require_operator_write
from database.models import Base, LinkHit, TrackedLink


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
    app.include_router(links_mod.router)
    app.dependency_overrides[get_bot_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: admin
    app.dependency_overrides[require_operator_write] = lambda: admin
    return TestClient(app, raise_server_exceptions=False), maker


def test_create_and_list_link():
    client, _ = _client()
    r = client.post(
        "/business/links",
        json={"name": "канал", "target_url": "https://t.me/+abcdef"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["code"] and body["clicks_total"] == 0 and body["clicks_24h"] == 0
    rows = client.get("/business/links").json()
    assert len(rows) == 1 and rows[0]["code"] == body["code"]


def test_create_rejects_non_http_target():
    client, _ = _client()
    r = client.post("/business/links", json={"name": "x", "target_url": "t.me/abc"})
    assert r.status_code == 400


def test_redirect_logs_hit_and_counts():
    client, maker = _client()
    code = client.post(
        "/business/links",
        json={"name": "k", "target_url": "https://t.me/+abcdef"},
    ).json()["code"]
    r = client.get(f"/r/{code}", follow_redirects=False)
    assert r.status_code == 307, r.status_code
    assert r.headers["location"] == "https://t.me/+abcdef"
    client.get(f"/r/{code}", follow_redirects=False)
    with maker() as db:
        assert db.query(LinkHit).count() == 2
    rows = client.get("/business/links").json()
    assert rows[0]["clicks_total"] == 2 and rows[0]["clicks_24h"] == 2


def test_redirect_unknown_code_404():
    client, _ = _client()
    r = client.get("/r/nonexistent", follow_redirects=False)
    assert r.status_code == 404


def test_delete_link_removes_hits():
    client, maker = _client()
    link_id = client.post(
        "/business/links",
        json={"name": "d", "target_url": "https://t.me/+abcdef"},
    ).json()["id"]
    code = client.get("/business/links").json()[0]["code"]
    client.get(f"/r/{code}", follow_redirects=False)
    assert client.delete(f"/business/links/{link_id}").status_code == 204
    with maker() as db:
        assert db.query(TrackedLink).count() == 0
        assert db.query(LinkHit).count() == 0
