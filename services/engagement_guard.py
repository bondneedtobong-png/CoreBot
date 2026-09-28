"""Telegram-backed checks for replies in communities administered by the account."""
from __future__ import annotations

import re

from telethon.tl.functions.channels import GetParticipantRequest
from telethon.tl.types import Channel, ChannelParticipantAdmin, ChannelParticipantCreator


_MESSAGE_LINK = re.compile(
    r"^https://t\.me/(?:(?P<public>[A-Za-z0-9_]{5,32})|c/(?P<private>[1-9]\d*))/"
    r"(?P<message>[1-9]\d*)(?:\?.*)?$", re.I,
)
_GROUP_REF = re.compile(
    r"^(?:@(?P<public>[A-Za-z0-9_]{5,32})|https://t\.me/c/(?P<private>[1-9]\d*))/?$",
    re.I,
)


class EngagementTargetError(ValueError):
    """A stable, non-sensitive denial code safe for command status and UI."""


def parse_group_ref(ref: str) -> str | int:
    match = _GROUP_REF.fullmatch((ref or "").strip())
    if match is None:
        raise EngagementTargetError("invalid_group_ref")
    return match.group("public") or int("-100" + match.group("private"))


async def discover_managed_messages(client, group_ref: str) -> list[dict]:
    """Read a bounded recent window in one administered supergroup."""
    peer = parse_group_ref(group_ref)
    try:
        entity = await client.get_entity(peer)
    except Exception as exc:
        raise EngagementTargetError("target_unavailable") from exc
    if not isinstance(entity, Channel) or not entity.megagroup:
        raise EngagementTargetError("discussion_group_required")
    try:
        membership = await client(GetParticipantRequest(channel=entity, participant="me"))
    except Exception as exc:
        raise EngagementTargetError("admin_unverified") from exc
    if not isinstance(membership.participant, (ChannelParticipantAdmin, ChannelParticipantCreator)):
        raise EngagementTargetError("admin_required")
    try:
        me = await client.get_me()
        if me is None:
            raise EngagementTargetError("account_unavailable")
        recent = await client.get_messages(entity, limit=20)
    except EngagementTargetError:
        raise
    except Exception as exc:
        raise EngagementTargetError("messages_unavailable") from exc
    username = getattr(entity, "username", None)
    link_prefix = f"https://t.me/{username}" if username else f"https://t.me/c/{entity.id}"
    result = []
    for message in recent:
        if getattr(message, "out", False) or getattr(message, "sender_id", None) == me.id:
            continue
        source_text = (getattr(message, "raw_text", None) or "").strip()
        if not 1 <= len(source_text) <= 4000:
            continue
        sender = getattr(message, "sender", None)
        if sender is None:
            try:
                sender = await message.get_sender()
            except Exception:
                continue
        if sender is None or getattr(sender, "bot", False):
            continue
        message_id = getattr(message, "id", None)
        if not isinstance(message_id, int) or message_id <= 0:
            continue
        result.append({"message_link": f"{link_prefix}/{message_id}",
                       "source_text": source_text, "message_id": message_id})
        if len(result) == 10:
            break
    return result


def parse_message_link(link: str) -> tuple[str | int, int]:
    match = _MESSAGE_LINK.fullmatch((link or "").strip())
    if match is None:
        raise EngagementTargetError("invalid_message_link")
    peer = match.group("public") or int("-100" + match.group("private"))
    return peer, int(match.group("message"))


async def inspect_managed_message(
    client, link: str, *, expected_peer_id: int | None = None,
    expected_text: str | None = None,
) -> dict:
    """Read the exact message, requiring current admin membership in its group.

    Telegram errors are deliberately collapsed; exception strings may include
    usernames, phone numbers or proxy details.
    """
    peer, message_id = parse_message_link(link)
    try:
        entity = await client.get_entity(peer)
    except Exception as exc:
        raise EngagementTargetError("target_unavailable") from exc
    if not isinstance(entity, Channel) or not entity.megagroup:
        raise EngagementTargetError("discussion_group_required")
    if expected_peer_id is not None and int(entity.id) != int(expected_peer_id):
        raise EngagementTargetError("target_changed")
    try:
        membership = await client(GetParticipantRequest(channel=entity, participant="me"))
    except Exception as exc:
        raise EngagementTargetError("admin_unverified") from exc
    if not isinstance(membership.participant, (ChannelParticipantAdmin, ChannelParticipantCreator)):
        raise EngagementTargetError("admin_required")
    try:
        message = await client.get_messages(entity, ids=message_id)
    except Exception as exc:
        raise EngagementTargetError("message_unavailable") from exc
    if message is None or int(message.id) != message_id:
        raise EngagementTargetError("message_unavailable")
    source_text = (getattr(message, "raw_text", None) or getattr(message, "message", None) or "").strip()
    if not 3 <= len(source_text) <= 4000:
        raise EngagementTargetError("message_text_unavailable")
    if expected_text is not None and source_text != expected_text:
        raise EngagementTargetError("message_changed")
    return {
        "peer_id": int(entity.id),
        "peer_ref": f"-100{entity.id}",
        "message_id": message_id,
        "source_text": source_text,
        "managed_title": (entity.title or "")[:100],
        "message_link": f"https://t.me/c/{entity.id}/{message_id}",
    }
