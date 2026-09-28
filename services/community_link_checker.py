"""Read-only ownership checks for public Telegram channels and supergroups."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from telethon.tl.functions.channels import GetParticipantRequest
from telethon.tl.types import (
    Channel,
    ChannelParticipantAdmin,
    ChannelParticipantCreator,
)

from bot.config import SESSIONS_DIR
from database.models import AccountStatus, CommunityLinkCheck
from database.repositories import AccountRepository
from database.session import session_scope
from database.sqlite_pragmas import commit_with_busy_retry
from sqlalchemy import select, desc
from workers.manager import account_worker_for_action


_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")
_OWNERS = (
    ChannelParticipantCreator,
    ChannelParticipantAdmin,
)


def _parse_public_link(link: str) -> str | None:
    if not isinstance(link, str):
        return None
    try:
        parsed = urlsplit(link)
        if (parsed.scheme.lower() != "https" or parsed.netloc.lower() not in {"t.me", "www.t.me"}
                or "?" in link or "#" in link or parsed.username or parsed.password):
            return None
        path = parsed.path
    except (TypeError, ValueError):
        return None
    if not path.startswith("/") or path.count("/") != 1:
        return None
    username = path[1:]
    if not _USERNAME_RE.fullmatch(username) or username.casefold() in {"joinchat", "addstickers", "share"}:
        return None
    return username


def parse_community_link_batch(raw: str) -> dict:
    """Parse up to 20 newline-separated public links without side effects."""
    if len(raw) > 4096:
        raise ValueError("batch exceeds 4096 characters")

    rows = [(line_number, line.strip()) for line_number, line in enumerate(raw.splitlines(), 1)
            if line.strip()]
    if len(rows) > 20:
        raise ValueError("batch exceeds 20 non-empty lines")

    links: list[str] = []
    errors: list[dict] = []
    duplicates: list[dict] = []
    seen: set[str] = set()
    for line_number, line in rows:
        username = _parse_public_link(line)
        if username is None:
            errors.append({"line": line_number, "reason": "invalid_link"})
            continue
        key = username.casefold()
        if key in seen:
            duplicates.append({"line": line_number, "reason": "duplicate_link"})
            continue
        seen.add(key)
        links.append(f"https://t.me/{username}")
    return {"links": links, "errors": errors, "duplicates": duplicates}


def _result(*, status: str, reason: str, canonical_link: str | None = None,
            title: str | None = None, kind: str | None = None,
            username: str | None = None, account_name: str | None = None) -> dict:
    return {
        "status": status,
        "reason": reason,
        "canonical_link": canonical_link,
        "title": title,
        "kind": kind,
        "username": username,
        "account_name": account_name,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


async def _check_owned_community_link(account_id: int, link: str) -> dict:
    """Check a public channel/supergroup and whether the selected account is admin.

    The operation only resolves the public entity and asks Telegram for that
    account's participant record. It never joins or interacts with the target.
    """
    username = _parse_public_link(link)
    if username is None:
        return _result(status="failed", reason="invalid_link")

    async with session_scope() as session:
        account = await AccountRepository.get_by_id(session, int(account_id))
    if account is None:
        return _result(status="failed", reason="account_not_found", username=username,
                       canonical_link=f"https://t.me/{username}")
    if account.status != AccountStatus.ACTIVE:
        return _result(status="failed", reason="account_unavailable", username=username,
                       canonical_link=f"https://t.me/{username}")

    account_name = getattr(account, "display_title", None) or getattr(account, "username", None)
    session_path = Path(SESSIONS_DIR) / f"{account.session_name}.session"
    if not session_path.is_file():
        return _result(status="failed", reason="session_unavailable", username=username,
                       canonical_link=f"https://t.me/{username}", account_name=account_name)

    worker = account_worker_for_action(account, session_path, account.proxy)
    connected_here = False
    try:
        connected_here = not bool(getattr(worker, "is_connected", False))
        if not await worker.connect(quiet=True):
            return _result(status="failed", reason="account_unavailable", username=username,
                           canonical_link=f"https://t.me/{username}", account_name=account_name)
        entity = await worker.client.get_entity(username)
        if isinstance(entity, Channel):
            kind = "supergroup" if entity.megagroup else "channel" if entity.broadcast else None
        else:
            kind = None
        if kind is None:
            return _result(status="failed", reason="unavailable_or_not_owned", username=username,
                           canonical_link=f"https://t.me/{username}", account_name=account_name)

        participant = await worker.client(GetParticipantRequest(entity, "me"))
        if not isinstance(participant.participant, _OWNERS):
            return _result(status="failed", reason="unavailable_or_not_owned", username=username,
                           canonical_link=f"https://t.me/{username}", account_name=account_name)
        return _result(status="ok", reason="ok", username=username,
                       canonical_link=f"https://t.me/{username}", title=entity.title,
                       kind=kind, account_name=account_name)
    except Exception:
        # Avoid returning Telegram exception text, which can contain account data.
        return _result(status="failed", reason="unavailable_or_not_owned", username=username,
                       canonical_link=f"https://t.me/{username}", account_name=account_name)
    finally:
        if connected_here:
            try:
                await worker.disconnect()
            except Exception:
                pass


async def check_owned_community_link(account_id: int, link: str, actor_id: int | None = None) -> dict:
    """Check ownership and record every syntactically valid attempt."""
    if _parse_public_link(link) is None:
        return await _check_owned_community_link(account_id, link)

    result = await _check_owned_community_link(account_id, link)
    checked = datetime.fromisoformat(result["checked_at"])
    if checked.tzinfo is not None:
        checked = checked.astimezone(timezone.utc).replace(tzinfo=None)
    record = CommunityLinkCheck(
        account_id=int(account_id), actor_id=actor_id,
        canonical_link=result["canonical_link"], status=result["status"],
        reason=result["reason"],
        title=result["title"] if result["status"] == "ok" else None,
        kind=result["kind"] if result["status"] == "ok" else None,
        checked_at=checked,
    )
    async with session_scope() as session:
        session.add(record)
        await commit_with_busy_retry(session, op_name="community-link-check")
    return result


async def list_owned_community_link_checks(
    limit: int = 20, account_id: int | None = None, actor_id: int | None = None,
) -> list[dict]:
    """Return recent checks without contacting Telegram."""
    bounded_limit = max(1, min(int(limit), 100))
    statement = select(CommunityLinkCheck).order_by(
        desc(CommunityLinkCheck.checked_at), desc(CommunityLinkCheck.id)
    ).limit(bounded_limit)
    if account_id is not None:
        statement = statement.where(CommunityLinkCheck.account_id == int(account_id))
    if actor_id is not None:
        statement = statement.where(CommunityLinkCheck.actor_id == int(actor_id))
    async with session_scope() as session:
        rows = (await session.execute(statement)).scalars().all()
    return [{
        "id": row.id, "account_id": row.account_id, "actor_id": row.actor_id,
        "canonical_link": row.canonical_link, "status": row.status, "reason": row.reason,
        "title": row.title, "kind": row.kind,
        "checked_at": row.checked_at.replace(tzinfo=timezone.utc).isoformat(),
    } for row in rows]
