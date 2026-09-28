"""Operator directed reaction to one verified post in an account-administered chat."""
from __future__ import annotations

import asyncio
import re
from datetime import timedelta
from urllib.parse import urlsplit

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import selectinload
from telethon import errors
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.tl.functions.messages import SendReactionRequest
from telethon.tl.types import Channel, ChannelParticipantAdmin, ChannelParticipantCreator, ReactionEmoji

from bot.config import SESSIONS_DIR
from database.models import Account, ManagedReactionAttempt
from database.session import session_scope
from database.sqlite_pragmas import commit_with_busy_retry, execute_with_busy_retry
from services.account_safety import pause_account, reserve_send
from utils.time import utcnow_naive
from workers.manager import account_worker_for_action


EMOJI_WHITELIST = frozenset({"👍", "❤️", "🔥"})
_PUBLIC = re.compile(r"[A-Za-z][A-Za-z0-9_]{4,31}")
_NUMBER = re.compile(r"[1-9][0-9]*")


class ManagedReactionError(ValueError):
    """An operator-facing validation or eligibility rejection."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _parse_target(link: str, emoji: str) -> tuple[str | int, int, str]:
    if emoji not in EMOJI_WHITELIST:
        raise ManagedReactionError("invalid_emoji")
    if not isinstance(link, str) or len(link) > 255:
        raise ManagedReactionError("invalid_link")
    candidate = link.strip()
    if "://" not in candidate:
        candidate = "https://" + candidate
    parsed = urlsplit(candidate)
    if (parsed.scheme != "https" or parsed.netloc.lower() not in {"t.me", "www.t.me"}
            or parsed.query or parsed.fragment or parsed.username or parsed.password):
        raise ManagedReactionError("invalid_link")
    parts = parsed.path.strip("/").split("/")
    if len(parts) == 2 and _PUBLIC.fullmatch(parts[0]) and _NUMBER.fullmatch(parts[1]):
        return parts[0], int(parts[1]), f"https://t.me/{parts[0]}/{parts[1]}"
    if (len(parts) == 3 and parts[0] == "c" and _NUMBER.fullmatch(parts[1])
            and _NUMBER.fullmatch(parts[2])):
        return int("-100" + parts[1]), int(parts[2]), f"https://t.me/c/{parts[1]}/{parts[2]}"
    raise ManagedReactionError("invalid_link")


async def _account(account_id: int) -> Account:
    async with session_scope() as session:
        account = await session.scalar(
            select(Account).options(selectinload(Account.proxy)).where(Account.id == int(account_id))
        )
        if account is None:
            raise ManagedReactionError("account_missing")
        return account


async def _target_details(client, peer_ref: str | int, message_id: int, link: str, emoji: str,
                          account: Account) -> tuple[dict, Channel]:
    try:
        entity = await client.get_entity(peer_ref)
    except (ValueError, errors.RPCError) as exc:
        raise ManagedReactionError("chat_unavailable") from exc
    if not isinstance(entity, Channel):
        raise ManagedReactionError("not_community")
    if isinstance(peer_ref, int) and entity.id != int(str(peer_ref)[4:]):
        raise ManagedReactionError("chat_mismatch")
    try:
        membership = await client(GetParticipantRequest(channel=entity, participant="me"))
    except errors.RPCError as exc:
        raise ManagedReactionError("permission_unverified") from exc
    if not isinstance(membership.participant, (ChannelParticipantCreator, ChannelParticipantAdmin)):
        raise ManagedReactionError("admin_required")
    try:
        message = await client.get_messages(entity, ids=message_id)
    except errors.RPCError as exc:
        raise ManagedReactionError("message_unavailable") from exc
    if message is None or getattr(message, "id", None) != message_id:
        raise ManagedReactionError("message_missing")
    details = {
        "chat_title": entity.title or "",
        "peer_id": int(entity.id),
        "message_id": message_id,
        "text_excerpt": (getattr(message, "message", None) or "")[:240],
        "account_name": account.display_title,
        "link": link,
        "emoji": emoji,
    }
    return details, entity


async def _worker_for(account: Account):
    worker = account_worker_for_action(
        account, SESSIONS_DIR / f"{account.session_name}.session", account.proxy,
    )
    if not await worker.connect(quiet=True) or worker.client is None:
        raise ManagedReactionError("worker_unavailable")
    return worker


async def preview_managed_reaction(account_id: int, link: str, emoji: str) -> dict:
    """Read eligibility and the exact post without reserving a send attempt."""
    peer_ref, message_id, canonical_link = _parse_target(link, emoji)
    account = await _account(account_id)
    worker = await _worker_for(account)
    try:
        async with worker._connection_lock:
            details, entity = await _target_details(
                worker.client, peer_ref, message_id, canonical_link, emoji, account,
            )
            details["already_attempted"] = await _prior_attempt(
                int(account_id), int(entity.id), message_id,
            )
        return details
    finally:
        await worker.disconnect()


async def _prior_attempt(account_id: int, peer_id: int, message_id: int) -> bool:
    async with session_scope() as session:
        return await session.scalar(
            select(ManagedReactionAttempt.id).where(
                ManagedReactionAttempt.account_id == account_id,
                ManagedReactionAttempt.peer_id == peer_id,
                ManagedReactionAttempt.message_id == message_id,
            )
        ) is not None


async def _claim_attempt(account_id: int, peer_id: int, message_id: int, actor_id: int,
                         link: str, emoji: str, chat_title: str) -> bool:
    async with session_scope() as session:
        result = await execute_with_busy_retry(
            session,
            sqlite_insert(ManagedReactionAttempt).values(
                account_id=account_id, peer_id=peer_id, message_id=message_id,
                actor_id=actor_id, chat_title=chat_title[:255], link=link, emoji=emoji,
                status="uncertain",
                created_at=utcnow_naive(), updated_at=utcnow_naive(),
            ).on_conflict_do_nothing(index_elements=[
                ManagedReactionAttempt.account_id, ManagedReactionAttempt.peer_id,
                ManagedReactionAttempt.message_id,
            ]),
            op_name="managed-reaction-claim",
        )
        await commit_with_busy_retry(session, op_name="managed-reaction-claim")
        return result.rowcount == 1


async def _finish_attempt(account_id: int, peer_id: int, message_id: int,
                          status: str, reason: str | None = None) -> None:
    async with session_scope() as session:
        await execute_with_busy_retry(
            session,
            update(ManagedReactionAttempt).where(
                ManagedReactionAttempt.account_id == account_id,
                ManagedReactionAttempt.peer_id == peer_id,
                ManagedReactionAttempt.message_id == message_id,
            ).values(status=status, reason=reason, updated_at=utcnow_naive()),
            op_name="managed-reaction-finish",
        )
        await commit_with_busy_retry(session, op_name="managed-reaction-finish")


async def send_managed_reaction(
    account_id: int, link: str, emoji: str, actor_id: int,
    *, expected_peer_id: int | None = None,
) -> dict:
    """Verify again, reserve once, persist no-retry audit, then send one RPC."""
    try:
        peer_ref, message_id, canonical_link = _parse_target(link, emoji)
        account = await _account(account_id)
        worker = await _worker_for(account)
    except ManagedReactionError as exc:
        return {"status": "failed", "reason": exc.reason}
    try:
        async with worker._send_lock:
            async with worker._connection_lock:
                try:
                    details, entity = await _target_details(
                        worker.client, peer_ref, message_id, canonical_link, emoji, account,
                    )
                except ManagedReactionError as exc:
                    return {"status": "failed", "reason": exc.reason}
                peer_id = int(entity.id)
                if expected_peer_id is not None and peer_id != int(expected_peer_id):
                    return {"status": "failed", "reason": "target_changed"}
                if await _prior_attempt(int(account_id), peer_id, message_id):
                    return {"status": "skipped", "reason": "already_attempted", **details}
                async with session_scope() as session:
                    allowed, reason = await reserve_send(
                        session, int(account_id), source="managed_reaction", peer_ref=str(peer_id),
                    )
                if not allowed:
                    return {"status": "skipped", "reason": reason, **details}
                if not await _claim_attempt(
                    int(account_id), peer_id, message_id, int(actor_id), canonical_link, emoji,
                    details["chat_title"],
                ):
                    return {"status": "skipped", "reason": "already_attempted", **details}
                try:
                    await worker.client(SendReactionRequest(
                        peer=entity, msg_id=message_id,
                        reaction=[ReactionEmoji(emoticon=emoji)], add_to_recent=False,
                    ))
                except errors.FloodWaitError as exc:
                    seconds = max(1, int(exc.seconds or 60))
                    await _finish_attempt(int(account_id), peer_id, message_id, "failed", "flood_wait")
                    async with session_scope() as session:
                        await pause_account(
                            session, int(account_id), reason_code="flood_wait",
                            source="managed_reaction", state="cooling_down",
                            resume_at=utcnow_naive() + timedelta(seconds=seconds),
                        )
                    return {"status": "failed", "reason": "flood_wait", "retry_after": seconds, **details}
                except (errors.ChatAdminRequiredError, errors.ReactionInvalidError,
                        errors.MessageIdInvalidError, errors.ChannelPrivateError):
                    await _finish_attempt(int(account_id), peer_id, message_id, "failed", "telegram_denied")
                    return {"status": "failed", "reason": "telegram_denied", **details}
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A timed-out RPC may already have succeeded. The unique audit blocks retries.
                    return {"status": "failed", "reason": "outcome_uncertain", **details}
                await _finish_attempt(int(account_id), peer_id, message_id, "sent")
                return {"status": "sent", "reason": "ok", **details}
    finally:
        await worker.disconnect()


async def list_managed_reaction_history(limit: int = 20) -> list[dict]:
    """Return recent audit rows without connecting an account or Telegram client."""
    bounded_limit = max(1, min(int(limit), 100))
    async with session_scope() as session:
        rows = (await session.execute(
            select(ManagedReactionAttempt, Account)
            .join(Account, Account.id == ManagedReactionAttempt.account_id)
            .order_by(ManagedReactionAttempt.id.desc())
            .limit(bounded_limit)
        )).all()
    return [
        {
            "account_id": account.id,
            "account_name": account.display_title,
            "chat_title": attempt.chat_title,
            "message_id": attempt.message_id,
            "emoji": attempt.emoji,
            "status": attempt.status,
            "reason": attempt.reason,
            "link": attempt.link,
            "created_at": attempt.created_at.isoformat() if attempt.created_at else None,
        }
        for attempt, account in rows
    ]
