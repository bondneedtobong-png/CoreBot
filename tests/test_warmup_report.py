"""The warmup report counts stored events without exposing their raw details."""
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
from database.models import Account, Base, WarmupLog, WarmupProfile


def test_report_utc_bounds_aggregates_and_read_only_auth(monkeypatch):
    now = datetime(2026, 9, 25, 12, 0, 0)
    monkeypatch.setattr(report_module, "utcnow_naive", lambda: now)
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(engine, expire_on_commit=False)
    try:
        with maker() as db:
            db.add_all([
                WarmupProfile(
                    name="safe", enabled=True, daily_action_limit=12,
                    base_delay_sec=30, jitter_sec=5,
                    target_chats_text="@owned_channel\n@second_channel\n",
                ),
                Account(
                    phone="+15550110001", session_name="warmup-report-1",
                    list_label="First", warmup_enabled=True, warmup_profile="safe",
                ),
                Account(
                    phone="+15550110002", session_name="warmup-report-2",
                    list_label="Second", warmup_enabled=False, warmup_profile="missing",
                    warmup_pause_reason="floodwait:60",
                    warmup_paused_until=now + timedelta(minutes=1),
                ),
            ])
            db.flush()
            db.add_all([
                WarmupLog(account_id=1, action="read_dialogs", status="ok",
                          created_at=now - timedelta(days=7)),
                WarmupLog(account_id=1, action="set_reaction", status="skip",
                          details="no_targets", created_at=now - timedelta(days=1)),
                WarmupLog(account_id=1, action="pause_floodwait_set_reaction", status="skip",
                          details="seconds=60", created_at=now - timedelta(days=1)),
                WarmupLog(account_id=1, action="read_channels", status="error",
                          details="err=private-secret", created_at=now - timedelta(hours=1)),
                WarmupLog(account_id=2, action="pause_daily_limit", status="skip",
                          created_at=now - timedelta(minutes=30)),
                WarmupLog(account_id=2, action="skip_reaction", status="skip",
                          details="empty:@private_channel", created_at=now - timedelta(minutes=20)),
                WarmupLog(account_id=2, action="read_channels", status="skip",
                          details="delay=30.0s worker_unavailable",
                          created_at=now - timedelta(minutes=10)),
                WarmupLog(account_id=2, action="read_channels", status="ok",
                          created_at=now - timedelta(days=15)),
                WarmupLog(account_id=2, action="read_dialogs", status="ok",
                          created_at=now - timedelta(days=30)),
                WarmupLog(account_id=2, action="read_dialogs", status="ok",
                          created_at=now - timedelta(days=30, microseconds=1)),
                WarmupLog(account_id=1, action="read_dialogs", status="ok",
                          created_at=now - timedelta(days=7, microseconds=1)),
                WarmupLog(account_id=1, action="read_dialogs", status="ok",
                          created_at=now),
            ])
            db.commit()

        app = FastAPI()
        app.include_router(report_module.router)

        def test_db():
            with maker() as db:
                yield db

        app.dependency_overrides[get_bot_db] = test_db
        with TestClient(app) as client:
            assert client.get("/business/warmup/report").status_code == 401
            app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(role="tenant_viewer")

            response = client.get("/business/warmup/report?days=7")
            assert response.status_code == 200
            body = response.json()
            assert body["since_utc"] == "2026-09-18T12:00:00Z"
            assert body["until_utc"] == "2026-09-25T12:00:00Z"
            assert body["totals"] == {"total": 7, "ok": 1, "skip": 5, "fail": 1}
            assert body["by_status"] == {"ok": 1, "skip": 5, "fail": 1}
            assert body["by_action"]["set_reaction"] == {
                "total": 3, "ok": 0, "skip": 3, "fail": 0,
            }
            assert body["skip_fail_reasons"] == {
                "daily_limit": {"total": 1, "skip": 1, "fail": 0},
                "empty_target": {"total": 1, "skip": 1, "fail": 0},
                "flood_wait": {"total": 1, "skip": 1, "fail": 0},
                "no_targets": {"total": 1, "skip": 1, "fail": 0},
                "unexpected_error": {"total": 1, "skip": 0, "fail": 1},
                "worker_unavailable": {"total": 1, "skip": 1, "fail": 0},
            }
            assert body["accounts"][0]["totals"]["total"] == 4
            assert body["accounts"][1]["totals"]["total"] == 3
            assert body["accounts"][1]["effective_profile_name"] == "safe"
            assert body["accounts"][1]["pause_reason"] == "flood_wait"
            assert body["profiles"][0]["target_count"] == 2
            assert "private-secret" not in response.text
            assert "private_channel" not in response.text
            assert "owned_channel" not in response.text
            assert "details" not in response.text

            thirty = client.get("/business/warmup/report?days=30")
            assert thirty.status_code == 200
            assert thirty.json()["totals"] == {"total": 10, "ok": 4, "skip": 5, "fail": 1}
            one = client.get("/business/warmup/report?days=7&account_id=1")
            assert one.status_code == 200
            assert len(one.json()["accounts"]) == 1
            assert one.json()["totals"] == {"total": 4, "ok": 1, "skip": 2, "fail": 1}
            assert client.get("/business/warmup/report?days=14").status_code == 422
            assert client.get("/business/warmup/report?account_id=0").status_code == 422
            assert client.get("/business/warmup/report?account_id=999").status_code == 404
            assert client.post("/business/warmup/report").status_code == 405

        with maker() as db:
            assert db.scalar(select(func.count(WarmupLog.id))) == 12
    finally:
        engine.dispose()
