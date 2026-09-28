"""Telegram Premium parser filtering stays transient and consistent by source."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from telethon.tl.types import User

from workers.parser import collect_users


class _Session:
    async def commit(self):
        return None


def _user(
    premium: bool | None, scam: bool | None, fake: bool | None
) -> User:
    return User(
        id=123, first_name="Test", username="test_user",
        premium=premium, scam=scam, fake=fake,
    )


@pytest.mark.parametrize("source", ["member", "active", "commenter"])
@pytest.mark.parametrize(
    "user_flags, user_filter, accepted, reason",
    [
        ((True, None, None), {"require_premium": True}, True, None),
        ((None, None, None), {"require_premium": True}, False, "not_premium"),
        # Scam/fake exclusion is opt-in and independent from Premium filtering.
        ((True, True, None), {"require_premium": True}, True, None),
        ((True, None, None), {"exclude_scam_fake": True}, True, None),
        ((True, True, None), {"exclude_scam_fake": True}, False, "scam"),
        ((True, None, True), {"exclude_scam_fake": True}, False, "fake"),
        (
            (True, True, None),
            {"require_premium": True, "exclude_scam_fake": True},
            False,
            "scam",
        ),
    ],
)
def test_require_premium_applies_to_each_user_source(
    monkeypatch, source: str, user_flags: tuple, user_filter: dict,
    accepted: bool, reason: str | None,
):
    stored: list[dict] = []
    reasons: list[str] = []
    edges: list[dict] = []

    async def upsert(_session, **kwargs):
        stored.append(kwargs)
        return SimpleNamespace(id=1)

    async def edge(_session, **kwargs):
        edges.append(kwargs)

    async def bump_reason(_session, _task_id, reason):
        reasons.append(reason)

    async def bump_counters(*_args, **_kwargs):
        return None

    monkeypatch.setattr(collect_users.storage, "upsert_user", upsert)
    monkeypatch.setattr(collect_users.storage, "add_user_source_edge", edge)
    monkeypatch.setattr(collect_users.storage, "bump_filter_reason", bump_reason)
    monkeypatch.setattr(collect_users.storage, "bump_task_counters", bump_counters)

    user = _user(*user_flags)

    class Client:
        async def iter_participants(self, *_args, **_kwargs):
            yield user

        async def iter_messages(self, *_args, reply_to=None, **_kwargs):
            if source == "commenter":
                if reply_to is None:
                    yield SimpleNamespace(id=77)
                else:
                    yield SimpleNamespace(
                        id=78,
                        sender_id=user.id,
                        raw_text="hello",
                        date=None,
                        get_sender=lambda: _return(user),
                    )
            elif source == "active":
                yield SimpleNamespace(
                    id=79,
                    sender_id=user.id,
                    raw_text="hello",
                    date=None,
                    get_sender=lambda: _return(user),
                )

    async def log(*_args, **_kwargs):
        return None

    async def exercise():
        session = _Session()
        if source == "member":
            count = await collect_users._collect_participants(
                Client(), object(), task_id=1, account_id=1, source_entity_id=5,
                source_entity_kind="group", source_kind="member", session=session,
                user_flt=user_filter, log=log, recent_only=False,
                limit_users=10,
            )
        elif source == "active":
            count = await collect_users._collect_from_messages(
                Client(), object(), task_id=1, account_id=1, source_entity_id=5,
                source_entity_kind="group", source_kind="active", session=session,
                user_flt=user_filter, log=log,
            )
        else:
            count = await collect_users._collect_channel_commenters_from_posts(
                Client(), object(), task_id=1, account_id=1, source_entity_id=5,
                session=session, user_flt=user_filter, log=log,
                posts_limit=1, comments_per_post=1,
            )
        assert count == int(accepted)

    async def _return(value):
        return value

    asyncio.run(exercise())
    assert len(stored) == int(accepted)
    assert len(edges) == int(accepted)
    if accepted:
        assert not {"premium", "scam", "fake"} & stored[0].keys()
        assert reasons == []
    else:
        assert reasons == [reason]
