"""Durable, read-only community checks on the existing BotCommand queue.

Each claim checks at most one link. If the process crashes after the Telegram read
but before the checkpoint commit, that one read may be repeated after restart.
No sends or joins are performed by this job.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from database.models import Account, AccountStatus, BotCommand
from database.session import session_scope
from database.sqlite_pragmas import commit_with_busy_retry, execute_with_busy_retry
from services.community_link_checker import _parse_public_link, check_owned_community_link
from utils.time import utcnow_naive


COMMAND = "community_link_check.batch"
MAX_LINKS = 20
MAX_SCHEDULE_AHEAD = timedelta(days=30)


def _schedule_due_at(
    now: datetime, delay_seconds: int | None, start_at_utc: datetime | None,
) -> datetime:
    if (delay_seconds is None) == (start_at_utc is None):
        raise ValueError("invalid_schedule")
    if start_at_utc is not None:
        if start_at_utc.tzinfo is None or start_at_utc.utcoffset() is None:
            raise ValueError("invalid_schedule")
        due = start_at_utc.astimezone(timezone.utc).replace(tzinfo=None)
        if not timedelta(minutes=5) <= due - now <= MAX_SCHEDULE_AHEAD:
            raise ValueError("invalid_schedule")
        return due
    if (not isinstance(delay_seconds, int) or isinstance(delay_seconds, bool)
            or not 0 <= delay_seconds <= 86400):
        raise ValueError("invalid_schedule")
    return now + timedelta(seconds=delay_seconds)


def _decode(raw: str | None) -> dict | None:
    try:
        value = json.loads(raw or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    links = value.get("links")
    owned = value.get("owned_titles")
    index = value.get("next_index")
    other = value.get("other_count")
    if (not isinstance(links, list) or not 1 <= len(links) <= MAX_LINKS
            or not isinstance(owned, list) or not isinstance(index, int)
            or isinstance(index, bool) or not 0 <= index <= len(links)
            or not isinstance(other, int) or isinstance(other, bool)
            or not 0 <= other <= index or len(owned) != index - other
            or not isinstance(value.get("account_id"), int) or isinstance(value.get("account_id"), bool)
            or value["account_id"] <= 0
            or not isinstance(value.get("actor_id"), int) or isinstance(value.get("actor_id"), bool)
            or value["actor_id"] <= 0
            or not isinstance(value.get("cancel_requested"), bool)
            or any(not isinstance(title, str) or len(title) > 100 for title in owned)):
        return None
    if any(not isinstance(link, str) or _parse_public_link(link) is None
           or link != f"https://t.me/{_parse_public_link(link)}" for link in links):
        return None
    if len({_parse_public_link(link).casefold() for link in links}) != len(links):
        return None
    return value


def _view(row: BotCommand, args: dict | None) -> dict:
    # Never expose target links, failed titles, or exception strings to callers.
    args = args or {}
    due = row.not_before.replace(tzinfo=timezone.utc).isoformat() if row.not_before else None
    return {
        "id": row.id, "status": row.status, "due_at": due,
        "created_at": row.created_at.replace(tzinfo=timezone.utc).isoformat() if row.created_at else None,
        "checked": args.get("next_index", 0), "total": len(args.get("links", [])),
        "owned_count": len(args.get("owned_titles", [])),
        "other_count": args.get("other_count", 0),
        "owned_titles": list(args.get("owned_titles", [])),
    }


async def enqueue_owned_community_batch(
    account_id: int, actor_id: int, links: list[str], delay_seconds: int | None = None,
    *, start_at_utc: datetime | None = None,
) -> dict:
    """Queue 1..20 already canonical public links for an active account."""
    if (not isinstance(account_id, int) or isinstance(account_id, bool) or account_id <= 0
            or not isinstance(actor_id, int) or isinstance(actor_id, bool) or actor_id <= 0
            or not isinstance(links, list) or not 1 <= len(links) <= MAX_LINKS):
        raise ValueError("invalid_batch")
    due_at = _schedule_due_at(utcnow_naive(), delay_seconds, start_at_utc)
    args = {
        "account_id": account_id, "actor_id": actor_id, "links": links,
        "next_index": 0, "other_count": 0, "owned_titles": [],
        "cancel_requested": False,
    }
    if _decode(json.dumps(args)) is None:
        raise ValueError("invalid_batch")
    async with session_scope() as session:
        account = await session.get(Account, account_id)
        if account is None or account.status != AccountStatus.ACTIVE:
            raise ValueError("account_unavailable")
        row = BotCommand(
            command=COMMAND, args_json=json.dumps(args, ensure_ascii=False),
            status="pending", requested_by=f"community-link-batch:{actor_id}",
            not_before=due_at,
        )
        session.add(row)
        await commit_with_busy_retry(session, op_name="community-batch-enqueue")
        await session.refresh(row)
        return _view(row, args)


async def get_owned_community_batch(command_id: int, actor_id: int) -> dict | None:
    if command_id <= 0 or actor_id <= 0:
        return None
    async with session_scope() as session:
        row = (await session.execute(select(BotCommand).where(
            BotCommand.id == command_id, BotCommand.command == COMMAND,
            BotCommand.requested_by == f"community-link-batch:{actor_id}",
        ))).scalar_one_or_none()
        return _view(row, _decode(row.args_json)) if row else None


async def list_owned_community_batches(actor_id: int, *, limit: int = 20) -> list[dict]:
    if actor_id <= 0 or not 1 <= limit <= 50:
        raise ValueError("invalid_history_request")
    async with session_scope() as session:
        rows = (await session.execute(select(BotCommand).where(
            BotCommand.command == COMMAND,
            BotCommand.requested_by == f"community-link-batch:{actor_id}",
        ).order_by(BotCommand.id.desc()).limit(limit))).scalars().all()
        return [_view(row, _decode(row.args_json)) for row in rows]


async def cancel_owned_community_batch(command_id: int, actor_id: int) -> dict | None:
    """Cancel a pending job now, or request stop after its current Telegram read."""
    if command_id <= 0 or actor_id <= 0:
        return None
    for _ in range(5):
        async with session_scope() as session:
            row = (await session.execute(select(BotCommand).where(
                BotCommand.id == command_id, BotCommand.command == COMMAND,
                BotCommand.requested_by == f"community-link-batch:{actor_id}",
            ))).scalar_one_or_none()
            if row is None:
                return None
            args = _decode(row.args_json)
            if row.status not in ("pending", "processing"):
                return _view(row, args)
            if args is None:
                await _fail_malformed(session, row, expected_status=row.status)
                return _view(row, None)
            args["cancel_requested"] = True
            values = {"args_json": json.dumps(args, ensure_ascii=False)}
            if row.status == "pending":
                values.update(status="cancelled", processed_at=utcnow_naive())
            result = await execute_with_busy_retry(
                session, update(BotCommand).where(
                    BotCommand.id == row.id, BotCommand.status == row.status,
                    BotCommand.args_json == row.args_json,
                ).values(**values), op_name="community-batch-cancel",
            )
            if result.rowcount == 1:
                await commit_with_busy_retry(session, op_name="community-batch-cancel")
                row.status = values.get("status", row.status)
                return _view(row, args)
            await session.rollback()
    raise RuntimeError("batch_conflict")


async def _fail_malformed(session, row: BotCommand, *, expected_status: str = "processing") -> None:
    await execute_with_busy_retry(session, update(BotCommand).where(
        BotCommand.id == row.id, BotCommand.status == expected_status,
    ).values(status="failed", error="invalid_batch", processed_at=utcnow_naive()),
        op_name="community-batch-invalid")
    await commit_with_busy_retry(session, op_name="community-batch-invalid")


async def recover_owned_community_batches(*, scope_factory=None) -> None:
    """Replay only this command's interrupted processing rows from their checkpoint."""
    async with (scope_factory or session_scope)() as session:
        rows = list((await session.execute(select(BotCommand).where(
            BotCommand.command == COMMAND, BotCommand.status == "processing",
        ))).scalars())
        for row in rows:
            args = _decode(row.args_json)
            row.status = "pending" if args and not args["cancel_requested"] else (
                "cancelled" if args else "failed"
            )
            row.error = None if args else "invalid_batch"
            row.processed_at = utcnow_naive() if row.status != "pending" else None
            row.not_before = None
        if rows:
            await commit_with_busy_retry(session, op_name="community-batch-recover")


async def process_owned_community_batch_step(command_id: int, *, scope_factory=None) -> None:
    """One claimed command means at most one checker call, then a durable checkpoint."""
    scope = scope_factory or session_scope
    async with scope() as session:
        row = await session.get(BotCommand, command_id)
        if row is None or row.command != COMMAND or row.status != "processing":
            return
        args = _decode(row.args_json)
        if args is None or row.requested_by != f"community-link-batch:{args['actor_id']}":
            await _fail_malformed(session, row)
            return
        account = await session.get(Account, args["account_id"])
        if account is None or account.status != AccountStatus.ACTIVE:
            await execute_with_busy_retry(session, update(BotCommand).where(
                BotCommand.id == command_id, BotCommand.status == "processing",
            ).values(status="failed", error="account_unavailable", processed_at=utcnow_naive()),
                op_name="community-batch-stale")
            await commit_with_busy_retry(session, op_name="community-batch-stale")
            return
        if args["cancel_requested"] or args["next_index"] == len(args["links"]):
            final = "cancelled" if args["cancel_requested"] else "done"
            await execute_with_busy_retry(session, update(BotCommand).where(
                BotCommand.id == command_id, BotCommand.status == "processing",
            ).values(status=final, processed_at=utcnow_naive()), op_name="community-batch-finish")
            await commit_with_busy_retry(session, op_name="community-batch-finish")
            return
        link = args["links"][args["next_index"]]
        account_id = args["account_id"]
        actor_id = args["actor_id"]

    try:
        result = await check_owned_community_link(account_id, link, actor_id=actor_id)
    except Exception:
        # The checker also writes per-link history. A failure here may mean that
        # history was not committed, so do not report this link as checked.
        async with scope() as session:
            await execute_with_busy_retry(session, update(BotCommand).where(
                BotCommand.id == command_id, BotCommand.status == "processing",
            ).values(status="failed", error="check_unavailable", processed_at=utcnow_naive()),
                op_name="community-batch-check-failed")
            await commit_with_busy_retry(session, op_name="community-batch-check-failed")
        return

    # CAS handles a UI cancellation that arrives while the Telegram read is in flight.
    for _ in range(5):
        async with scope() as session:
            row = await session.get(BotCommand, command_id)
            if row is None or row.status != "processing":
                return
            args = _decode(row.args_json)
            if args is None:
                await _fail_malformed(session, row)
                return
            if result.get("status") == "ok":
                args["owned_titles"].append(str(result.get("title") or "")[:100])
            else:
                args["other_count"] += 1
            args["next_index"] += 1
            done = args["next_index"] >= len(args["links"])
            status = "cancelled" if args["cancel_requested"] else "done" if done else "pending"
            changed = await execute_with_busy_retry(session, update(BotCommand).where(
                BotCommand.id == command_id, BotCommand.status == "processing",
                BotCommand.args_json == row.args_json,
            ).values(
                args_json=json.dumps(args, ensure_ascii=False), status=status,
                not_before=None, processed_at=utcnow_naive() if status != "pending" else None,
            ), op_name="community-batch-checkpoint")
            if changed.rowcount == 1:
                await commit_with_busy_retry(session, op_name="community-batch-checkpoint")
                return
            await session.rollback()
    raise RuntimeError("batch_conflict")
