# -*- coding: utf-8 -*-
"""Настройки рассылки из бота в панели: PATCH новых полей + тест-получатели."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from control_plane.business import mailings as mailings_mod
from control_plane.business.db import get_bot_db
from control_plane.deps import get_current_user, require_operator_write
from database.models import Base, Client, Mailing, MailingTestRecipient


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
    app.include_router(mailings_mod.router)
    app.dependency_overrides[get_bot_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: admin
    app.dependency_overrides[require_operator_write] = lambda: admin
    return TestClient(app, raise_server_exceptions=False), maker


def _make_mailing(maker) -> int:
    with maker() as db:
        m = Mailing(name="t", message_text="hi")
        db.add(m)
        db.commit()
        db.refresh(m)
        return int(m.id)


def test_patch_new_safety_fields():
    client, maker = _client()
    mid = _make_mailing(maker)
    r = client.patch(
        f"/business/mailings/{mid}",
        json={
            "use_typing": False,
            "smart_delay": True,
            "variant_mode": "sequential",
            "max_recipients": 500,
            "mailing_cooldown_hours": 6.5,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["use_typing"] is False
    assert body["smart_delay"] is True
    assert body["variant_mode"] == "sequential"
    assert body["max_recipients"] == 500
    assert body["mailing_cooldown_hours"] == 6.5


def test_patch_audience_filter_fields():
    client, maker = _client()
    mid = _make_mailing(maker)
    r = client.patch(
        f"/business/mailings/{mid}",
        json={
            "audience_client_status": "open",
            "audience_include_classes": ["accept", "alive"],
            "audience_exclude_classes": ["bl", "dead"],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["audience_client_status"] == "open"
    assert body["audience_include_classes"] == ["accept", "alive"]
    assert body["audience_exclude_classes"] == ["bl", "dead"]
    # Частичное обновление не трогает остальные ключи фильтра.
    r2 = client.patch(
        f"/business/mailings/{mid}", json={"audience_client_status": "new"}
    )
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["audience_client_status"] == "new"
    assert body2["audience_exclude_classes"] == ["bl", "dead"]


def test_patch_max_recipients_zero_means_none():
    client, maker = _client()
    mid = _make_mailing(maker)
    client.patch(f"/business/mailings/{mid}", json={"max_recipients": 10})
    r = client.patch(f"/business/mailings/{mid}", json={"max_recipients": 0})
    assert r.status_code == 200, r.text
    assert r.json()["max_recipients"] is None


def test_test_recipients_replace_normalize_dedup():
    client, maker = _client()
    mid = _make_mailing(maker)
    r = client.put(
        f"/business/mailings/{mid}/test-recipients",
        json={"usernames": ["@Alice", "alice", "bob_123", "бот", "x", "tg_user"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["usernames"] == ["alice", "bob_123", "tg_user"]
    assert body["unique"] == 3
    assert body["duplicates"] == 1
    assert body["created_clients"] == 3
    with maker() as db:
        assert db.query(Client).count() == 3
        assert db.query(MailingTestRecipient).count() == 3
    # Повторная загрузка — полная замена, без роста.
    r2 = client.put(
        f"/business/mailings/{mid}/test-recipients", json={"usernames": ["@Alice"]}
    )
    assert r2.json()["unique"] == 1
    with maker() as db:
        assert db.query(MailingTestRecipient).count() == 1
        assert db.query(Client).count() == 3  # клиенты не удаляются
    got = client.get(f"/business/mailings/{mid}/test-recipients").json()
    assert got["usernames"] == ["alice"]
