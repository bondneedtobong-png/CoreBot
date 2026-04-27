"""Сценарий сбора пользователей из групп/каналов (members / active / commenters)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import select
from telethon import TelegramClient
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.types import (
    Channel,
    User,
    ChannelParticipantsRecent,
    UserStatusLastMonth,
    UserStatusLastWeek,
    UserStatusOffline,
    UserStatusOnline,
    UserStatusRecently,
)

from database.models import ParsingTask
from workers.parser import filters, querygen, storage
from workers.parser.account_pool import RotatingClients

LogFn = Callable[..., Awaitable[None]]


def _display_name(u: User) -> str:
    fn = (getattr(u, "first_name", None) or "").strip()
    ln = (getattr(u, "last_name", None) or "").strip()
    return (fn + " " + ln).strip() or (u.username or str(u.id))


def _extract_last_seen_at(u: User) -> Optional[datetime]:
    st = getattr(u, "status", None)
    now = datetime.now(timezone.utc)
    if isinstance(st, UserStatusOnline):
        return now
    if isinstance(st, UserStatusOffline):
        ws = getattr(st, "was_online", None)
        if isinstance(ws, datetime):
            return ws if ws.tzinfo else ws.replace(tzinfo=timezone.utc)
    if isinstance(st, UserStatusRecently):
        return now - timedelta(days=1)
    if isinstance(st, UserStatusLastWeek):
        return now - timedelta(days=5)
    if isinstance(st, UserStatusLastMonth):
        return now - timedelta(days=20)
    return None


def _build_user_row(u: User) -> dict[str, Any]:
    un = u.username
    dn = _display_name(u)
    has_photo = bool(getattr(u, "photo", None))
    lang = filters.detect_lang_approx(dn + " " + (un or ""))
    last_seen_at = _extract_last_seen_at(u)
    susp = filters.user_looks_suspicious(
        username=un,
        telegram_id=int(u.id),
        has_avatar=has_photo,
    )
    return {
        "telegram_id": int(u.id),
        "username": un,
        "display_name": dn,
        "has_avatar": has_photo,
        "last_seen_at": last_seen_at,
        "lang_guess": lang,
        "is_deleted": bool(getattr(u, "deleted", False)),
        "is_suspicious": susp,
    }


async def _collect_from_messages(
    client: TelegramClient,
    entity: Any,
    *,
    task_id: int,
    account_id: int,
    source_entity_id: int,
    source_entity_kind: str,
    source_kind: str,
    session,
    user_flt: dict[str, Any],
    log: LogFn,
    limit_messages: int = 40,
) -> None:
    seen: set[int] = set()
    async for m in client.iter_messages(entity, limit=limit_messages):
        if not m or not m.sender_id:
            continue
        uid = int(m.sender_id)
        if uid in seen:
            continue
        seen.add(uid)
        try:
            u = await m.get_sender()
        except Exception:
            continue
        if not isinstance(u, User) or u.bot:
            continue
        row = _build_user_row(u)
        ok, _reason = filters.user_passes_filters(row, user_flt)
        if not ok:
            await storage.bump_task_counters(session, task_id, filtered_delta=1)
            await session.commit()
            continue
        pu = await storage.upsert_user(session, source_task_id=task_id, **row)
        await storage.add_user_source_edge(
            session,
            parsed_user=pu,
            source_entity_id=source_entity_id,
            source_entity_kind=source_entity_kind,
            source_kind=source_kind,
            source_task_id=task_id,
        )
        await storage.bump_task_counters(session, task_id, found_delta=1)
        await session.commit()
        await log(account_id, "info", "user_upsert", f"from_msg {uid}", {"telegram_id": uid})


async def _collect_participants(
    client: TelegramClient,
    entity: Any,
    *,
    task_id: int,
    account_id: int,
    source_entity_id: int,
    source_entity_kind: str,
    source_kind: str,
    session,
    user_flt: dict[str, Any],
    log: LogFn,
    recent_only: bool,
    limit_users: int,
) -> None:
    kwargs: dict[str, Any] = {"limit": limit_users}
    if recent_only:
        kwargs["filter"] = ChannelParticipantsRecent()
    async for u in client.iter_participants(entity, **kwargs):
        if not isinstance(u, User) or u.bot:
            continue
        row = _build_user_row(u)
        ok, _reason = filters.user_passes_filters(row, user_flt)
        if not ok:
            await storage.bump_task_counters(session, task_id, filtered_delta=1)
            await session.commit()
            continue
        pu = await storage.upsert_user(session, source_task_id=task_id, **row)
        await storage.add_user_source_edge(
            session,
            parsed_user=pu,
            source_entity_id=source_entity_id,
            source_entity_kind=source_entity_kind,
            source_kind=source_kind,
            source_task_id=task_id,
        )
        await storage.bump_task_counters(session, task_id, found_delta=1)
        await session.commit()
        await log(account_id, "info", "user_upsert", f"member {u.id}", {"telegram_id": int(u.id)})


async def run_users_task(
    session,
    task: ParsingTask,
    pool: RotatingClients,
    log: LogFn,
) -> None:
    params = task.params_json if isinstance(task.params_json, dict) else {}
    user_flt = params.get("filters") or {}
    if not isinstance(user_flt, dict):
        user_flt = {}
    mode = (task.mode or "max_coverage").lower()
    manual = querygen.manual_queries_from_txt(str(params.get("manual_usernames_text") or ""))
    peers = querygen.manual_queries_from_txt(str(params.get("user_inputs_text") or ""))
    if not peers:
        peers = querygen.manual_queries_from_txt(str(params.get("peers_text") or ""))
    peers = querygen.merge_query_lists(peers, manual)
    max_per_source = int(params.get("max_users_per_source") or 250)
    source_opts = params.get("source_options") or {}
    if not isinstance(source_opts, dict):
        source_opts = {}
    group_members = bool(source_opts.get("group_members", True))
    group_active = bool(source_opts.get("group_active", True))
    channel_commenters = bool(source_opts.get("channel_commenters", True))
    channel_active_if_discussion = bool(source_opts.get("channel_active_if_discussion", True))

    if not peers:
        await log(None, "error", "empty_sources", "Добавьте @username / t.me ссылки или загрузите txt")
        raise ValueError("empty_sources: peers text is empty")

    async def handle_peer(peer: str, account_id: int, client: TelegramClient) -> None:
        await log(account_id, "info", "resolve", f"peer={peer}")
        cur_status = await session.scalar(
            select(ParsingTask.status).where(ParsingTask.id == task.id)
        )
        if cur_status == "cancelled":
            await log(account_id, "info", "cancelled", "Task cancelled before resolve")
            return
        ent = await client.get_entity(peer)
        src_id = int(ent.id)
        is_mg = isinstance(ent, Channel) and bool(getattr(ent, "megagroup", False))
        is_bc = isinstance(ent, Channel) and bool(getattr(ent, "broadcast", False)) and not is_mg
        has_discussion = False
        if is_bc:
            try:
                inp = await client.get_input_entity(ent)
                full = await client(GetFullChannelRequest(channel=inp))
                has_discussion = bool(getattr(full.full_chat, "linked_chat_id", None))
            except Exception:
                has_discussion = False

        modes: list[str] = []
        if is_mg:
            if group_members:
                modes.append("members")
            if group_active:
                modes.append("active")
        elif is_bc:
            if channel_commenters:
                modes.append("commenters")
            if channel_active_if_discussion and has_discussion:
                modes.append("active")
        if mode == "active_only":
            modes = [m for m in modes if m in ("active", "commenters")]

        for mname in dict.fromkeys(modes):
            cur_status = await session.scalar(
                select(ParsingTask.status).where(ParsingTask.id == task.id)
            )
            if cur_status == "cancelled":
                return
            if is_mg:
                if mname in ("members", "active"):
                    await _collect_participants(
                        client,
                        ent,
                        task_id=task.id,
                        account_id=account_id,
                        source_entity_id=src_id,
                        source_entity_kind="group",
                        source_kind="active" if mname == "active" else "member",
                        session=session,
                        user_flt=user_flt,
                        log=log,
                        recent_only=(mname == "active"),
                        limit_users=max_per_source,
                    )
                elif mname == "commenters":
                    await _collect_from_messages(
                        client,
                        ent,
                        task_id=task.id,
                        account_id=account_id,
                        source_entity_id=src_id,
                        source_entity_kind="group",
                        source_kind="commenter",
                        session=session,
                        user_flt=user_flt,
                        log=log,
                    )
            elif is_bc:
                if mname in ("commenters", "active", "members"):
                    await _collect_from_messages(
                        client,
                        ent,
                        task_id=task.id,
                        account_id=account_id,
                        source_entity_id=src_id,
                        source_entity_kind="channel",
                        source_kind="commenter" if mname == "commenters" else "active",
                        session=session,
                        user_flt=user_flt,
                        log=log,
                    )
            else:
                await log(account_id, "warn", "skip_entity", f"unsupported entity for {peer}")

    total = max(len(peers), 1)
    for idx, peer in enumerate(peers):
        if not peer:
            continue
        aid, client = await pool.next_client()
        prog = min(95, int(10 + (idx + 1) / total * 80))
        await storage.bump_task_counters(
            session,
            task.id,
            progress_percent=prog,
            current_stage="collect",
            current_account_id=aid,
            current_query=peer,
        )
        await session.commit()
        try:
            await handle_peer(peer, aid, client)
        except Exception as e:
            await storage.bump_task_counters(session, task.id, error_delta=1)
            await session.commit()
            await log(aid, "error", "peer_failed", str(e)[:500], {"peer": peer})
