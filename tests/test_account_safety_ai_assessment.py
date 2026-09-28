"""AI assessment uses only bounded observations and never writes safety state."""

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from control_plane.business import account_safety
from control_plane.business.db import get_bot_db
from control_plane.deps import require_operator_write
from database.models import (
    Account, AccountHealthCheck, AccountSafetyEvent, AccountSafetyState, AccountStatus,
    Base, BotCommand, Proxy,
)
from services.neurochat.provider_registry import ProviderRuntime
from utils.time import utcnow_naive


def _client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    maker = sessionmaker(engine, expire_on_commit=False)
    with maker() as db:
        proxy = Proxy(name="private-proxy", host="private.proxy.example", port=1080,
                      username="proxy-user", password="proxy-password")
        db.add(proxy)
        db.flush()
        account = Account(phone="+15551234567", username="private_user",
                          session_name="private-session-content", status=AccountStatus.ACTIVE,
                          proxy_id=proxy.id, daily_limit=10)
        db.add(account)
        db.flush()
        db.add(AccountSafetyState(account_id=account.id, state="review_required",
                                  reason_code="peer_flood", day_utc=utcnow_naive().date().isoformat(),
                                  attempts_today=3))
        db.add(AccountSafetyEvent(account_id=account.id, event_type="paused", reason_code="peer_flood",
                                  source="operator", actor="private-actor", peer_ref="private-peer"))
        command = BotCommand(command="account_safety.health_check", args_json="{}", status="done",
                             requested_by="private-actor")
        db.add(command)
        db.flush()
        db.add(AccountHealthCheck(account_id=account.id, command_id=command.id, status="done",
                                  proxy_state="ok", auth_state="ok", reason_code="ok",
                                  requested_by="private-actor"))
        db.commit()
        account_id = account.id
    app = FastAPI()
    app.include_router(account_safety.router)

    def test_db():
        with maker() as db:
            yield db

    app.dependency_overrides[get_bot_db] = test_db
    app.dependency_overrides[require_operator_write] = lambda: SimpleNamespace(username="operator")
    return TestClient(app), maker, account_id, engine


def test_assessment_prompt_is_secret_free_and_read_only(monkeypatch):
    client, maker, account_id, engine = _client()
    captured = {}
    monkeypatch.setattr(account_safety, "resolve_default_provider_sync", lambda _db: ProviderRuntime(
        "Test", "https://provider.example", "test-model", "provider-secret", is_openrouter=True,
    ))

    async def fake_generate(messages, model, **kwargs):
        captured.update(messages=messages, model=model, kwargs=kwargs)
        return "Observed safety stop. https://unsafe.example/path Contact me at foo@example.com", None

    monkeypatch.setattr(account_safety, "generate_reply_with_retries_and_fallback", fake_generate)
    try:
        with maker() as db:
            before = (db.get(AccountSafetyState, account_id).state,
                      len(db.scalars(select(AccountSafetyEvent)).all()),
                      len(db.scalars(select(BotCommand)).all()))
        response = client.post(f"/business/account-safety/{account_id}/ai-assessment")
        assert response.status_code == 200
        data = response.json()
        assert data["account_id"] == account_id
        assert data["observed"]["attempts_today"] == 3
        assert data["observed"]["proxy_assigned"] is True
        assert "predict" in data["disclaimer"]
        assert "http" not in data["assessment"]
        assert "foo@example.com" not in data["assessment"]
        prompt = str(captured["messages"])
        for secret in ("+15551234567", "private_user", "private.proxy.example",
                       "proxy-password", "private-session-content", "private-peer", "private-actor",
                       "provider-secret"):
            assert secret not in prompt
            assert secret not in str(data)
        assert captured["kwargs"]["generation"]["max_tokens"] <= 300
        with maker() as db:
            after = (db.get(AccountSafetyState, account_id).state,
                     len(db.scalars(select(AccountSafetyEvent)).all()),
                     len(db.scalars(select(BotCommand)).all()))
        assert after == before
    finally:
        client.close()
        engine.dispose()


def test_assessment_requires_configured_provider(monkeypatch):
    client, _maker, account_id, engine = _client()
    monkeypatch.setattr(account_safety, "resolve_default_provider_sync", lambda _db: ProviderRuntime(
        "Test", "https://provider.example", "test-model", "", is_openrouter=True,
    ))
    try:
        assert client.post(f"/business/account-safety/{account_id}/ai-assessment").status_code == 409
    finally:
        client.close()
        engine.dispose()


def test_assessment_missing_provider(monkeypatch):
    client, _maker, account_id, engine = _client()

    def missing(_db):
        raise ValueError("default AI provider is missing")

    monkeypatch.setattr(account_safety, "resolve_default_provider_sync", missing)
    try:
        response = client.post(f"/business/account-safety/{account_id}/ai-assessment")
        assert response.status_code == 409
        assert response.json()["detail"] == "AI provider unavailable"
    finally:
        client.close()
        engine.dispose()


def test_assessment_unavailable_model(monkeypatch):
    client, _maker, account_id, engine = _client()
    monkeypatch.setattr(account_safety, "resolve_default_provider_sync", lambda _db: ProviderRuntime(
        "Test", "https://provider.example", "test-model", "provider-secret", is_openrouter=True,
    ))

    async def unavailable(*_args, **_kwargs):
        return None, "provider unavailable"

    monkeypatch.setattr(account_safety, "generate_reply_with_retries_and_fallback", unavailable)
    try:
        response = client.post(f"/business/account-safety/{account_id}/ai-assessment")
        assert response.status_code == 502
        assert "provider-secret" not in response.text
    finally:
        client.close()
        engine.dispose()
