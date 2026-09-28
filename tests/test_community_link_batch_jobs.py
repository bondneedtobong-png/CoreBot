"""Durable community batch checks: due gate, checkpoints, cancellation and scope."""
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from database.models import Account, AccountStatus, Base, BotCommand
from utils.time import utcnow_naive
import services.community_link_batch_jobs as jobs
import workers.bot_command_consumer as consumer_module


def test_custom_utc_schedule_accepts_bounded_aware_time_only():
    now = datetime(2026, 9, 27, 10, 0)
    assert jobs._schedule_due_at(
        now, None, datetime(2026, 9, 27, 14, 30, tzinfo=timezone(timedelta(hours=4))),
    ) == datetime(2026, 9, 27, 10, 30)
    assert jobs._schedule_due_at(now, 600, None) == now + timedelta(minutes=10)
    for due in (
        datetime(2026, 9, 27, 10, 4, tzinfo=timezone.utc),
        datetime(2026, 10, 28, 10, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 27, 11, 0),
    ):
        with pytest.raises(ValueError):
            jobs._schedule_due_at(now, None, due)
    with pytest.raises(ValueError):
        jobs._schedule_due_at(now, 600, datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc))
    with pytest.raises(ValueError):
        jobs._schedule_due_at(now, None, None)


def test_batch_queue_durable_step_and_redaction(tmp_path, monkeypatch):
    db_path = tmp_path / "community-batch.db"
    sync_engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(sync_engine)
    sync_maker = sessionmaker(sync_engine, expire_on_commit=False)
    with sync_maker() as session:
        session.add(Account(phone="+15551119999", session_name="batch", status=AccountStatus.ACTIVE))
        session.commit()

    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        maker = async_sessionmaker(engine, expire_on_commit=False)

        @asynccontextmanager
        async def scope():
            async with maker() as session:
                yield session

        monkeypatch.setattr(jobs, "session_scope", scope)
        monkeypatch.setattr(consumer_module, "session_scope", scope)
        consumer = consumer_module.BotCommandConsumer()
        consumer._did_recover_community_batches = True
        consumer._did_recover_safety = True
        consumer._did_recover_health = True
        consumer._did_recover_mailings = True
        consumer._did_recover_engagement = True
        links = ["https://t.me/own_group", "https://t.me/private_group"]
        calls = []

        async def checker(account_id, link, actor_id=None):
            calls.append((account_id, link, actor_id))
            if link == links[0]:
                return {"status": "ok", "title": "Our group", "canonical_link": link}
            return {"status": "failed", "title": "Secret group", "canonical_link": link,
                    "reason": "unavailable_or_not_owned"}

        monkeypatch.setattr(jobs, "check_owned_community_link", checker)
        try:
            with pytest.raises(ValueError):
                await jobs.enqueue_owned_community_batch(1, 7, [links[0], links[0]], 0)
            with pytest.raises(ValueError):
                await jobs.enqueue_owned_community_batch(1, 7, ["http://t.me/bad_link"], 0)
            with pytest.raises(ValueError):
                await jobs.enqueue_owned_community_batch(99, 7, links, 0)

            delayed = await jobs.enqueue_owned_community_batch(1, 7, links, 3600)
            assert delayed["due_at"] is not None
            own_history = await jobs.list_owned_community_batches(7)
            assert len(own_history) == 1 and own_history[0]["id"] == delayed["id"]
            assert await jobs.list_owned_community_batches(8) == []
            exact_due = (utcnow_naive() + timedelta(days=2)).replace(tzinfo=timezone.utc)
            exact = await jobs.enqueue_owned_community_batch(
                1, 7, links, start_at_utc=exact_due,
            )
            with sync_maker() as session:
                assert session.get(BotCommand, exact["id"]).not_before == exact_due.replace(tzinfo=None)
            assert (await jobs.cancel_owned_community_batch(exact["id"], 7))["status"] == "cancelled"
            assert await consumer._tick() == 0
            assert calls == []
            with sync_maker() as session:
                row = session.get(BotCommand, delayed["id"])
                row.not_before = utcnow_naive() - timedelta(seconds=1)
                session.commit()

            assert await consumer._tick() == 1
            assert len(calls) == 1
            progress = await jobs.get_owned_community_batch(delayed["id"], 7)
            assert progress["status"] == "pending"
            assert (progress["checked"], progress["owned_count"], progress["other_count"]) == (1, 1, 0)
            assert progress["owned_titles"] == ["Our group"]
            assert await jobs.get_owned_community_batch(delayed["id"], 8) is None
            assert await jobs.cancel_owned_community_batch(delayed["id"], 8) is None

            assert await consumer._tick() == 1
            done = await jobs.get_owned_community_batch(delayed["id"], 7)
            assert done["status"] == "done"
            assert (done["checked"], done["other_count"]) == (2, 1)
            exposed = json.dumps(done)
            assert "private_group" not in exposed and "Secret group" not in exposed
            assert "own_group" not in exposed
            assert len(calls) == 2

            pending = await jobs.enqueue_owned_community_batch(1, 7, links, 3600)
            cancelled = await jobs.cancel_owned_community_batch(pending["id"], 7)
            assert cancelled["status"] == "cancelled"
            assert await consumer._tick() == 0

            processing = await jobs.enqueue_owned_community_batch(1, 7, links, 0)

            async def cancel_during_check(account_id, link, actor_id=None):
                snapshot = await jobs.cancel_owned_community_batch(processing["id"], 7)
                assert snapshot["status"] == "processing"
                return {"status": "failed", "title": "Hidden target", "canonical_link": link}

            monkeypatch.setattr(jobs, "check_owned_community_link", cancel_during_check)
            assert await consumer._tick() == 1
            stopped = await jobs.get_owned_community_batch(processing["id"], 7)
            assert stopped["status"] == "cancelled" and stopped["checked"] == 1
            assert "Hidden target" not in json.dumps(stopped)

            recovery = await jobs.enqueue_owned_community_batch(1, 7, links, 0)
            with sync_maker() as session:
                row = session.get(BotCommand, recovery["id"])
                args = json.loads(row.args_json)
                args.update(next_index=1, owned_titles=["Saved title"])
                row.args_json = json.dumps(args)
                row.status = "processing"
                session.commit()
            await jobs.recover_owned_community_batches()
            resumed = await jobs.get_owned_community_batch(recovery["id"], 7)
            assert resumed["status"] == "pending" and resumed["checked"] == 1
            assert resumed["owned_titles"] == ["Saved title"]

            malformed = await jobs.enqueue_owned_community_batch(1, 7, links, 0)
            with sync_maker() as session:
                row = session.get(BotCommand, malformed["id"])
                row.args_json = '{"links": ["https://t.me/secret_group"]}'
                session.commit()
            assert await consumer._tick() >= 1
            failed = await jobs.get_owned_community_batch(malformed["id"], 7)
            assert failed["status"] == "failed"
            assert "secret_group" not in json.dumps(failed)

            unavailable = await jobs.enqueue_owned_community_batch(1, 7, links, 0)

            async def broken_checker(*_args, **_kwargs):
                raise RuntimeError("secret proxy password")

            monkeypatch.setattr(jobs, "check_owned_community_link", broken_checker)
            assert await consumer._tick() >= 1
            unavailable_view = await jobs.get_owned_community_batch(unavailable["id"], 7)
            assert unavailable_view["status"] == "failed"
            assert unavailable_view["checked"] == 0
            assert "secret" not in json.dumps(unavailable_view)
        finally:
            await engine.dispose()

    asyncio.run(scenario())
