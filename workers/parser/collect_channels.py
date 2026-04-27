"""Сценарий парсинга каналов (search + метаданные + depth=2)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from telethon import TelegramClient
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.contacts import SearchRequest
from telethon.tl.types import Channel

from database.models import ParsingTask
from workers.parser import depth_expand, filters, floodwait, querygen, storage
from workers.parser.account_pool import RotatingClients

LogFn = Callable[..., Awaitable[None]]


def _utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _is_broadcast_channel(ch: Channel) -> bool:
    return bool(getattr(ch, "broadcast", False)) and not bool(getattr(ch, "megagroup", False))


async def _enrich_channel(
    client: TelegramClient,
    ch: Channel,
) -> dict[str, Any]:
    """Базовые поля + попытка full channel для подписчиков / discussion."""
    tid = int(ch.id)
    username = getattr(ch, "username", None) or None
    title = getattr(ch, "title", None) or None
    is_public = bool(username)
    subscribers = getattr(ch, "participants_count", None)
    has_discussion: Optional[bool] = None
    last_post_at: Optional[datetime] = None
    is_active_7d: Optional[bool] = None

    try:
        inp = await client.get_input_entity(ch)
        full = await floodwait.run_with_floodwait(lambda: client(GetFullChannelRequest(channel=inp)))
        fc = full.full_chat
        subscribers = getattr(fc, "participants_count", subscribers)
        has_discussion = bool(getattr(fc, "linked_chat_id", None))
    except Exception:
        pass

    try:
        async for m in client.iter_messages(ch, limit=5):
            if m and m.date:
                last_post_at = m.date
                break
    except Exception:
        pass

    lp = _utc(last_post_at)
    if lp is not None:
        is_active_7d = lp >= datetime.now(timezone.utc) - timedelta(days=7)

    text_blob = " ".join(x for x in [title or "", username or ""] if x)
    lang = filters.detect_lang_approx(text_blob)

    return {
        "telegram_id": tid,
        "username": username,
        "title": title,
        "subscribers": subscribers,
        "is_public": is_public,
        "has_discussion": has_discussion,
        "lang": lang,
        "last_post_at": last_post_at,
        "is_active_7d": is_active_7d,
    }


async def run_channel_task(
    session,
    task: ParsingTask,
    pool: RotatingClients,
    log: LogFn,
) -> None:
    params = task.params_json if isinstance(task.params_json, dict) else {}
    flt = params.get("filters") or {}
    if not isinstance(flt, dict):
        flt = {}
    max_entities = int(params.get("max_entities_per_task") or 500)
    max_depth2 = int(params.get("max_nodes_depth2") or 30)

    queries = querygen.build_channel_or_group_queries(params)
    queries.extend(querygen.manual_queries_from_txt(str(params.get("manual_usernames_text") or "")))
    queries = querygen.merge_query_lists(queries)
    if not queries:
        await log(None, "error", "empty_queries", "Укажите keyword в params или manual_usernames_text")
        raise ValueError("empty_queries: keyword или manual_usernames_text")

    depth = int(task.depth or 1)
    seeds: list[dict[str, Any]] = []
    total_seen: set[int] = set()

    async def search_query(q: str, account_id: int, client: TelegramClient) -> None:
        nonlocal seeds
        await log(account_id, "info", "search", f"Query: {q}", {"query": q})
        result = await floodwait.run_with_floodwait(
            lambda: client(SearchRequest(q=q, limit=25))
        )
        for chat in result.chats:
            if not isinstance(chat, Channel):
                continue
            if not _is_broadcast_channel(chat):
                continue
            tid = int(chat.id)
            if tid in total_seen:
                continue
            total_seen.add(tid)
            if len(total_seen) > max_entities:
                return
            row = await _enrich_channel(client, chat)
            ok, reason = filters.channel_passes_filters(row, flt)
            if not ok:
                await storage.bump_task_counters(
                    session,
                    task.id,
                    filtered_delta=1,
                    current_query=q,
                    current_account_id=account_id,
                    current_stage="filter",
                )
                await session.commit()
                continue
            action = await storage.upsert_channel(session, source_task_id=task.id, **row)
            await storage.bump_task_counters(
                session,
                task.id,
                found_delta=1,
                current_query=q,
                current_account_id=account_id,
                current_stage="collect",
            )
            await session.commit()
            seeds.append({"title": row.get("title"), "username": row.get("username")})
            await log(
                account_id,
                "info",
                "channel_upsert",
                f"{action} ch {tid} @{row.get('username')}",
                {"telegram_id": tid},
            )

    nq = len(queries) or 1
    for i, q in enumerate(queries):
        aid, client = await pool.next_client()
        prog = min(95, int(30 + (i + 1) / nq * 40))
        await storage.bump_task_counters(
            session,
            task.id,
            progress_percent=prog,
            current_stage="depth1",
            current_account_id=aid,
            current_query=q,
        )
        await session.commit()
        t2 = await session.get(ParsingTask, task.id)
        if t2 and t2.status == "cancelled":
            return
        try:
            await search_query(q, aid, client)
        except Exception as e:
            await storage.bump_task_counters(session, task.id, error_delta=1)
            await session.commit()
            await log(aid, "error", "search_failed", str(e)[:500], {"query": q})

    if depth >= 2:
        extra = depth_expand.expand_queries_from_seeds(
            seeds[:max_depth2],
            str(params.get("keyword") or ""),
            max_extra_queries=max_depth2,
        )
        extra = [x for x in extra if x not in queries]
        for j, q in enumerate(extra):
            aid, client = await pool.next_client()
            prog = min(98, 70 + int((j + 1) / max(len(extra), 1) * 25))
            await storage.bump_task_counters(
                session,
                task.id,
                progress_percent=prog,
                current_stage="depth2",
                current_account_id=aid,
                current_query=q,
            )
            await session.commit()
            t2 = await session.get(ParsingTask, task.id)
            if t2 and t2.status == "cancelled":
                return
            try:
                await search_query(q, aid, client)
            except Exception as e:
                await storage.bump_task_counters(session, task.id, error_delta=1)
                await session.commit()
                await log(aid, "warn", "depth2_failed", str(e)[:400], {"query": q})
