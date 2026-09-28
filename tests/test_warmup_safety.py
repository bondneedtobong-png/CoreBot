"""Warmup must respect the shared account stop and outbound budget."""

import asyncio
from contextlib import asynccontextmanager

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from database.models import Account, AccountSafetyState, AccountStatus, Base, WarmupLog, WarmupProfile
from services.account_safety import pause_account
from workers import warmup


def test_warmup_skips_review_accounts_and_denied_reactions(tmp_path, monkeypatch):
    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'warmup-safety.db'}")
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with maker() as session:
                session.add_all([
                    WarmupProfile(name="safe", allowed_actions="set_reaction", work_start_hour=9, work_end_hour=18),
                    Account(phone="+15550100001", session_name="paused-warmup",
                            status=AccountStatus.ACTIVE, warmup_enabled=True, daily_limit=10),
                    Account(phone="+15550100002", session_name="budget-warmup",
                            status=AccountStatus.ACTIVE, warmup_enabled=True, daily_limit=0),
                ])
                await session.commit()
                ids = list((await session.execute(select(Account.id).order_by(Account.id))).scalars())
                session.add(AccountSafetyState(account_id=ids[0], state="review_required", reason_code="manual_pause"))
                await session.commit()

            @asynccontextmanager
            async def scope():
                async with maker() as session:
                    yield session

            monkeypatch.setattr(warmup, "session_scope", scope)
            monkeypatch.setattr(warmup, "is_off_hours", lambda *_args: True)
            monkeypatch.setattr(warmup, "choose_warmup_action", lambda: "set_reaction")
            runner = warmup.WarmupRunner()
            connected = []

            async def ensure(account):
                connected.append(account.id)
                return type("FakeWorker", (), {"_send_lock": asyncio.Lock()})()

            async def action(*_args):
                raise AssertionError("reaction should not be sent")

            monkeypatch.setattr(runner, "_ensure_worker_connected", ensure)
            monkeypatch.setattr(runner, "_parse_targets", lambda _raw: ["@owned_test"])
            monkeypatch.setattr(runner, "_do_action", action)
            await runner._tick()
            assert connected == [ids[1]]
            async with maker() as session:
                logs = (await session.execute(select(WarmupLog))).scalars().all()
                assert [(row.account_id, row.action, row.details) for row in logs] == [
                    (ids[1], "skip_reaction", "daily_limit"),
                ]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_warmup_reaction_shares_send_lock_and_does_not_reserve_empty_target(tmp_path, monkeypatch):
    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'reaction-lock.db'}")
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with maker() as session:
                session.add(Account(
                    phone="+15550100003", session_name="reaction-lock",
                    status=AccountStatus.ACTIVE, daily_limit=1,
                ))
                await session.commit()

            runner = warmup.WarmupRunner()
            worker = type("FakeWorker", (), {"_send_lock": asyncio.Lock()})()
            sent = []

            async def action(_worker, _action, _targets, *, target_override=None):
                sent.append(target_override)
                return "ok", "reaction"

            monkeypatch.setattr(runner, "_do_action", action)
            async with maker() as session:
                assert await runner._perform_account_action(
                    worker, 1, "set_reaction", [], session,
                ) == ("skip", "no_targets")

            async def attempt():
                async with maker() as session:
                    return await runner._perform_account_action(
                        worker, 1, "set_reaction", ["@owned_test"], session,
                    )

            results = await asyncio.gather(attempt(), attempt())
            assert sorted(results) == [("ok", "reaction"), ("skip", "daily_limit")]
            assert sent == ["@owned_test"]
            async with maker() as session:
                state = await session.get(AccountSafetyState, 1)
                assert state.attempts_today == 1
                await pause_account(session, 1, reason_code="manual_pause", source="operator")
            async with maker() as session:
                assert await runner._perform_account_action(
                    worker, 1, "read_dialogs", [], session,
                ) == ("skip", "manual_pause")
        finally:
            await engine.dispose()

    asyncio.run(scenario())
