"""Small, operator-confirmed text posts to communities administered by one account.

Each chat is one durable BotCommand. A claimed command is never retried after a
crash because Telegram may have accepted the post before the process stopped.
"""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import selectinload
from telethon import errors
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.tl.types import Channel, ChannelParticipantAdmin, ChannelParticipantCreator

from bot.config import SESSIONS_DIR
from database.models import Account, AccountStatus, BotCommand
from database.session import session_scope
from database.sqlite_pragmas import commit_with_busy_retry, execute_with_busy_retry
from services.account_safety import check_account_gate, pause_account, record_chat_cooldown, reserve_send
from services.community_link_checker import _parse_public_link
from utils.time import utcnow_naive
from workers.manager import account_worker_for_action

COMMAND = "owned_chat_campaign.send"
MAX_CHATS = 5
MAX_TEXT = 4000
MAX_DELAY_SECONDS = 86400


class CampaignError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def parse_links(raw: str) -> list[str]:
    if not isinstance(raw, str) or len(raw) > 2048:
        raise CampaignError("invalid_links")
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not 1 <= len(lines) <= MAX_CHATS:
        raise CampaignError("chat_count")
    usernames = [_parse_public_link(link) for link in lines]
    if any(name is None for name in usernames):
        raise CampaignError("invalid_links")
    if len({name.casefold() for name in usernames}) != len(lines):
        raise CampaignError("duplicate_chat")
    return [f"https://t.me/{name}" for name in usernames]


def validate_text(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT:
        raise CampaignError("invalid_text")
    return value.strip()


async def _account(account_id: int) -> Account:
    async with session_scope() as session:
        account = await session.scalar(select(Account).options(selectinload(Account.proxy)).where(Account.id == int(account_id)))
        if account is None or account.status != AccountStatus.ACTIVE:
            raise CampaignError("account_unavailable")
        allowed, reason = await check_account_gate(session, int(account_id))
        if not allowed:
            raise CampaignError(reason)
        return account


async def _resolve_owned(client, link: str, expected_peer_id: int | None = None) -> dict:
    username = _parse_public_link(link)
    if username is None:
        raise CampaignError("invalid_links")
    try:
        entity = await client.get_entity(username)
        if not isinstance(entity, Channel) or not (entity.megagroup or entity.broadcast):
            raise CampaignError("not_community")
        participant = await client(GetParticipantRequest(channel=entity, participant="me"))
    except CampaignError:
        raise
    except Exception as exc:
        raise CampaignError("ownership_unverified") from exc
    member = participant.participant
    if not isinstance(member, (ChannelParticipantAdmin, ChannelParticipantCreator)):
        raise CampaignError("admin_required")
    if (entity.broadcast and isinstance(member, ChannelParticipantAdmin)
            and not bool(getattr(getattr(member, "admin_rights", None), "post_messages", False))):
        raise CampaignError("posting_rights_required")
    if expected_peer_id is not None and entity.id != expected_peer_id:
        raise CampaignError("target_changed")
    return {"peer_id": int(entity.id), "title": (entity.title or "")[:100], "entity": entity}


async def preview_campaign(account_id: int, links: list[str], text: str) -> list[dict]:
    """Resolve every selected chat through the account; no send or join occurs."""
    if parse_links("\n".join(links)) != links:
        raise CampaignError("invalid_links")
    validate_text(text)
    account = await _account(account_id)
    worker = account_worker_for_action(
        account, SESSIONS_DIR / f"{account.session_name}.session", account.proxy,
    )
    if not await worker.connect(quiet=True) or worker.client is None:
        raise CampaignError("worker_unavailable")
    try:
        result = []
        for link in links:
            details = await _resolve_owned(worker.client, link)
            result.append({"link": link, "peer_id": details["peer_id"], "title": details["title"]})
        if len({item["peer_id"] for item in result}) != len(result):
            raise CampaignError("duplicate_chat")
        return result
    finally:
        await worker.disconnect()


def _decode(raw: str | None) -> dict | None:
    try:
        value = json.loads(raw or "")
        if not isinstance(value, dict) or set(value) != {
            "account_id", "actor_id", "link", "peer_id", "title", "text", "campaign_id",
        }:
            return None
        if not all(isinstance(value[key], int) and not isinstance(value[key], bool) and value[key] > 0
                   for key in ("account_id", "actor_id", "peer_id", "campaign_id")):
            return None
        if (not isinstance(value["link"], str) or parse_links(value["link"]) != [value["link"]]
                or not isinstance(value["title"], str) or len(value["title"]) > 100
                or validate_text(value["text"]) != value["text"]):
            return None
        return value
    except (ValueError, TypeError, CampaignError):
        return None


async def enqueue_campaign(account_id: int, actor_id: int, preview: list[dict], text: str,
                           delay_seconds: int) -> list[int]:
    """Freeze reviewed targets and text into up to five scheduled commands."""
    if not isinstance(delay_seconds, int) or isinstance(delay_seconds, bool) or not 0 <= delay_seconds <= MAX_DELAY_SECONDS:
        raise CampaignError("invalid_schedule")
    if not isinstance(actor_id, int) or actor_id <= 0:
        raise CampaignError("invalid_actor")
    text = validate_text(text)
    links = [item.get("link") for item in preview] if isinstance(preview, list) else []
    if parse_links("\n".join(links)) != links:
        raise CampaignError("invalid_links")
    if any(not isinstance(item.get("peer_id"), int) or item["peer_id"] <= 0
           or not isinstance(item.get("title"), str) or len(item["title"]) > 100 for item in preview):
        raise CampaignError("invalid_preview")
    if len({item["peer_id"] for item in preview}) != len(preview):
        raise CampaignError("duplicate_chat")
    await _account(account_id)
    due = utcnow_naive() + timedelta(seconds=delay_seconds)
    async with session_scope() as session:
        rows = []
        for item in preview:
            row = BotCommand(command=COMMAND, status="pending", requested_by=f"owned-chat:{actor_id}", not_before=due)
            session.add(row)
            rows.append(row)
        await session.flush()
        campaign_id = rows[0].id
        for row, item in zip(rows, preview):
            row.args_json = json.dumps({
                "account_id": account_id, "actor_id": actor_id, "link": item["link"],
                "peer_id": item["peer_id"], "title": item["title"], "text": text,
                "campaign_id": campaign_id,
            }, ensure_ascii=False)
        await commit_with_busy_retry(session, op_name="owned-chat-enqueue")
        return [int(row.id) for row in rows]


async def _finish(command_id: int, status: str, reason: str) -> None:
    async with session_scope() as session:
        await execute_with_busy_retry(session, update(BotCommand).where(
            BotCommand.id == command_id, BotCommand.command == COMMAND,
            BotCommand.status == "processing",
        ).values(status=status, error=reason[:120], processed_at=utcnow_naive()), op_name="owned-chat-finish")
        await commit_with_busy_retry(session, op_name="owned-chat-finish")


async def process_campaign_send(command_id: int) -> None:
    """One claimed command, one guarded RPC, no replay on uncertain outcome."""
    async with session_scope() as session:
        row = await session.get(BotCommand, int(command_id))
        if row is None or row.command != COMMAND or row.status != "processing":
            return
        args = _decode(row.args_json)
    if args is None:
        await _finish(command_id, "failed", "invalid_request")
        return
    try:
        account = await _account(args["account_id"])
        worker = account_worker_for_action(
            account, SESSIONS_DIR / f"{account.session_name}.session", account.proxy,
        )
        if not await worker.connect(quiet=True) or worker.client is None:
            raise CampaignError("worker_unavailable")
        try:
            async with worker._send_lock:
                # Cancellation may have arrived while connecting.
                async with session_scope() as session:
                    current = await session.get(BotCommand, command_id)
                    if current is None or current.status != "processing" or current.error == "stop_requested":
                        await _finish(command_id, "cancelled", "stopped")
                        return
                details = await _resolve_owned(worker.client, args["link"], args["peer_id"])
                async with session_scope() as session:
                    allowed, reason = await reserve_send(
                        session, args["account_id"], source="owned_chat_campaign",
                        peer_ref=str(args["peer_id"]),
                    )
                if not allowed:
                    raise CampaignError(reason)
                # Durable claim and budget reservation precede the sole Telegram send.
                try:
                    sent = await worker.client.send_message(details["entity"], args["text"])
                except errors.SlowModeWaitError as exc:
                    async with session_scope() as session:
                        await record_chat_cooldown(
                            session, args["account_id"], peer_ref=str(args["peer_id"]),
                            resume_at=utcnow_naive() + timedelta(seconds=max(1, int(exc.seconds or 1))),
                            source="owned_chat_campaign",
                        )
                    await _finish(command_id, "failed", "slow_mode")
                    return
                except errors.PeerFloodError:
                    async with session_scope() as session:
                        await pause_account(session, args["account_id"], reason_code="peer_flood",
                                            source="owned_chat_campaign")
                    await _finish(command_id, "failed", "peer_flood")
                    return
                except errors.FloodWaitError as exc:
                    async with session_scope() as session:
                        await pause_account(session, args["account_id"], reason_code="flood_wait",
                                            source="owned_chat_campaign", state="cooling_down",
                                            resume_at=utcnow_naive() + timedelta(seconds=max(1, int(exc.seconds or 60))))
                    await _finish(command_id, "failed", "flood_wait")
                    return
                except (errors.AuthKeyDuplicatedError, errors.AuthKeyUnregisteredError,
                        errors.SessionRevokedError):
                    async with session_scope() as session:
                        await pause_account(session, args["account_id"], reason_code="auth_invalid",
                                            source="owned_chat_campaign", state="needs_reauth")
                    await _finish(command_id, "failed", "auth_invalid")
                    return
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await _finish(command_id, "failed", "outcome_uncertain")
                    return
                await _finish(command_id, "done", f"sent:{int(sent.id)}")
        finally:
            await worker.disconnect()
    except CampaignError as exc:
        await _finish(command_id, "failed", exc.reason)


async def recover_campaign_sends(*, scope_factory=session_scope) -> None:
    """A processing send may already exist in Telegram: record uncertainty."""
    async with scope_factory() as session:
        await execute_with_busy_retry(session, update(BotCommand).where(
            BotCommand.command == COMMAND, BotCommand.status == "processing",
        ).values(status="failed", error="outcome_uncertain_after_restart", processed_at=utcnow_naive()),
            op_name="owned-chat-recover")
        await commit_with_busy_retry(session, op_name="owned-chat-recover")


async def stop_campaign(campaign_id: int, actor_id: int) -> int:
    """Cancel pending sends and request stop on a claimed send before its RPC."""
    async with session_scope() as session:
        rows = list((await session.execute(select(BotCommand).where(
            BotCommand.command == COMMAND, BotCommand.requested_by == f"owned-chat:{actor_id}",
        ))).scalars())
        matching = [row for row in rows if (args := _decode(row.args_json)) and args["campaign_id"] == campaign_id]
        if not matching:
            return 0
        count = 0
        for row in matching:
            if row.status == "pending":
                result = await execute_with_busy_retry(session, update(BotCommand).where(
                    BotCommand.id == row.id, BotCommand.status == "pending",
                ).values(status="cancelled", error="stopped", processed_at=utcnow_naive()),
                    op_name="owned-chat-stop-pending")
                count += result.rowcount
            elif row.status == "processing":
                result = await execute_with_busy_retry(session, update(BotCommand).where(
                    BotCommand.id == row.id, BotCommand.status == "processing",
                ).values(error="stop_requested"), op_name="owned-chat-stop-processing")
                count += result.rowcount
        await commit_with_busy_retry(session, op_name="owned-chat-stop")
        return count


async def campaign_history(actor_id: int, limit: int = 20) -> list[dict]:
    async with session_scope() as session:
        rows = list((await session.execute(select(BotCommand).where(
            BotCommand.command == COMMAND, BotCommand.requested_by == f"owned-chat:{actor_id}",
        ).order_by(BotCommand.id.desc()).limit(max(1, min(limit, 50))))).scalars())
    result = []
    for row in rows:
        args = _decode(row.args_json)
        if args:
            result.append({"id": row.id, "campaign_id": args["campaign_id"], "chat": args["title"],
                           "link": args["link"], "status": row.status, "reason": row.error,
                           "due_at": row.not_before, "processed_at": row.processed_at})
    return result
