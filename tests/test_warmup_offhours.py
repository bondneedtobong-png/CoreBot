"""Off-hours warmup respects a persisted clock and an explicit action allowlist."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from database.models import Account, AccountStatus, Base, WarmupLog, WarmupProfile
from database import repositories
from database.repository import Database
from services.warmup_schedule import MIN_INTERVAL_SECONDS, is_off_hours, next_off_hours, parse_read_targets
from workers import warmup


def test_work_window_and_cross_midnight():
    assert not is_off_hours(datetime(2026, 9, 27, 9), "Europe/Samara", 9, 18)
    assert is_off_hours(datetime(2026, 9, 27, 15), "Europe/Samara", 9, 18)
    assert next_off_hours(datetime(2026, 9, 27, 9), "Europe/Samara", 9, 18) == datetime(2026, 9, 27, 14)
    assert not is_off_hours(datetime(2026, 9, 27, 20), "UTC", 18, 3)
    assert not is_off_hours(datetime(2026, 9, 28, 2), "UTC", 18, 3)
    assert is_off_hours(datetime(2026, 9, 28, 3), "UTC", 18, 3)
    assert parse_read_targets("@owned_chat\nhttps://t.me/public_chat/99\nhttps://t.me/+invite\nhttps://other.test/x") == [
        "@owned_chat", "@public_chat",
    ]


def test_no_action_in_work_hours_or_before_persisted_interval(tmp_path, monkeypatch):
    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'warmup.db'}")
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            now = datetime(2026, 9, 27, 9)  # 13:00 in Samara: work hours
            async with maker() as session:
                session.add(WarmupProfile(name="safe", time_zone="Europe/Samara",
                    work_start_hour=9, work_end_hour=18, allowed_actions="read_dialogs",
                    base_delay_sec=1800, jitter_sec=0, daily_action_limit=4))
                session.add(Account(phone="+15550100011", session_name="quiet",
                    status=AccountStatus.ACTIVE, warmup_enabled=True, warmup_profile="safe"))
                await session.commit()

            @asynccontextmanager
            async def scope():
                async with maker() as session:
                    yield session

            monkeypatch.setattr(warmup, "session_scope", scope)
            monkeypatch.setattr(warmup, "utcnow_naive", lambda: now)
            monkeypatch.setattr(repositories, "utcnow_naive", lambda: now)
            runner = warmup.WarmupRunner()
            calls = []

            async def ensure(_account):
                calls.append("connect")
                return object()

            monkeypatch.setattr(runner, "_ensure_worker_connected", ensure)
            await runner._tick()
            assert calls == []
            async with maker() as session:
                account = await session.get(Account, 1)
                assert account.warmup_next_run_at == datetime(2026, 9, 27, 14)
                account.warmup_next_run_at = None
                account.warmup_last_action_at = datetime(2026, 9, 27, 14)
                await session.commit()
            now = datetime(2026, 9, 27, 14, 1)  # quiet, but only one minute since action
            await runner._tick()
            assert calls == []
            async with maker() as session:
                account = await session.get(Account, 1)
                assert account.warmup_next_run_at == datetime(2026, 9, 27, 14) + timedelta(seconds=MIN_INTERVAL_SECONDS)
                assert (await session.execute(select(WarmupLog))).scalars().all() == []
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_quiet_window_runs_once_and_persists_next_due(tmp_path, monkeypatch):
    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'quiet.db'}")
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with maker() as session:
                session.add(WarmupProfile(name="safe", time_zone="UTC", work_start_hour=9,
                    work_end_hour=18, allowed_actions="read_dialogs", base_delay_sec=3600,
                    jitter_sec=0, daily_action_limit=4))
                session.add(Account(phone="+15550100012", session_name="quiet-run",
                    status=AccountStatus.ACTIVE, warmup_enabled=True, warmup_profile="safe"))
                await session.commit()

            @asynccontextmanager
            async def scope():
                async with maker() as session:
                    yield session

            now = datetime(2026, 9, 27, 20)
            monkeypatch.setattr(warmup, "session_scope", scope)
            monkeypatch.setattr(warmup, "utcnow_naive", lambda: now)
            monkeypatch.setattr(repositories, "utcnow_naive", lambda: now)
            monkeypatch.setattr(warmup, "choose_warmup_action", lambda: "read_dialogs")
            runner = warmup.WarmupRunner()
            calls = []

            async def ensure(_account):
                return object()

            async def action(_worker, _account_id, selected, _targets, _session):
                calls.append(selected)
                return "ok", "dialogs_refreshed"

            monkeypatch.setattr(runner, "_ensure_worker_connected", ensure)
            monkeypatch.setattr(runner, "_perform_account_action", action)
            await runner._tick()
            await runner._tick()
            assert calls == ["read_dialogs"]
            async with maker() as session:
                account = await session.get(Account, 1)
                assert account.warmup_actions_today == 1
                assert account.warmup_next_run_at >= now + timedelta(seconds=MIN_INTERVAL_SECONDS)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_additive_warmup_profile_migration_is_idempotent(tmp_path):
    async def scenario():
        url = f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}"
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE warmup_profiles (id INTEGER PRIMARY KEY, name VARCHAR(50) UNIQUE NOT NULL, base_delay_sec FLOAT, jitter_sec FLOAT, daily_action_limit INTEGER, target_chats_text TEXT, enabled BOOLEAN, created_at DATETIME, updated_at DATETIME)"))
            await conn.execute(text("INSERT INTO warmup_profiles(id, name, base_delay_sec, jitter_sec, daily_action_limit, enabled) VALUES (1, 'safe', 45, 25, 40, 1)"))
        await engine.dispose()
        db = Database(url)
        await db.connect()
        await db._run_migrations()
        async with db.engine.connect() as conn:
            cols = {row[1] for row in (await conn.execute(text("PRAGMA table_info(warmup_profiles)"))).all()}
            assert {"time_zone", "work_start_hour", "work_end_hour", "allowed_actions"} <= cols
            row = (await conn.execute(text("SELECT time_zone, work_start_hour, work_end_hour, allowed_actions FROM warmup_profiles WHERE id=1"))).one()
            assert row == ("Europe/Moscow", 9, 18, "read_dialogs,read_channels")
            preset = (await conn.execute(text("SELECT base_delay_sec, jitter_sec, daily_action_limit FROM warmup_profiles WHERE id=1"))).one()
            assert preset == (3600.0, 900.0, 4)
        await db.engine.dispose()

    asyncio.run(scenario())
