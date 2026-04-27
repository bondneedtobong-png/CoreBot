"""Сценарий парсинга групп / супергрупп (search + метаданные + depth=1/2/3)."""
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


def _is_megagroup(ch: Channel) -> bool:
    return bool(getattr(ch, "megagroup", False)) and not bool(getattr(ch, "broadcast", False))


async def _enrich_group(client: TelegramClient, ch: Channel) -> dict[str, Any]:
    tid = int(ch.id)
    username = getattr(ch, "username", None) or None
    title = getattr(ch, "title", None) or None
    group_type = "public" if username else "private"
    members = getattr(ch, "participants_count", None)
    last_post_at: Optional[datetime] = None
    is_active_7d: Optional[bool] = None

    try:
        inp = await client.get_input_entity(ch)
        full = await floodwait.run_with_floodwait(lambda: client(GetFullChannelRequest(channel=inp)))
        fc = full.full_chat
        members = getattr(fc, "participants_count", members)
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
        "members_count": members,
        "group_type": group_type,
        "lang": lang,
        "is_active_7d": is_active_7d,
    }


async def run_group_task(
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
    max_depth3 = int(params.get("max_nodes_depth3") or 20)
    expanded_search = bool(params.get("expanded_search", True))

    queries = querygen.build_channel_or_group_queries(params)
    queries.extend(querygen.manual_queries_from_txt(str(params.get("manual_usernames_text") or "")))
    queries = querygen.merge_query_lists(queries)
    if not queries:
        await log(None, "error", "empty_queries", "Укажите keyword в params или manual_usernames_text")
        raise ValueError("empty_queries: keyword или manual_usernames_text")

    depth = max(1, min(3, int(task.depth or 1)))
    seeds: list[dict[str, Any]] = []
    total_seen: set[int] = set()

    async def search_query(q: str, account_id: int, client: TelegramClient) -> None:
        nonlocal seeds
        await log(account_id, "info", "search", f"Query: {q}", {"query": q})
        result = await floodwait.run_with_floodwait(
            lambda: client(SearchRequest(q=q, limit=100)),
            on_flood_seconds=lambda sec: log(
                account_id,
                "warn",
                "floodwait",
                f"FloodWait {sec}s on query={q}",
                {"query": q, "seconds": sec},
            ),
        )
        for chat in result.chats:
            if not isinstance(chat, Channel):
                continue
            if not _is_megagroup(chat):
                continue
            tid = int(chat.id)
            if tid in total_seen:
                continue
            total_seen.add(tid)
            if len(total_seen) > max_entities:
                return
            row = await _enrich_group(client, chat)
            ok, reason = filters.group_passes_filters(row, flt)
            if not ok:
                await log(account_id, "info", "filtered", f"{tid} reason={reason}", {"query": q})
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
            action = await storage.upsert_group(session, source_task_id=task.id, **row)
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
                "group_upsert",
                f"{action} grp {tid} @{row.get('username')}",
                {"telegram_id": tid},
            )

    nq = len(queries) or 1
    for i, q in enumerate(queries):
        aid, client = await pool.next_client()
        prog = min(92, int(8 + (i + 1) / nq * 52))
        await storage.bump_task_counters(
            session,
            task.id,
            progress_percent=prog,
            current_stage="search",
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

    if depth >= 2 and expanded_search:
        extra = depth_expand.expand_queries_from_seeds(
            seeds[:max_depth2],
            " ".join(params.get("keywords") or [str(params.get("keyword") or "")]),
            max_extra_queries=max_depth2,
        )
        extra = [x for x in extra if x not in queries]
        for j, q in enumerate(extra):
            aid, client = await pool.next_client()
            prog = min(97, 60 + int((j + 1) / max(len(extra), 1) * 24))
            await storage.bump_task_counters(
                session,
                task.id,
                progress_percent=prog,
                current_stage="search",
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

    if depth >= 3 and expanded_search:
        extra2 = depth_expand.expand_queries_from_seeds(
            seeds[-max_depth3:],
            " ".join(params.get("keywords") or [str(params.get("keyword") or "")]),
            max_extra_queries=max_depth3,
        )
        extra2 = [x for x in extra2 if x not in queries]
        for k, q in enumerate(extra2):
            aid, client = await pool.next_client()
            prog = min(99, 84 + int((k + 1) / max(len(extra2), 1) * 14))
            await storage.bump_task_counters(
                session,
                task.id,
                progress_percent=prog,
                current_stage="search",
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
                await log(aid, "warn", "depth3_failed", str(e)[:400], {"query": q})
