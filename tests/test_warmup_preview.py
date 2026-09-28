"""Warmup preview explains stored eligibility without making Telegram calls."""
from datetime import datetime, timedelta
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from control_plane.business import warmup_report as report_module
from control_plane.business.db import get_bot_db
from control_plane.deps import get_current_user
from database.models import (
    Account, AccountSafetyState, AccountStatus, Base, WarmupProfile,
)


def _make_client(maker):
    app = FastAPI()
    app.include_router(report_module.router)

    def test_db():
        with maker() as db:
            yield db

    app.dependency_overrides[get_bot_db] = test_db
    return app, TestClient(app)


def test_preview_readiness_schedule_and_target_requirements(monkeypatch):
    now = datetime(2026, 9, 25, 12, 0)
    monkeypatch.setattr(report_module, "utcnow_naive", lambda: now)
    monkeypatch.setattr(report_module, "WARMUP_ENABLED", True)
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(engine, expire_on_commit=False)
    try:
        with maker() as db:
            db.add_all([
                WarmupProfile(name="safe", enabled=True, daily_action_limit=5,
                              base_delay_sec=30, jitter_sec=5,
                              time_zone="UTC", work_start_hour=18, work_end_hour=3,
                              target_chats_text="https://t.me/\n"),
                Account(phone="+15550200001", session_name="preview-one",
                        status=AccountStatus.ACTIVE, warmup_enabled=True,
                        warmup_profile="safe", warmup_actions_today=2,
                        daily_limit=3, last_reset=now,
                        warmup_last_action_at=now - timedelta(hours=1),
                        warmup_next_run_at=now - timedelta(minutes=1)),
            ])
            db.commit()

        app, client = _make_client(maker)
        with client:
            assert client.get("/business/warmup/preview?account_id=1").status_code == 401
            app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(role="tenant_viewer")
            response = client.get("/business/warmup/preview?account_id=1")
            assert response.status_code == 200
            body = response.json()
            assert body["observed_at_utc"] == "2026-09-25T12:00:00Z"
            assert body["ready_now"] is True
            assert body["next_eligible_at_utc"] == body["observed_at_utc"]
            assert body["profile"] == {
                "selected_name": "safe", "effective_name": "safe",
                "source": "selected", "enabled": True, "target_count": 0,
                "base_delay_sec": 30.0, "jitter_sec": 5.0,
                "time_zone": "UTC", "work_start_hour": 18, "work_end_hour": 3,
                "allowed_actions": ["read_channels", "read_dialogs"],
            }
            assert body["warmup_daily"] == {
                "limit": 5, "used": 2, "remaining": 3,
                "persisted_used": 2, "reset_on_next_tick": False,
            }
            assert body["safety_gate"]["allowed"] is True
            actions = {row["name"]: row for row in body["actions"]}
            assert actions["read_dialogs"]["available_now"] is True
            assert actions["read_channels"]["unavailable_reasons"] == ["no_targets"]
            assert actions["set_reaction"]["unavailable_reasons"] == ["action_not_allowed", "no_targets"]
            assert "short_reply" not in actions
            assert "https://t.me/" not in response.text

            assert client.get("/business/warmup/preview?account_id=999").status_code == 404
            assert client.get("/business/warmup/preview?account_id=0").status_code == 422
            assert client.post("/business/warmup/preview?account_id=1").status_code == 405
            with maker() as db:
                db.get(Account, 1).warmup_next_run_at = now
                db.commit()
            boundary = client.get("/business/warmup/preview?account_id=1").json()
            assert boundary["ready_now"] is False
            assert boundary["next_eligible_at_utc"] == "2026-09-25T12:00:00.000001Z"

        with maker() as db:
            assert db.scalar(select(func.count(AccountSafetyState.account_id))) == 0
            assert db.get(Account, 1).warmup_actions_today == 2
    finally:
        engine.dispose()


def test_preview_timed_and_persistent_blocks_with_profile_fallback(monkeypatch):
    now = datetime(2026, 9, 25, 12, 0)
    monkeypatch.setattr(report_module, "utcnow_naive", lambda: now)
    monkeypatch.setattr(report_module, "WARMUP_ENABLED", True)
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(engine, expire_on_commit=False)
    try:
        with maker() as db:
            db.add(WarmupProfile(name="safe", enabled=True, daily_action_limit=4,
                                 time_zone="UTC", work_start_hour=18, work_end_hour=3,
                                 target_chats_text="@owned\nhttps://t.me/other/post\n"))
            db.add_all([
                Account(phone="+15550200002", session_name="preview-timed",
                        status=AccountStatus.ACTIVE, warmup_enabled=True,
                        warmup_profile="missing", warmup_actions_today=9,
                        last_reset=now - timedelta(days=2), daily_limit=2,
                        warmup_next_run_at=now + timedelta(minutes=10),
                        warmup_paused_until=now + timedelta(minutes=30),
                        flood_wait_until=now + timedelta(minutes=20)),
                Account(phone="+15550200003", session_name="preview-stopped",
                        status=AccountStatus.ACTIVE, warmup_enabled=False,
                        warmup_profile="safe", warmup_actions_today=4,
                        warmup_last_action_at=now - timedelta(hours=1),
                        last_reset=now, daily_limit=2),
                Account(phone="+15550200006", session_name="preview-send-budget",
                        status=AccountStatus.ACTIVE, warmup_enabled=True,
                        warmup_profile="safe", last_reset=now, daily_limit=2),
            ])
            db.flush()
            db.add(AccountSafetyState(
                account_id=2, state="needs_reauth", reason_code="auth_invalid",
                day_utc=now.date().isoformat(), attempts_today=2,
                resume_at=now + timedelta(hours=1),
            ))
            db.add(AccountSafetyState(
                account_id=3, state="ready", day_utc=now.date().isoformat(),
                attempts_today=2,
            ))
            db.commit()

        app, client = _make_client(maker)
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(role="tenant_viewer")
        with client:
            timed = client.get("/business/warmup/preview?account_id=1").json()
            assert timed["profile"]["source"] == "safe_fallback"
            assert timed["profile"]["target_count"] == 2
            assert timed["warmup_daily"]["persisted_used"] == 9
            assert timed["warmup_daily"]["used"] == 0
            assert timed["warmup_daily"]["reset_on_next_tick"] is True
            assert timed["safety_gate"]["reason"] == "platform_restricted"
            assert timed["ready_now"] is False
            assert timed["next_eligible_at_utc"] == "2026-09-25T12:30:00.000001Z"
            assert timed["actions"][0]["unavailable_reasons"] == ["scheduled_later"]

            stopped = client.get("/business/warmup/preview?account_id=2").json()
            assert stopped["blocking_reasons"] == [
                "warmup_disabled", "auth_invalid", "warmup_daily_limit",
            ]
            assert stopped["next_eligible_at_utc"] is None
            assert stopped["safety_gate"]["resume_at_utc"] == "2026-09-25T13:00:00Z"
            assert stopped["outbound_daily"]["remaining"] == 0
            assert all(not action["available_now"] for action in stopped["actions"])

            budget = client.get("/business/warmup/preview?account_id=3").json()
            assert budget["safety_gate"]["allowed"] is True
            assert budget["ready_now"] is True
            budget_actions = {row["name"]: row for row in budget["actions"]}
            assert budget_actions["read_dialogs"]["available_now"] is True
            assert budget_actions["set_reaction"]["unavailable_reasons"] == [
                "action_not_allowed", "outbound_daily_limit",
            ]
    finally:
        engine.dispose()


def test_preview_missing_profile_and_disabled_profile_are_explained(monkeypatch):
    now = datetime(2026, 9, 25, 12, 0)
    monkeypatch.setattr(report_module, "utcnow_naive", lambda: now)
    monkeypatch.setattr(report_module, "WARMUP_ENABLED", True)
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(engine, expire_on_commit=False)
    try:
        with maker() as db:
            db.add_all([
                Account(phone="+15550200004", session_name="preview-defaults",
                        status=AccountStatus.ACTIVE, warmup_enabled=True,
                        warmup_profile="missing", last_reset=now, daily_limit=1),
                Account(phone="+15550200005", session_name="preview-disabled-profile",
                        status=AccountStatus.ACTIVE, warmup_enabled=True,
                        warmup_profile="disabled", last_reset=now, daily_limit=1),
                WarmupProfile(name="disabled", enabled=False, daily_action_limit=2),
            ])
            db.commit()

        app, client = _make_client(maker)
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(role="tenant_viewer")
        with client:
            missing = client.get("/business/warmup/preview?account_id=1").json()
            assert missing["profile"]["source"] == "config_defaults"
            assert missing["profile"]["effective_name"] is None
            assert missing["profile"]["target_count"] == 0
            assert missing["warmup_daily"]["limit"] == min(12, report_module.WARMUP_DAILY_ACTION_LIMIT)
            assert missing["blocking_reasons"] == ["work_hours"]
            assert missing["next_eligible_at_utc"] == "2026-09-25T15:00:00Z"
            assert missing["actions"][0]["available_now"] is False

            disabled = client.get("/business/warmup/preview?account_id=2").json()
            assert disabled["profile"]["enabled"] is False
            assert disabled["blocking_reasons"] == ["profile_disabled", "work_hours"]
            assert disabled["next_eligible_at_utc"] is None
    finally:
        engine.dispose()
