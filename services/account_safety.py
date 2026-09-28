"""One persistent safety gate for every Telegram text send.

The attempt budget is reserved before an RPC. A failed or uncertain RPC still
counts as an attempt; this intentionally favours stopping over retrying.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import case, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import (
    Account, AccountChatCooldown, AccountSafetyEvent, AccountSafetyState, AccountStatus,
    Client, ClientClassCounter, ClientContactPermission, ClientStatus,
)
from database.sqlite_pragmas import commit_with_busy_retry, execute_with_busy_retry
from utils.time import utcnow_naive


POLICY_VERSION = "v2.2"


def normalize_peer_ref(peer: int | str) -> str:
    return str(peer).strip().casefold().lstrip("@")[:80]


async def has_contact_permission(session: AsyncSession, client_id: int) -> bool:
    client = await session.get(Client, int(client_id))
    if client is None or client.status in (ClientStatus.INVALID, ClientStatus.BLOCKED):
        return False
    permission = await session.get(ClientContactPermission, int(client_id))
    if permission is None or permission.state != "opt_in":
        return False
    suppressed = await session.scalar(
        select(ClientClassCounter.id).where(
            ClientClassCounter.client_id == int(client_id),
            ClientClassCounter.class_key.in_(["bl", "stop"]),
            ClientClassCounter.count > 0,
        ).limit(1)
    )
    return suppressed is None


async def check_account_gate(
    session: AsyncSession, account_id: int, *, peer_ref: str | None = None,
) -> tuple[bool, str]:
    """Read the persisted stop without spending an outbound attempt."""
    now = utcnow_naive()
    account = await session.get(Account, int(account_id))
    if account is None:
        return False, "account_missing"
    state = await session.get(AccountSafetyState, int(account_id))
    if state is not None and state.state != "ready":
        return False, state.reason_code or state.state
    if account.status != AccountStatus.ACTIVE:
        return False, "account_not_active"
    if account.is_spam_blocked or (account.flood_wait_until and account.flood_wait_until > now):
        return False, "platform_restricted"
    if peer_ref is not None:
        cooldown = await session.get(AccountChatCooldown, (int(account_id), normalize_peer_ref(peer_ref)))
        if cooldown is not None and cooldown.resume_at > now:
            return False, cooldown.reason_code
    if max(0, int(account.daily_limit or 0)) == 0:
        return False, "daily_limit"
    return True, "ok"


async def reserve_send(
    session: AsyncSession, account_id: int, *, source: str, peer_ref: str | None = None,
    client_id: int | None = None,
) -> tuple[bool, str]:
    """Atomically reserve one outbound attempt for an active account."""
    account_id = int(account_id)
    now = utcnow_naive()
    today = now.date().isoformat()
    allowed, reason = await check_account_gate(session, account_id, peer_ref=peer_ref)
    if not allowed:
        return False, reason
    if source.startswith("mailing"):
        if client_id is None or not await has_contact_permission(session, client_id):
            return False, "contact_permission"
    account = await session.get(Account, account_id)
    limit = max(0, int(account.daily_limit or 0))
    if limit == 0:
        return False, "daily_limit"

    await execute_with_busy_retry(
        session,
        sqlite_insert(AccountSafetyState)
        .values(account_id=account_id, state="ready", day_utc=today, attempts_today=0, updated_at=now)
        .on_conflict_do_nothing(index_elements=[AccountSafetyState.account_id]),
        op_name="safety-create",
    )
    reservation = (
        update(AccountSafetyState)
        .where(
            AccountSafetyState.account_id == account_id,
            AccountSafetyState.state == "ready",
            or_(AccountSafetyState.day_utc != today, AccountSafetyState.attempts_today < limit),
            # A concurrent manual stop or account restriction must win before this reservation.
            select(Account.id)
            .where(
                Account.id == account_id,
                Account.status == AccountStatus.ACTIVE,
                Account.is_spam_blocked.is_(False),
                or_(Account.flood_wait_until.is_(None), Account.flood_wait_until <= now),
            )
            .exists(),
        )
        .values(
            day_utc=today,
            attempts_today=case(
                (AccountSafetyState.day_utc == today, AccountSafetyState.attempts_today + 1),
                else_=1,
            ),
            source=source[:32],
            updated_at=now,
        )
    )
    if source.startswith("mailing"):
        reservation = reservation.where(
            select(ClientContactPermission.client_id).where(
                ClientContactPermission.client_id == int(client_id),
                ClientContactPermission.state == "opt_in",
            ).exists(),
            ~select(ClientClassCounter.id).where(
                ClientClassCounter.client_id == int(client_id),
                ClientClassCounter.class_key.in_(["bl", "stop"]),
                ClientClassCounter.count > 0,
            ).exists(),
        )
    result = await execute_with_busy_retry(session, reservation, op_name="safety-reserve")
    await commit_with_busy_retry(session, op_name="safety-reserve")
    if result.rowcount == 1:
        return True, "ok"
    state = await session.get(AccountSafetyState, account_id)
    if state is not None and state.state != "ready":
        return False, state.reason_code or "manual_pause"
    if source.startswith("mailing") and not await has_contact_permission(session, client_id):
        return False, "contact_permission"
    return False, "daily_limit"


async def pause_account(
    session: AsyncSession,
    account_id: int,
    *,
    reason_code: str,
    source: str,
    actor: str = "system",
    state: str = "review_required",
    resume_at: datetime | None = None,
) -> None:
    """Persist a stop before another module can reserve a send."""
    now = utcnow_naive()
    await execute_with_busy_retry(
        session,
        sqlite_insert(AccountSafetyState)
        .values(
            account_id=int(account_id), state=state, reason_code=reason_code[:64],
            source=source[:32], attempts_today=0, updated_at=now,
            resume_at=resume_at,
        )
        .on_conflict_do_update(
            index_elements=[AccountSafetyState.account_id],
            set_={"state": state, "reason_code": reason_code[:64],
                  "source": source[:32], "updated_at": now, "reviewed_at": None,
                  "reviewed_by": None, "resume_at": resume_at},
        ),
        op_name="safety-pause",
    )
    session.add(AccountSafetyEvent(
        account_id=int(account_id), event_type="paused", reason_code=reason_code[:64],
        source=source[:32], actor=actor[:120], created_at=now, resume_at=resume_at,
    ))
    await commit_with_busy_retry(session, op_name="safety-pause")


async def record_chat_cooldown(
    session: AsyncSession,
    account_id: int,
    *,
    peer_ref: str,
    resume_at: datetime,
    source: str,
) -> None:
    """Persist a chat-only slow mode without stopping unrelated conversations."""
    now = utcnow_naive()
    await execute_with_busy_retry(
        session,
        sqlite_insert(AccountChatCooldown)
        .values(
            account_id=int(account_id), peer_ref=normalize_peer_ref(peer_ref), resume_at=resume_at,
            reason_code="slow_mode", source=source[:32], updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=[AccountChatCooldown.account_id, AccountChatCooldown.peer_ref],
            set_={"resume_at": resume_at, "source": source[:32], "updated_at": now},
            where=AccountChatCooldown.resume_at < resume_at,
        ),
        op_name="safety-chat-cooldown",
    )
    session.add(AccountSafetyEvent(
        account_id=int(account_id), event_type="chat_paused", reason_code="slow_mode",
        source=source[:32], peer_ref=normalize_peer_ref(peer_ref), resume_at=resume_at,
        actor="system", created_at=now,
    ))
    await commit_with_busy_retry(session, op_name="safety-chat-cooldown")
