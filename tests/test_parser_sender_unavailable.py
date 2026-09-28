"""Visible author events remain accounted for when Telethon cannot resolve a sender."""

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from database.models import Base, ParsingFilterReasonCount, ParsingTask
from workers.parser.collect_users import (
    _collect_channel_commenters_from_posts,
    _collect_from_messages,
)


class UnavailableSenderMessage:
    def __init__(self, message_id, sender_id, *, raises=False):
        self.id = message_id
        self.sender_id = sender_id
        self.raises = raises
        self.raw_text = "visible message"

    async def get_sender(self):
        if self.raises:
            raise LookupError("sender is no longer available")
        return None


class FakeClient:
    def __init__(self, messages=(), comments=()):
        self.messages = messages
        self.comments = comments

    async def iter_messages(self, _entity, *, limit, reply_to=None):
        items = self.comments if reply_to is not None else self.messages
        for message in items[:limit]:
            yield message


async def silent_log(*_args, **_kwargs):
    return None


def test_unavailable_message_and_comment_senders_are_counted(tmp_path):
    db_path = tmp_path / "sender-unavailable.db"

    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with maker() as session:
                task = ParsingTask(kind="users", status="running")
                session.add(task)
                await session.commit()

                message_client = FakeClient(messages=[
                    UnavailableSenderMessage(10, 501, raises=True),
                ])
                assert await _collect_from_messages(
                    message_client, object(), task_id=task.id, account_id=1,
                    source_entity_id=700, source_entity_kind="group",
                    source_kind="active", session=session, user_flt={}, log=silent_log,
                ) == 0

                comment_client = FakeClient(
                    messages=[type("Post", (), {"id": 20})()],
                    comments=[UnavailableSenderMessage(21, 502)],
                )
                assert await _collect_channel_commenters_from_posts(
                    comment_client, object(), task_id=task.id, account_id=1,
                    source_entity_id=800, session=session, user_flt={}, log=silent_log,
                ) == 0

                reason = await session.get(
                    ParsingFilterReasonCount, (task.id, "sender_unavailable"),
                )
                await session.refresh(task)
                assert reason.count == 2
                assert task.filtered_count == 2
                assert task.found_count == 0
        finally:
            await engine.dispose()

    asyncio.run(scenario())
