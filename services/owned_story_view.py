"""A single, operator-selected view of an active story in an administered channel.

``IncrementStoryViewsRequest`` is the Telegram RPC that records a view for an
exact story ID. ``ReadStoriesRequest`` is deliberately absent: it marks every
story through max_id read. Preview fetches metadata and never records a view.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import selectinload
from telethon import errors
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.tl.functions.stories import GetStoriesByIDRequest, IncrementStoryViewsRequest
from telethon.tl.types import Channel, ChannelParticipantAdmin, ChannelParticipantCreator, StoryItem

from bot.config import SESSIONS_DIR
from database.models import Account, OwnedStoryViewAttempt
from database.session import session_scope
from database.sqlite_pragmas import commit_with_busy_retry, execute_with_busy_retry
from services.account_safety import check_account_gate, pause_account, reserve_send
from utils.time import utcnow_naive
from workers.manager import account_worker_for_action


_PUBLIC = re.compile(r"[A-Za-z][A-Za-z0-9_]{4,31}")
_NUMBER = re.compile(r"[1-9][0-9]*")


class OwnedStoryViewError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _parse_link(link: str) -> tuple[str, int, str]:
    if not isinstance(link, str) or len(link) > 255:
        raise OwnedStoryViewError("invalid_link")
    candidate = link.strip()
    if "://" not in candidate:
        candidate = "https://" + candidate
    parsed = urlsplit(candidate)
    if (parsed.scheme != "https" or parsed.netloc.lower() not in {"t.me", "www.t.me"}
            or parsed.query or parsed.fragment or parsed.username or parsed.password):
        raise OwnedStoryViewError("invalid_link")
    parts = parsed.path.strip("/").split("/")
    if (len(parts) != 3 or not _PUBLIC.fullmatch(parts[0]) or parts[1] != "s"
            or not _NUMBER.fullmatch(parts[2])):
        raise OwnedStoryViewError("invalid_link")
    story_id = int(parts[2])
    if story_id > 2_147_483_647:
        raise OwnedStoryViewError("invalid_link")
    return parts[0], story_id, f"https://t.me/{parts[0]}/s/{story_id}"


async def _account(account_id: int) -> Account:
    async with session_scope() as session:
        account = await session.scalar(
            select(Account).options(selectinload(Account.proxy)).where(Account.id == int(account_id))
        )
        if account is None:
            raise OwnedStoryViewError("account_missing")
        allowed, reason = await check_account_gate(session, int(account_id))
        if not allowed:
            raise OwnedStoryViewError(reason)
        return account


async def _worker_for(account: Account):
    worker = account_worker_for_action(
        account, SESSIONS_DIR / f"{account.session_name}.session", account.proxy,
    )
    # Worker.connect obtains the cross-process session lease; borrowed workers
    # retain their existing lease and serialize through their action locks.
    if not await worker.connect(quiet=True) or worker.client is None:
        raise OwnedStoryViewError("worker_unavailable")
    return worker


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


async def _story_details(client, username: str, story_id: int, link: str, account: Account):
    try:
        entity = await client.get_entity(username)
    except (ValueError, errors.RPCError) as exc:
        raise OwnedStoryViewError("channel_unavailable") from exc
    if not isinstance(entity, Channel) or not entity.broadcast:
        raise OwnedStoryViewError("channel_required")
    # A username can be reassigned between preview and execution. The exact
    # peer ID is returned to the caller and must be supplied for execution.
    try:
        membership = await client(GetParticipantRequest(channel=entity, participant="me"))
    except errors.RPCError as exc:
        raise OwnedStoryViewError("permission_unverified") from exc
    if not isinstance(membership.participant, (ChannelParticipantCreator, ChannelParticipantAdmin)):
        raise OwnedStoryViewError("admin_required")
    try:
        response = await client(GetStoriesByIDRequest(peer=entity, id=[story_id]))
    except errors.RPCError as exc:
        raise OwnedStoryViewError("story_unavailable") from exc
    stories = getattr(response, "stories", ())
    story = next((item for item in stories if isinstance(item, StoryItem) and item.id == story_id), None)
    if story is None:
        raise OwnedStoryViewError("story_missing")
    expires = getattr(story, "expire_date", None)
    if not isinstance(expires, datetime):
        raise OwnedStoryViewError("expiry_unverified")
    expires_at = _naive_utc(expires)
    if expires_at <= utcnow_naive():
        raise OwnedStoryViewError("story_expired")
    return {
        "account_id": int(account.id), "account_name": account.display_title,
        "peer_id": int(entity.id), "channel_title": (entity.title or "")[:255],
        "story_id": story_id, "link": link, "expires_at": expires_at.isoformat(),
    }, entity, expires_at


async def _prior_attempt(account_id: int, peer_id: int, story_id: int) -> bool:
    async with session_scope() as session:
        return await session.scalar(select(OwnedStoryViewAttempt.id).where(
            OwnedStoryViewAttempt.account_id == account_id,
            OwnedStoryViewAttempt.peer_id == peer_id,
            OwnedStoryViewAttempt.story_id == story_id,
        )) is not None


async def preview_owned_story_view(account_id: int, link: str) -> dict:
    """Verify an active administered channel story without changing its views."""
    username, story_id, canonical = _parse_link(link)
    account = await _account(account_id)
    worker = await _worker_for(account)
    try:
        async with worker._connection_lock:
            details, _, _ = await _story_details(worker.client, username, story_id, canonical, account)
            details["already_attempted"] = await _prior_attempt(account.id, details["peer_id"], story_id)
        return details
    finally:
        await worker.disconnect()


async def _claim(account_id: int, peer_id: int, story_id: int, actor_id: int,
                 details: dict, expires_at: datetime) -> bool:
    now = utcnow_naive()
    async with session_scope() as session:
        result = await execute_with_busy_retry(session, sqlite_insert(OwnedStoryViewAttempt).values(
            account_id=account_id, peer_id=peer_id, story_id=story_id, actor_id=actor_id,
            channel_title=details["channel_title"], link=details["link"], expires_at=expires_at,
            status="uncertain", created_at=now, updated_at=now,
        ).on_conflict_do_nothing(index_elements=[
            OwnedStoryViewAttempt.account_id, OwnedStoryViewAttempt.peer_id,
            OwnedStoryViewAttempt.story_id,
        ]), op_name="owned-story-view-claim")
        await commit_with_busy_retry(session, op_name="owned-story-view-claim")
        return result.rowcount == 1


async def _finish(account_id: int, peer_id: int, story_id: int, status: str,
                  reason: str | None = None) -> None:
    async with session_scope() as session:
        await execute_with_busy_retry(session, update(OwnedStoryViewAttempt).where(
            OwnedStoryViewAttempt.account_id == account_id,
            OwnedStoryViewAttempt.peer_id == peer_id,
            OwnedStoryViewAttempt.story_id == story_id,
        ).values(status=status, reason=reason, updated_at=utcnow_naive()),
            op_name="owned-story-view-finish")
        await commit_with_busy_retry(session, op_name="owned-story-view-finish")


async def view_owned_story(account_id: int, link: str, actor_id: int,
                           *, expected_peer_id: int) -> dict:
    """Record at most one exact story view RPC after preview and revalidation."""
    if not isinstance(expected_peer_id, int) or expected_peer_id <= 0:
        return {"status": "failed", "reason": "preview_required"}
    if not isinstance(actor_id, int) or actor_id <= 0:
        return {"status": "failed", "reason": "actor_required"}
    try:
        username, story_id, canonical = _parse_link(link)
        account = await _account(account_id)
        worker = await _worker_for(account)
    except OwnedStoryViewError as exc:
        return {"status": "failed", "reason": exc.reason}
    try:
        async with worker._send_lock:
            async with worker._connection_lock:
                try:
                    details, entity, expires_at = await _story_details(
                        worker.client, username, story_id, canonical, account,
                    )
                except OwnedStoryViewError as exc:
                    return {"status": "failed", "reason": exc.reason}
                peer_id = details["peer_id"]
                if peer_id != int(expected_peer_id):
                    return {"status": "failed", "reason": "target_changed"}
                if await _prior_attempt(int(account_id), peer_id, story_id):
                    return {"status": "skipped", "reason": "already_attempted", **details}
                if expires_at <= utcnow_naive():
                    return {"status": "skipped", "reason": "story_expired", **details}
                async with session_scope() as session:
                    allowed, reason = await reserve_send(
                        session, int(account_id), source="owned_story_view", peer_ref=str(peer_id),
                    )
                if not allowed:
                    return {"status": "skipped", "reason": reason, **details}
                if not await _claim(int(account_id), peer_id, story_id, int(actor_id), details, expires_at):
                    return {"status": "skipped", "reason": "already_attempted", **details}
                try:
                    if expires_at <= utcnow_naive():
                        await _finish(int(account_id), peer_id, story_id, "skipped", "story_expired")
                        return {"status": "skipped", "reason": "story_expired", **details}
                    accepted = await worker.client(IncrementStoryViewsRequest(peer=entity, id=[story_id]))
                except errors.FloodWaitError as exc:
                    seconds = max(1, int(exc.seconds or 60))
                    await _finish(int(account_id), peer_id, story_id, "failed", "flood_wait")
                    async with session_scope() as session:
                        await pause_account(session, int(account_id), reason_code="flood_wait",
                                            source="owned_story_view", state="cooling_down",
                                            resume_at=utcnow_naive() + timedelta(seconds=seconds))
                    return {"status": "failed", "reason": "flood_wait", "retry_after": seconds, **details}
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A timed-out RPC may have succeeded; the claim blocks replay.
                    return {"status": "failed", "reason": "outcome_uncertain", **details}
                if accepted is not True:
                    await _finish(int(account_id), peer_id, story_id, "failed", "telegram_denied")
                    return {"status": "failed", "reason": "telegram_denied", **details}
                await _finish(int(account_id), peer_id, story_id, "accepted")
                return {"status": "accepted", "reason": "ok", **details}
    finally:
        await worker.disconnect()


async def list_owned_story_view_history(actor_id: int, limit: int = 20) -> list[dict]:
    """Return bounded audit rows for the requesting operator only."""
    bounded = max(1, min(int(limit), 100))
    async with session_scope() as session:
        rows = (await session.execute(
            select(OwnedStoryViewAttempt, Account)
            .join(Account, Account.id == OwnedStoryViewAttempt.account_id)
            .where(OwnedStoryViewAttempt.actor_id == int(actor_id))
            .order_by(OwnedStoryViewAttempt.id.desc()).limit(bounded)
        )).all()
    return [{
        "account_id": account.id, "account_name": account.display_title,
        "peer_id": row.peer_id, "channel_title": row.channel_title,
        "story_id": row.story_id, "link": row.link,
        "expires_at": row.expires_at.isoformat(), "status": row.status,
        "reason": row.reason, "created_at": row.created_at.isoformat(),
    } for row, account in rows]
