"""Сценарий сбора пользователей из групп/каналов (members / active / commenters)."""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from telethon import TelegramClient
from telethon.tl.types import Channel, User, ChannelParticipantsRecent

from database.models import ParsingTask
from workers.parser import filters, querygen, storage
from workers.parser.account_pool import RotatingClients

LogFn = Callable[..., Awaitable[None]]


def _display_name(u: User) -> str:
    fn = (getattr(u, "first_name", None) or "").strip()
    ln = (getattr(u, "last_name", None) or "").strip()
    return (fn + " " + ln).strip() or (u.username or str(u.id))


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
        un = u.username
        dn = _display_name(u)
        has_photo = bool(getattr(u, "photo", None))
        lang = filters.detect_lang_approx(dn + " " + (un or ""))
        susp = filters.user_looks_suspicious(
            username=un,
            telegram_id=int(u.id),
            has_avatar=has_photo,
        )
        row = {
            "telegram_id": int(u.id),
            "username": un,
            "display_name": dn,
            "has_avatar": has_photo,
            "lang_guess": lang,
            "is_deleted": bool(getattr(u, "deleted", False)),
            "is_suspicious": susp,
        }
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
        un = u.username
        dn = _display_name(u)
        has_photo = bool(getattr(u, "photo", None))
        lang = filters.detect_lang_approx(dn + " " + (un or ""))
        susp = filters.user_looks_suspicious(
            username=un,
            telegram_id=int(u.id),
            has_avatar=has_photo,
        )
        row = {
            "telegram_id": int(u.id),
            "username": un,
            "display_name": dn,
            "has_avatar": has_photo,
            "lang_guess": lang,
            "is_deleted": bool(getattr(u, "deleted", False)),
            "is_suspicious": susp,
        }
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
    raw_sources = params.get("sources") or []
    if not isinstance(raw_sources, list):
        raw_sources = []
    manual = querygen.manual_queries_from_txt(str(params.get("manual_usernames_text") or ""))
    max_per_source = int(params.get("max_users_per_source") or 250)

    specs: list[dict[str, Any]] = []
    for s in raw_sources:
        if isinstance(s, dict) and (s.get("peer") or "").strip():
            specs.append(s)
    for p in manual:
        specs.append(
            {
                "peer": p,
                "modes": ["members", "active", "commenters"],
            }
        )

    if not specs:
        await log(None, "error", "empty_sources", "Добавьте sources[] с peer или manual_usernames_text")
        raise ValueError("empty_sources: sources[].peer или manual_usernames_text")

    async def handle_spec(peer: str, modes: list[str], account_id: int, client: TelegramClient) -> None:
        await log(account_id, "info", "resolve", f"peer={peer}")
        ent = await client.get_entity(peer)
        src_id = int(ent.id)
        is_mg = isinstance(ent, Channel) and bool(getattr(ent, "megagroup", False))
        is_bc = isinstance(ent, Channel) and bool(getattr(ent, "broadcast", False)) and not is_mg

        if not isinstance(modes, list) or not modes:
            modes = ["members"]
        if mode == "active_only":
            modes = [m for m in modes if m in ("active", "commenters")]
            if not modes:
                modes = ["active"]

        for mname in modes:
            t2 = await session.get(ParsingTask, task.id)
            if t2 and t2.status == "cancelled":
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

    total = max(len(specs), 1)
    for idx, spec in enumerate(specs):
        peer = str(spec.get("peer") or "").strip()
        if not peer:
            continue
        modes = spec.get("modes") or ["members", "active"]
        aid, client = await pool.next_client()
        prog = min(95, int(10 + (idx + 1) / total * 80))
        await storage.bump_task_counters(
            session,
            task.id,
            progress_percent=prog,
            current_stage="users",
            current_account_id=aid,
            current_query=peer,
        )
        await session.commit()
        try:
            await handle_spec(peer, modes, aid, client)
        except Exception as e:
            await storage.bump_task_counters(session, task.id, error_delta=1)
            await session.commit()
            await log(aid, "error", "peer_failed", str(e)[:500], {"peer": peer})
