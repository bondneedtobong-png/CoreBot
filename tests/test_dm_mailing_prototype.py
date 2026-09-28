"""Bot launch shares the panel's frozen, consent-only command path."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from database.models import (
    Base, BotCommand, Client, ClientContactPermission, ClientStatus,
    Mailing, MailingRun, MailingRunRecipient,
)


def test_bot_queues_one_frozen_opt_in_run(tmp_path, monkeypatch):
    import bot.handlers.mailing as handler
    import workers.manager as manager_module

    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mailing.db'}")
        maker = async_sessionmaker(engine, expire_on_commit=False)

        @asynccontextmanager
        async def scope():
            async with maker() as session:
                try:
                    yield session
                except BaseException:
                    await session.rollback()
                    raise

        async def render(*_args):
            return None

        monkeypatch.setattr(handler, "session_scope", scope)
        monkeypatch.setattr(handler, "_render_mailing_screen", render)
        monkeypatch.setattr(handler, "is_authorized_user", lambda _user_id: True)
        monkeypatch.setattr(
            manager_module, "worker_manager",
            SimpleNamespace(_mailing_busy=False, is_running=False, current_mailing_id=None),
        )
        answers = []

        async def answer(text, **kwargs):
            answers.append((text, kwargs))

        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=123), answer=answer, data="mailing_stop_1",
        )
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with maker() as session:
                session.add_all([
                    Client(username="allowed", status=ClientStatus.NEW),
                    Client(username="not_allowed", status=ClientStatus.NEW),
                    Mailing(name="notice", message_text="hello"),
                ])
                await session.commit()
                session.add(ClientContactPermission(
                    client_id=1, state="opt_in", source="signup", actor="owner",
                ))
                await session.commit()
                mailing = await session.get(Mailing, 1)

            assert await handler._execute_mailing_start(callback, mailing) is True
            assert await handler._execute_mailing_start(callback, mailing) is False
            async with maker() as session:
                runs = list((await session.scalars(select(MailingRun))).all())
                commands = list((await session.scalars(select(BotCommand))).all())
                recipients = list((await session.scalars(
                    select(MailingRunRecipient.client_id)
                )).all())
                assert len(runs) == len(commands) == 1
                assert runs[0].status == "queued"
                assert runs[0].audience_count == 1
                assert recipients == [1]
                assert json.loads(commands[0].args_json) == {
                    "mailing_id": 1, "run_id": runs[0].id,
                }
                assert commands[0].status == "pending"
                assert answers[-1][1]["show_alert"] is True
            await handler.cb_mailing_stop(callback)
            async with maker() as session:
                assert (await session.get(MailingRun, runs[0].id)).status == "cancelled"
                assert (await session.get(BotCommand, commands[0].id)).status == "cancelled"
        finally:
            await engine.dispose()

    asyncio.run(scenario())
