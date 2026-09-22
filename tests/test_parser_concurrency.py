from __future__ import annotations

import asyncio

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from database.models import Base, ParsedChannel, ParsingTask


def test_only_one_session_can_claim_pending_parser_task(tmp_path):
    from workers.parser.task_runner import claim_pending_task

    db_path = (tmp_path / "claim.db").as_posix()
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with Session() as setup:
            row = ParsingTask(kind="channels", status="pending")
            setup.add(row)
            await setup.commit()
            task_id = int(row.id)

        async with Session() as first, Session() as second:
            first_won = await claim_pending_task(first, task_id)
            await first.commit()
            second_won = await claim_pending_task(second, task_id)
            await second.commit()

        assert first_won is True
        assert second_won is False

    asyncio.run(scenario())
    asyncio.run(engine.dispose())


def test_channel_upsert_survives_unique_key_race(tmp_path):
    from workers.parser.storage import upsert_channel

    db_path = (tmp_path / "upsert.db").as_posix()
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        connect_args={"timeout": 2},
    )
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with Session() as setup:
            task = ParsingTask(kind="channels", status="running")
            setup.add(task)
            await setup.commit()
            task_id = int(task.id)

        async with Session() as first, Session() as second:
            await upsert_channel(
                first,
                telegram_id=777,
                source_task_id=task_id,
                title="first title",
            )

            competing = asyncio.create_task(
                upsert_channel(
                    second,
                    telegram_id=777,
                    source_task_id=task_id,
                    title="second title",
                )
            )
            await asyncio.sleep(0.05)
            await first.commit()
            await asyncio.wait_for(competing, timeout=2)
            await second.commit()

        async with Session() as verify:
            count = await verify.scalar(select(func.count(ParsedChannel.id)))
            row = await verify.scalar(
                select(ParsedChannel).where(ParsedChannel.telegram_id == 777)
            )
            assert count == 1
            assert row is not None
            assert row.title == "second title"

    asyncio.run(scenario())
    asyncio.run(engine.dispose())
