"""Регрессия модуля парсинга: querygen, filters, depth_expand, FloodWait helper."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import asyncio

import pytest
from telethon.errors import FloodWaitError

from workers.parser import querygen, filters, depth_expand
from workers.parser import floodwait as floodwait_mod
from workers.parser.floodwait import run_with_floodwait
from workers.parser.collect_users import _entity_reference
from workers.parser.account_pool import AccountSnap, RotatingClients


def test_querygen_build_channel_queries():
    qs = querygen.build_channel_or_group_queries({"keyword": "crypto"})
    assert any("crypto" == q.strip() for q in qs)
    assert len(qs) >= 1


def test_querygen_manual_txt():
    txt = "@foo\nhttps://t.me/bar\n#comment\n"
    assert querygen.manual_queries_from_txt(txt) == ["foo", "bar"]


def test_private_chat_numeric_reference():
    assert _entity_reference(" -1003993284688 ") == -1003993284688
    assert _entity_reference("@example") == "@example"


def test_parser_reuses_bot_client_without_disconnecting(monkeypatch):
    from workers.manager import worker_manager

    class LiveClient:
        def is_connected(self):
            return True

        async def disconnect(self):
            raise AssertionError("borrowed bot client must stay connected")

    client = LiveClient()
    monkeypatch.setattr(
        worker_manager, "workers", {8: SimpleNamespace(client=client, is_connected=True)}
    )
    pool = RotatingClients([AccountSnap(8, "unused", None)], runtime_workers=True)

    async def exercise():
        assert await pool.next_client() == (8, client)
        await pool.disconnect_all()

    asyncio.run(exercise())


def test_querygen_merge_unique():
    assert querygen.merge_query_lists(["a", "b"], ["b", "c"]) == ["a", "b", "c"]


def test_filters_channel_subscribers():
    row = {"subscribers": 50, "is_active_7d": True, "lang": "ru"}
    ok, _ = filters.channel_passes_filters(row, {"subscribers_min": 100})
    assert ok is False
    ok2, _ = filters.channel_passes_filters(row, {"subscribers_min": 10, "lang": "ru"})
    assert ok2 is True


@pytest.mark.parametrize(
    ("members_count", "flt", "expected"),
    [
        (10, {"members_min": 10}, (True, "")),
        (10, {"members_max": 10}, (True, "")),
        (9, {"members_min": "10"}, (False, "members_min")),
        (11, {"members_max": 10}, (False, "members_max")),
        (None, {"members_min": 10, "members_max": 20}, (True, "")),
    ],
)
def test_filters_group_member_count_bounds(members_count, flt, expected):
    assert filters.group_passes_filters({"members_count": members_count}, flt) == expected


def test_filters_is_active_7d():
    now = datetime.now(timezone.utc)
    row = {"is_active_7d": True, "subscribers": 1}
    ok, _ = filters.channel_passes_filters(row, {"is_active_7d": True})
    assert ok is True
    old = now - timedelta(days=10)
    row2 = {"is_active_7d": False, "last_post_at": old, "subscribers": 1}
    ok3, _ = filters.channel_passes_filters(row2, {"is_active_7d": True})
    assert ok3 is False


def test_depth_expand_bounded():
    seeds = [{"title": "Crypto News Daily", "username": "cryptonews"}]
    extra = depth_expand.expand_queries_from_seeds(seeds, "crypto", max_extra_queries=5)
    assert isinstance(extra, list)
    assert len(extra) <= 5


def test_floodwait_retries(monkeypatch):
    n = {"c": 0}

    async def op():
        n["c"] += 1
        if n["c"] < 3:
            raise FloodWaitError(None)
        return "ok"

    async def fake_sleep(_x):
        return None

    monkeypatch.setattr(floodwait_mod.asyncio, "sleep", fake_sleep)

    out = asyncio.run(run_with_floodwait(op, max_retries=5))
    assert out == "ok"
    assert n["c"] == 3


def test_parsing_task_schema_kind():
    from control_plane.business.schemas import ParsingTaskCreate

    t = ParsingTaskCreate(
        kind="channels",
        account_ids=[1, 2],
        depth=2,
        mode="max_coverage",
        params={"keyword": "test"},
    )
    assert t.kind == "channels"
    assert t.depth == 2


def test_parsing_task_schema_rejects_bad_kind():
    from pydantic import ValidationError

    from control_plane.business.schemas import ParsingTaskCreate

    with pytest.raises(ValidationError):
        ParsingTaskCreate(kind="other", account_ids=[1], params={})
