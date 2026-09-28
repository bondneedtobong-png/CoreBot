"""
Бизнес-роуты веб-панели поверх corebot.db:

  GET    /business/accounts                                — список аккаунтов
  POST   /business/accounts/{id}/mode                      — AI_ACTIVE | MANUAL
  GET    /business/accounts/{id}/dialogs                   — список диалогов
  GET    /business/accounts/{id}/dialogs/{peer}/messages   — лента сообщений
  POST   /business/accounts/{id}/dialogs/{peer}/send       — поставить ручную отправку
  DELETE /business/accounts/{id}/dialogs/{peer}            — удалить диалог
  POST   /business/cleanup                                 — массовая чистка
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from utils.time import utcnow_naive
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import String, and_, cast, delete, desc, func, insert, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, aliased

from control_plane.business.db import commit_sync, get_bot_db
from control_plane.business.schemas import (
    AccountCreate,
    AccountBulkMetadataIn,
    AccountBulkMetadataOut,
    AccountDetail,
    AccountListItem,
    AccountModeIn,
    AccountPatch,
    CleanupRequest,
    CleanupResult,
    DialogListItem,
    DialogMarkReadIn,
    DialogReadState,
    MessageOut,
    QueueBulkActionIn,
    QueueBulkActionOut,
    QueueListItem,
    SendMessageIn,
    SendMessageOut,
)
from control_plane.deps import get_current_user, require_operator_write
from control_plane.models import User
from database.sqlite_pragmas import run_sync_with_busy_retry
from database.models import (
    Account,
    AccountImportEvent,
    AccountStatus,
    Client,
    ClientClassCounter,
    ClientInteraction,
    ClientMailSession,
    DialogExportAudit,
    DialogReadCursor,
    DialogViewAudit,
    Group,
    MailingLog,
    Membership,
    NeuroActionLog,
    NeuroChatMessage,
    NeuroStopList,
    OutboundQueue,
    Proxy,
    account_groups,
)

router = APIRouter(prefix="/business", tags=["business"])


# ---------------------------- Accounts -----------------------------------


def _serialize_account(
    a: Account,
    *,
    dialogs_count: int = 0,
    pending_outbound: int = 0,
    last_dialog_at: Optional[datetime] = None,
) -> AccountListItem:
    status_val = None
    if a.status is not None:
        status_val = getattr(a.status, "value", str(a.status))
    membership_val = None
    if a.membership is not None:
        membership_val = getattr(a.membership, "value", str(a.membership))
    return AccountListItem(
        id=int(a.id),
        phone=a.phone,
        username=a.username,
        list_label=a.list_label,
        first_name=a.first_name,
        last_name=a.last_name,
        status=status_val,
        membership=membership_val,
        ai_mode=(a.ai_mode or "AI_ACTIVE").upper(),
        last_activity=a.last_activity,
        dialogs_count=int(dialogs_count),
        pending_outbound=int(pending_outbound),
        last_dialog_at=last_dialog_at,
    )


@router.get("/accounts", response_model=list[AccountListItem])
def list_accounts(
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    accounts = db.execute(select(Account).order_by(Account.id)).scalars().all()

    dialog_counts: dict[int, int] = dict(
        db.execute(
            select(
                NeuroChatMessage.account_id,
                func.count(func.distinct(NeuroChatMessage.peer_user_id)),
            ).group_by(NeuroChatMessage.account_id)
        ).all()
    )
    pending_counts: dict[int, int] = dict(
        db.execute(
            select(OutboundQueue.account_id, func.count(OutboundQueue.id))
            .where(OutboundQueue.status == "pending")
            .group_by(OutboundQueue.account_id)
        ).all()
    )

    # Время последнего сообщения в любом из диалогов аккаунта.
    # Учитываем как входящие/исходящие из NeuroChatMessage,
    # так и pending/sent ручные сообщения из OutboundQueue,
    # чтобы UI «Диалоги» сортировал список как мессенджер.
    last_neuro: dict[int, datetime] = dict(
        db.execute(
            select(NeuroChatMessage.account_id, func.max(NeuroChatMessage.created_at))
            .group_by(NeuroChatMessage.account_id)
        ).all()
    )
    last_outbound: dict[int, datetime] = dict(
        db.execute(
            select(OutboundQueue.account_id, func.max(OutboundQueue.created_at))
            .group_by(OutboundQueue.account_id)
        ).all()
    )

    def _max_dialog(account_id: int) -> Optional[datetime]:
        candidates = [
            v for v in (last_neuro.get(account_id), last_outbound.get(account_id)) if v
        ]
        return max(candidates) if candidates else None

    return [
        _serialize_account(
            a,
            dialogs_count=dialog_counts.get(a.id, 0),
            pending_outbound=pending_counts.get(a.id, 0),
            last_dialog_at=_max_dialog(a.id),
        )
        for a in accounts
    ]


@router.post("/accounts", response_model=AccountDetail, status_code=status.HTTP_201_CREATED)
def create_account(
    payload: AccountCreate,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(require_operator_write),
):
    phone = (payload.phone or "").strip()
    if not phone:
        raise HTTPException(status_code=400, detail="phone is required")
    existing_phone = db.execute(select(Account.id).where(Account.phone == phone)).first()
    if existing_phone:
        raise HTTPException(status_code=400, detail="phone already exists")

    session_name = (payload.session_name or "").strip()
    if not session_name:
        # Если аккаунт создаётся из веба без Tdata, создаём "пустой" session_name,
        # чтобы запись была валидной в БД; подключение воркера произойдёт только
        # когда в data/sessions появится реальная .session с тем же именем.
        cleaned_phone = "".join(ch for ch in phone if ch.isdigit()) or "acc"
        session_name = f"web_{cleaned_phone}_{int(utcnow_naive().timestamp())}"
    existing_session = db.execute(
        select(Account.id).where(Account.session_name == session_name)
    ).first()
    if existing_session:
        raise HTTPException(status_code=400, detail="session_name already exists")

    proxy_id = None
    if payload.proxy_id is not None and int(payload.proxy_id) > 0:
        proxy = db.get(Proxy, int(payload.proxy_id))
        if not proxy:
            raise HTTPException(status_code=400, detail="proxy not found")
        proxy_id = int(payload.proxy_id)

    try:
        status_value = AccountStatus(payload.status)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid status")
    try:
        membership_value = Membership(payload.membership)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid membership")

    row = Account(
        phone=phone,
        session_name=session_name,
        username=(payload.username or "").strip() or None,
        list_label=(payload.list_label or "").strip() or None,
        first_name=(payload.first_name or "").strip() or None,
        last_name=(payload.last_name or "").strip() or None,
        status=status_value,
        membership=membership_value,
        ai_mode=(payload.ai_mode or "MANUAL").upper(),
        proxy_id=proxy_id,
    )
    db.add(row)
    db.add(AccountImportEvent(account=row, source_kind="manual_web"))
    commit_sync(db)
    db.refresh(row)

    desired_groups = sorted({int(x) for x in (payload.group_ids or []) if int(x) > 0})
    if desired_groups:
        db.execute(
            insert(account_groups),
            [{"account_id": int(row.id), "group_id": gid} for gid in desired_groups],
        )
        commit_sync(db)

    return _serialize_account_detail(db, row)


@router.post("/accounts/{account_id}/mode", response_model=AccountListItem)
def set_account_mode(
    account_id: int,
    payload: AccountModeIn,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(require_operator_write),
):
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="account not found")
    new_mode = payload.mode.upper()
    db.execute(
        update(Account)
        .where(Account.id == account_id)
        .values(ai_mode=new_mode, updated_at=utcnow_naive())
    )
    commit_sync(db)
    db.refresh(account)
    return _serialize_account(account)


# ---------------------------- Account editor -----------------------------


def _account_groups_ids(db: Session, account_id: int) -> list[int]:
    rows = db.execute(
        select(account_groups.c.group_id).where(
            account_groups.c.account_id == account_id
        )
    ).all()
    return sorted(int(r[0]) for r in rows)


def _serialize_account_detail(db: Session, a: Account) -> AccountDetail:
    base = _serialize_account(a).model_dump()
    origin = db.execute(
        select(AccountImportEvent).where(AccountImportEvent.account_id == a.id)
        .order_by(AccountImportEvent.id).limit(1)
    ).scalar_one_or_none()
    proxy_label = None
    if a.proxy_id:
        p = db.get(Proxy, int(a.proxy_id))
        if p:
            proxy_label = f"{p.name} ({p.host}:{p.port})"
    base.update(
        created_at=a.created_at.replace(tzinfo=timezone.utc) if a.created_at else None,
        import_source=origin.source_kind if origin else None,
        imported_at=origin.created_at.replace(tzinfo=timezone.utc) if origin else None,
        bio=a.bio,
        tags=a.tags,
        daily_limit=int(a.daily_limit or 0),
        messages_today=int(a.messages_today or 0),
        messages_sent=int(a.messages_sent or 0),
        messages_failed=int(a.messages_failed or 0),
        warmup_enabled=bool(a.warmup_enabled),
        warmup_profile=a.warmup_profile,
        proxy_id=int(a.proxy_id) if a.proxy_id else None,
        proxy_label=proxy_label,
        flood_wait_until=a.flood_wait_until,
        is_spam_blocked=bool(a.is_spam_blocked),
        group_ids=_account_groups_ids(db, int(a.id)),
    )
    return AccountDetail(**base)


@router.post("/accounts/bulk-metadata", response_model=AccountBulkMetadataOut)
def bulk_account_metadata(
    payload: AccountBulkMetadataIn,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(require_operator_write),
):
    """Change local tags and account groups as one validated transaction."""
    account_ids = sorted(set(payload.account_ids))
    group_ids = sorted(set(payload.group_ids))
    tags: list[str] = []
    seen_tags: set[str] = set()
    for raw in payload.tags:
        if not isinstance(raw, str):
            raise HTTPException(status_code=400, detail="invalid tag")
        tag = raw.strip()
        if (not tag or len(tag) > 50 or "," in tag
                or any(ord(char) < 32 for char in tag)):
            raise HTTPException(status_code=400, detail="invalid tag")
        if tag.casefold() not in seen_tags:
            seen_tags.add(tag.casefold())
            tags.append(tag)
    if not tags and not group_ids:
        raise HTTPException(status_code=400, detail="select tags or groups")

    accounts = db.execute(
        select(Account).where(Account.id.in_(account_ids)).order_by(Account.id)
    ).scalars().all()
    if len(accounts) != len(account_ids):
        found = {account.id for account in accounts}
        raise HTTPException(status_code=404, detail={
            "missing_account_ids": sorted(set(account_ids) - found),
        })
    if group_ids:
        found_groups = set(db.execute(
            select(Group.id).where(Group.id.in_(group_ids))
        ).scalars())
        if len(found_groups) != len(group_ids):
            raise HTTPException(status_code=404, detail={
                "missing_group_ids": sorted(set(group_ids) - found_groups),
            })

    existing_memberships = set(db.execute(
        select(account_groups.c.account_id, account_groups.c.group_id).where(
            account_groups.c.account_id.in_(account_ids),
            account_groups.c.group_id.in_(group_ids),
        )
    ).all()) if group_ids else set()
    tags_changed_accounts = 0
    memberships_added = 0
    memberships_removed = 0
    changed_accounts: set[int] = set()
    for account in accounts:
        original = [tag.strip() for tag in (account.tags or "").split(",") if tag.strip()]
        if payload.action == "add":
            updated = list(original)
            current = {tag.casefold() for tag in updated}
            for tag in tags:
                if tag.casefold() not in current:
                    updated.append(tag)
                    current.add(tag.casefold())
        else:
            updated = [tag for tag in original if tag.casefold() not in seen_tags]
        if updated != original:
            normalized = "," + ",".join(updated) + "," if updated else ""
            if len(normalized) > 500:
                raise HTTPException(status_code=400, detail="account tag limit exceeded")
            account.tags = normalized
            tags_changed_accounts += 1
            changed_accounts.add(account.id)
        for group_id in group_ids:
            membership = (account.id, group_id)
            if payload.action == "add" and membership not in existing_memberships:
                db.execute(insert(account_groups).values(
                    account_id=account.id, group_id=group_id,
                ))
                memberships_added += 1
                changed_accounts.add(account.id)
            elif payload.action == "remove" and membership in existing_memberships:
                db.execute(delete(account_groups).where(
                    account_groups.c.account_id == account.id,
                    account_groups.c.group_id == group_id,
                ))
                memberships_removed += 1
                changed_accounts.add(account.id)
        if account.id in changed_accounts:
            account.updated_at = utcnow_naive()
    commit_sync(db, op_name="bulk-account-metadata")
    return AccountBulkMetadataOut(
        action=payload.action,
        account_ids=account_ids,
        accounts_changed=len(changed_accounts),
        tags_changed_accounts=tags_changed_accounts,
        group_memberships_added=memberships_added,
        group_memberships_removed=memberships_removed,
    )


@router.get("/accounts/{account_id}", response_model=AccountDetail)
def get_account_detail(
    account_id: int,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    a = db.get(Account, account_id)
    if not a:
        raise HTTPException(status_code=404, detail="account not found")
    return _serialize_account_detail(db, a)


@router.patch("/accounts/{account_id}", response_model=AccountDetail)
def patch_account(
    account_id: int,
    payload: AccountPatch,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(require_operator_write),
):
    a = db.get(Account, account_id)
    if not a:
        raise HTTPException(status_code=404, detail="account not found")

    if payload.list_label is not None:
        a.list_label = payload.list_label.strip() or None
    if payload.first_name is not None:
        a.first_name = payload.first_name.strip() or None
    if payload.last_name is not None:
        a.last_name = payload.last_name.strip() or None
    if payload.bio is not None:
        a.bio = payload.bio
    if payload.tags is not None:
        a.tags = payload.tags
    if payload.status is not None:
        try:
            a.status = AccountStatus(payload.status)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid status")
    if payload.membership is not None:
        try:
            a.membership = Membership(payload.membership)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid membership")
    if payload.daily_limit is not None:
        a.daily_limit = int(payload.daily_limit)
    if payload.warmup_enabled is not None:
        a.warmup_enabled = bool(payload.warmup_enabled)
    if payload.warmup_profile is not None:
        a.warmup_profile = payload.warmup_profile.strip() or None
    if payload.proxy_id is not None:
        if int(payload.proxy_id) <= 0:
            a.proxy_id = None
        else:
            if not db.get(Proxy, int(payload.proxy_id)):
                raise HTTPException(status_code=400, detail="proxy not found")
            a.proxy_id = int(payload.proxy_id)
    a.updated_at = utcnow_naive()
    commit_sync(db)
    db.refresh(a)

    # Группы — отдельная таблица.
    if payload.group_ids is not None:
        desired = sorted({int(x) for x in payload.group_ids if int(x) > 0})
        db.execute(
            delete(account_groups).where(account_groups.c.account_id == account_id)
        )
        if desired:
            db.execute(
                insert(account_groups),
                [{"account_id": account_id, "group_id": gid} for gid in desired],
            )
        commit_sync(db)

    return _serialize_account_detail(db, a)


@router.delete(
    "/accounts/{account_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_account(
    account_id: int,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(require_operator_write),
):
    a = db.get(Account, account_id)
    if not a:
        raise HTTPException(status_code=404, detail="account not found")
    # Зависимые таблицы. CASCADE-FK у нас не везде, поэтому чистим явно.
    # Иначе SQLite (с PRAGMA foreign_keys=ON) ругнётся на FK constraint.
    db.execute(delete(account_groups).where(account_groups.c.account_id == account_id))
    db.execute(delete(NeuroChatMessage).where(NeuroChatMessage.account_id == account_id))
    db.execute(delete(DialogExportAudit).where(DialogExportAudit.account_id == account_id))
    db.execute(delete(DialogViewAudit).where(DialogViewAudit.account_id == account_id))
    db.execute(delete(OutboundQueue).where(OutboundQueue.account_id == account_id))
    db.execute(delete(MailingLog).where(MailingLog.account_id == account_id))
    db.execute(delete(NeuroActionLog).where(NeuroActionLog.account_id == account_id))
    db.execute(delete(NeuroStopList).where(NeuroStopList.account_id == account_id))
    db.execute(delete(ClientMailSession).where(ClientMailSession.account_id == account_id))
    # ClientInteraction.account_id — SET NULL по FK, обнуляем явно для совместимости.
    db.execute(
        update(ClientInteraction)
        .where(ClientInteraction.account_id == account_id)
        .values(account_id=None)
    )
    db.delete(a)
    commit_sync(db)


# ---------------------------- Dialogs ------------------------------------


def _ensure_account(db: Session, account_id: int) -> Account:
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="account not found")
    return account


def _queue_item_to_out(db: Session, row: OutboundQueue) -> QueueListItem:
    account = db.get(Account, int(row.account_id))
    client_username = None
    if getattr(row, "client_id", None):
        cl = db.get(Client, int(row.client_id))
        client_username = cl.username if cl else None
    title = (
        (account.list_label or account.username or account.phone or f"#{row.account_id}")
        if account
        else f"#{row.account_id}"
    )
    return QueueListItem(
        queue_id=int(row.id),
        account_id=int(row.account_id),
        account_title=title,
        peer_user_id=int(row.peer_user_id),
        client_id=int(row.client_id) if row.client_id else None,
        client_username=client_username,
        text=row.text or "",
        status=row.status or "pending",
        error=row.error,
        attempts=int(getattr(row, "attempts", 0) or 0),
        requested_by=row.requested_by,
        created_at=row.created_at or utcnow_naive(),
        next_attempt_at=row.next_attempt_at,
        sent_at=row.sent_at,
    )


@router.get("/queue", response_model=list[QueueListItem])
def list_outbound_queue(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    account_id: Optional[int] = Query(default=None),
    peer_user_id: Optional[int] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    stmt = select(OutboundQueue)
    if status_filter:
        stmt = stmt.where(OutboundQueue.status == status_filter.strip().lower())
    if account_id is not None:
        stmt = stmt.where(OutboundQueue.account_id == int(account_id))
    if peer_user_id is not None:
        stmt = stmt.where(OutboundQueue.peer_user_id == int(peer_user_id))
    rows = (
        db.execute(stmt.order_by(OutboundQueue.id.desc()).limit(limit).offset(offset))
        .scalars()
        .all()
    )
    return [_queue_item_to_out(db, r) for r in rows]


@router.post("/queue/bulk", response_model=QueueBulkActionOut)
def queue_bulk_action(
    payload: QueueBulkActionIn,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(require_operator_write),
):
    qids = sorted({int(x) for x in payload.queue_ids if int(x) > 0})
    if not qids:
        raise HTTPException(status_code=400, detail="queue_ids is empty")
    rows = (
        db.execute(select(OutboundQueue).where(OutboundQueue.id.in_(qids)))
        .scalars()
        .all()
    )
    by_id = {int(r.id): r for r in rows}
    updated = 0
    skipped = 0
    for qid in qids:
        row = by_id.get(qid)
        if not row:
            skipped += 1
            continue
        if payload.action == "retry":
            if row.status not in ("failed", "cancelled", "uncertain"):
                skipped += 1
                continue
            result = db.execute(
                update(OutboundQueue)
                .where(
                    OutboundQueue.id == qid,
                    OutboundQueue.status.in_(("failed", "cancelled", "uncertain")),
                )
                .values(
                    status="pending",
                    error=None,
                    attempts=0,
                    next_attempt_at=None,
                    sent_at=None,
                )
            )
            changed = result.rowcount or 0
            updated += changed
            skipped += 1 - changed
            continue
        if payload.action == "cancel":
            if row.status not in ("pending", "failed"):
                skipped += 1
                continue
            result = db.execute(
                update(OutboundQueue)
                .where(OutboundQueue.id == qid, OutboundQueue.status.in_(("pending", "failed")))
                .values(status="cancelled", next_attempt_at=None)
            )
            changed = result.rowcount or 0
            updated += changed
            skipped += 1 - changed
            continue
        skipped += 1
    commit_sync(db)
    return QueueBulkActionOut(
        requested=len(qids),
        updated=updated,
        skipped=skipped,
    )


@router.get(
    "/accounts/{account_id}/dialogs", response_model=list[DialogListItem]
)
def list_dialogs(
    account_id: int,
    limit: int = Query(default=200, ge=1, le=1000),
    q: str = Query(default="", max_length=100),
    waiting_only: bool = Query(default=False),
    unread_only: bool = Query(default=False),
    db: Session = Depends(get_bot_db),
    user: User = Depends(get_current_user),
):
    _ensure_account(db, account_id)

    last_msg_subq = (
        select(
            NeuroChatMessage.peer_user_id.label("peer_user_id"),
            func.max(NeuroChatMessage.id).label("last_id"),
            func.count(NeuroChatMessage.id).label("messages_count"),
        )
        .where(NeuroChatMessage.account_id == account_id)
        .group_by(NeuroChatMessage.peer_user_id)
        .subquery()
    )

    queued_reply = (
        select(OutboundQueue.id)
        .where(
            OutboundQueue.account_id == account_id,
            OutboundQueue.peer_user_id == last_msg_subq.c.peer_user_id,
            OutboundQueue.status.in_(("pending", "sending", "uncertain")),
        )
        .correlate(last_msg_subq)
        .exists()
    )

    incoming = aliased(NeuroChatMessage)
    unread_count = (
        select(func.count(incoming.id))
        .where(
            incoming.account_id == account_id,
            incoming.peer_user_id == last_msg_subq.c.peer_user_id,
            incoming.role == "user",
            incoming.id > func.coalesce(DialogReadCursor.last_read_message_id, 0),
        )
        .correlate(last_msg_subq, DialogReadCursor)
        .scalar_subquery()
    )

    query = (
        select(
            last_msg_subq.c.peer_user_id,
            last_msg_subq.c.messages_count,
            NeuroChatMessage.id,
            NeuroChatMessage.content,
            NeuroChatMessage.created_at,
            NeuroChatMessage.role,
            Client.id,
            Client.username,
            queued_reply.label("has_queued_reply"),
            unread_count.label("unread_count"),
        )
        .join(NeuroChatMessage, NeuroChatMessage.id == last_msg_subq.c.last_id)
        .outerjoin(
            DialogReadCursor,
            and_(
                DialogReadCursor.operator_user_id == user.id,
                DialogReadCursor.account_id == account_id,
                DialogReadCursor.peer_user_id == last_msg_subq.c.peer_user_id,
            ),
        )
        .join(
            Client,
            Client.telegram_user_id == last_msg_subq.c.peer_user_id,
            isouter=True,
        )
        .order_by(desc(NeuroChatMessage.created_at))
        .limit(limit)
    )
    if waiting_only:
        query = query.where(NeuroChatMessage.role == "user", ~queued_reply)
    if unread_only:
        query = query.where(unread_count > 0)
    search = q.strip().removeprefix("@").lower()
    if search:
        pattern = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        query = query.where(or_(
            cast(last_msg_subq.c.peer_user_id, String).like(pattern, escape="\\"),
            func.lower(Client.username).like(pattern, escape="\\"),
            func.lower(NeuroChatMessage.content).like(pattern, escape="\\"),
        ))
    rows = db.execute(query).all()

    items: list[DialogListItem] = []
    for (
        peer_user_id,
        messages_count,
        _msg_id,
        content,
        created_at,
        role,
        client_id,
        client_username,
        has_queued_reply,
        unread_count_value,
    ) in rows:
        text_preview = (content or "")[:160]
        items.append(
            DialogListItem(
                account_id=account_id,
                peer_user_id=int(peer_user_id),
                client_id=int(client_id) if client_id else None,
                client_username=client_username,
                last_message=text_preview,
                last_message_at=created_at,
                last_role=role,
                messages_count=int(messages_count or 0),
                waiting_for_reply=role == "user" and not has_queued_reply,
                unread_count=int(unread_count_value or 0),
            )
        )
    return items


@router.get("/dialogs", response_model=list[DialogListItem])
def list_global_dialogs(
    response: Response,
    limit: int = Query(default=200, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=10000),
    q: str = Query(default="", max_length=100),
    waiting_only: bool = Query(default=False),
    unread_only: bool = Query(default=False),
    db: Session = Depends(get_bot_db),
    user: User = Depends(get_current_user),
):
    """Read-only inbox across accounts, with operator-specific unread state."""
    response.headers["Cache-Control"] = "no-store"
    last_msg_subq = (
        select(
            NeuroChatMessage.account_id.label("account_id"),
            NeuroChatMessage.peer_user_id.label("peer_user_id"),
            NeuroChatMessage.id.label("last_id"),
            func.count(NeuroChatMessage.id).over(
                partition_by=(NeuroChatMessage.account_id, NeuroChatMessage.peer_user_id),
            ).label("messages_count"),
            func.row_number().over(
                partition_by=(NeuroChatMessage.account_id, NeuroChatMessage.peer_user_id),
                order_by=(NeuroChatMessage.created_at.desc(), NeuroChatMessage.id.desc()),
            ).label("latest_rank"),
        )
        .subquery()
    )
    queued_reply = (
        select(OutboundQueue.id)
        .where(
            OutboundQueue.account_id == last_msg_subq.c.account_id,
            OutboundQueue.peer_user_id == last_msg_subq.c.peer_user_id,
            OutboundQueue.status.in_(("pending", "sending", "uncertain")),
        )
        .correlate(last_msg_subq)
        .exists()
    )
    incoming = aliased(NeuroChatMessage)
    unread_count = (
        select(func.count(incoming.id))
        .where(
            incoming.account_id == last_msg_subq.c.account_id,
            incoming.peer_user_id == last_msg_subq.c.peer_user_id,
            incoming.role == "user",
            incoming.id > func.coalesce(DialogReadCursor.last_read_message_id, 0),
        )
        .correlate(last_msg_subq, DialogReadCursor)
        .scalar_subquery()
    )
    query = (
        select(
            last_msg_subq.c.account_id,
            last_msg_subq.c.peer_user_id,
            last_msg_subq.c.messages_count,
            NeuroChatMessage.content,
            NeuroChatMessage.created_at,
            NeuroChatMessage.role,
            Client.id,
            Client.username,
            queued_reply.label("has_queued_reply"),
            unread_count.label("unread_count"),
        )
        .join(NeuroChatMessage, NeuroChatMessage.id == last_msg_subq.c.last_id)
        .outerjoin(
            DialogReadCursor,
            and_(
                DialogReadCursor.operator_user_id == user.id,
                DialogReadCursor.account_id == last_msg_subq.c.account_id,
                DialogReadCursor.peer_user_id == last_msg_subq.c.peer_user_id,
            ),
        )
        .outerjoin(Client, Client.telegram_user_id == last_msg_subq.c.peer_user_id)
        .where(last_msg_subq.c.latest_rank == 1)
    )
    if waiting_only:
        query = query.where(NeuroChatMessage.role == "user", ~queued_reply)
    if unread_only:
        query = query.where(unread_count > 0)
    search = q.strip().removeprefix("@").lower()
    if search:
        pattern = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        matching_message = (
            select(incoming.id)
            .where(
                incoming.account_id == last_msg_subq.c.account_id,
                incoming.peer_user_id == last_msg_subq.c.peer_user_id,
                func.lower(incoming.content).like(pattern, escape="\\"),
            )
            .correlate(last_msg_subq)
            .exists()
        )
        query = query.where(or_(
            cast(last_msg_subq.c.peer_user_id, String).like(pattern, escape="\\"),
            func.lower(Client.username).like(pattern, escape="\\"),
            matching_message,
        ))
    rows = db.execute(
        query.order_by(
            NeuroChatMessage.created_at.desc(), NeuroChatMessage.id.desc(),
        ).limit(limit).offset(offset)
    ).all()
    return [
        DialogListItem(
            account_id=int(account_id),
            peer_user_id=int(peer_user_id),
            client_id=int(client_id) if client_id else None,
            client_username=client_username,
            last_message=(content or "")[:160],
            last_message_at=created_at,
            last_role=role,
            messages_count=int(messages_count),
            waiting_for_reply=role == "user" and not has_queued_reply,
            unread_count=int(unread_count_value or 0),
        )
        for (
            account_id, peer_user_id, messages_count, content, created_at, role,
            client_id, client_username, has_queued_reply, unread_count_value,
        ) in rows
    ]


@router.post(
    "/accounts/{account_id}/dialogs/{peer_user_id}/read",
    response_model=DialogReadState,
)
def mark_dialog_read(
    account_id: int,
    peer_user_id: int,
    payload: Optional[DialogMarkReadIn] = None,
    db: Session = Depends(get_bot_db),
    user: User = Depends(require_operator_write),
):
    _ensure_account(db, account_id)
    dialog_exists = db.execute(
        select(NeuroChatMessage.id)
        .where(
            NeuroChatMessage.account_id == account_id,
            NeuroChatMessage.peer_user_id == peer_user_id,
        )
        .limit(1)
    ).first()
    if dialog_exists is None:
        raise HTTPException(status_code=404, detail="dialog not found")

    requested_id = payload.through_message_id if payload else None
    if requested_id is not None and requested_id > 0:
        target = db.execute(
            select(NeuroChatMessage.id).where(
                NeuroChatMessage.id == requested_id,
                NeuroChatMessage.account_id == account_id,
                NeuroChatMessage.peer_user_id == peer_user_id,
                NeuroChatMessage.role == "user",
            )
        ).scalar_one_or_none()
        if target is None:
            raise HTTPException(status_code=400, detail="through_message_id is not an incoming message in this dialog")
        cutoff = int(target)
    elif requested_id == 0:
        cutoff = 0
    else:
        cutoff = int(db.execute(
            select(func.max(NeuroChatMessage.id)).where(
                NeuroChatMessage.account_id == account_id,
                NeuroChatMessage.peer_user_id == peer_user_id,
                NeuroChatMessage.role == "user",
            )
        ).scalar_one() or 0)

    now = utcnow_naive()
    upsert = (
        sqlite_insert(DialogReadCursor)
        .values(
            operator_user_id=int(user.id), account_id=account_id,
            peer_user_id=peer_user_id, last_read_message_id=cutoff, updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=[
                DialogReadCursor.operator_user_id,
                DialogReadCursor.account_id,
                DialogReadCursor.peer_user_id,
            ],
            set_={
                "last_read_message_id": func.max(DialogReadCursor.last_read_message_id, cutoff),
                "updated_at": now,
            },
        )
    )
    run_sync_with_busy_retry(lambda: db.execute(upsert), op_name="dialog-mark-read")
    commit_sync(db, op_name="dialog-mark-read")
    cursor = db.get(DialogReadCursor, (int(user.id), account_id, peer_user_id))
    unread_count = db.execute(
        select(func.count(NeuroChatMessage.id)).where(
            NeuroChatMessage.account_id == account_id,
            NeuroChatMessage.peer_user_id == peer_user_id,
            NeuroChatMessage.role == "user",
            NeuroChatMessage.id > cursor.last_read_message_id,
        )
    ).scalar_one()
    return DialogReadState(
        account_id=account_id,
        peer_user_id=peer_user_id,
        last_read_message_id=int(cursor.last_read_message_id),
        unread_count=int(unread_count),
    )


@router.get(
    "/accounts/{account_id}/dialogs/{peer_user_id}/messages",
    response_model=list[MessageOut],
)
def list_messages(
    account_id: int,
    peer_user_id: int,
    after_id: Optional[int] = Query(default=None, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_bot_db),
    user: User = Depends(get_current_user),
):
    _ensure_account(db, account_id)
    stmt = (
        select(NeuroChatMessage)
        .where(
            NeuroChatMessage.account_id == account_id,
            NeuroChatMessage.peer_user_id == peer_user_id,
        )
        .order_by(NeuroChatMessage.id.desc())
        .limit(limit)
    )
    if after_id is not None:
        stmt = (
            select(NeuroChatMessage)
            .where(
                NeuroChatMessage.account_id == account_id,
                NeuroChatMessage.peer_user_id == peer_user_id,
                NeuroChatMessage.id > after_id,
            )
            .order_by(NeuroChatMessage.id.asc())
            .limit(limit)
        )
    rows = list(db.execute(stmt).scalars().all())
    if after_id is None:
        rows = list(reversed(rows))

    items: list[MessageOut] = [
        MessageOut(
            id=int(r.id),
            role=r.role,
            content=r.content,
            created_at=r.created_at,
            source="neuro",
        )
        for r in rows
    ]

    # При первой выборке (after_id is None) дополнительно подмешиваем строки
    # очереди (pending / sending / uncertain / failed / cancelled), чтобы пользователь
    # видел, что его ручные сообщения «в работе», а не пропали.
    if after_id is None:
        queue_rows = (
            db.execute(
                select(OutboundQueue)
                .where(
                    OutboundQueue.account_id == account_id,
                    OutboundQueue.peer_user_id == peer_user_id,
                    OutboundQueue.status.in_(
                        ["pending", "sending", "uncertain", "failed", "cancelled"]
                    ),
                )
                .order_by(OutboundQueue.created_at.asc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
        for q in queue_rows:
            items.append(
                MessageOut(
                    id=-int(q.id),  # отрицательный id — чтобы не конфликтовал с neuro
                    role="assistant",
                    content=q.text or "",
                    created_at=q.created_at or utcnow_naive(),
                    source="queue",
                    queue_status=q.status,
                    queue_error=q.error,
                    queue_attempts=int(getattr(q, "attempts", 0) or 0),
                    queue_id=int(q.id),
                )
            )
        items.sort(key=lambda m: (m.created_at, m.id))

    now = utcnow_naive()
    recent_view = db.execute(
        select(DialogViewAudit.id).where(
            DialogViewAudit.operator_user_id == int(user.id),
            DialogViewAudit.account_id == account_id,
            DialogViewAudit.peer_user_id == peer_user_id,
            DialogViewAudit.created_at >= now - timedelta(minutes=15),
        ).limit(1)
    ).scalar_one_or_none()
    if recent_view is None:
        db.add(DialogViewAudit(
            operator_user_id=int(user.id), account_id=account_id,
            peer_user_id=peer_user_id, created_at=now,
        ))
        commit_sync(db, op_name="dialog-view-audit")
    return items


@router.get("/accounts/{account_id}/dialogs/{peer_user_id}/export")
def export_dialog(
    account_id: int,
    peer_user_id: int,
    format: str = Query(default="csv", pattern="^(csv|txt)$"),
    db: Session = Depends(get_bot_db),
    user: User = Depends(require_operator_write),
):
    """Stream stored messages in keyset pages; pending outbound text is excluded."""
    _ensure_account(db, account_id)
    message_count, max_id = db.execute(
        select(func.count(), func.max(NeuroChatMessage.id))
        .where(
            NeuroChatMessage.account_id == account_id,
            NeuroChatMessage.peer_user_id == peer_user_id,
        )
    ).one()
    max_id = int(max_id or 0)
    engine = db.get_bind()

    def timestamp(value: datetime) -> str:
        aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return aware.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    db.add(DialogExportAudit(
        operator_user_id=int(user.id), account_id=account_id,
        peer_user_id=peer_user_id, export_format=format, message_count=message_count,
    ))
    commit_sync(db, op_name="dialog-export-audit")

    def chunks():
        if format == "csv":
            yield "\ufeffmessage_id,created_at_utc,role,content\r\n"
        last_id = 0
        while last_id < max_id:
            with Session(bind=engine) as page_db:
                rows = page_db.execute(
                    select(
                        NeuroChatMessage.id, NeuroChatMessage.created_at,
                        NeuroChatMessage.role, NeuroChatMessage.content,
                    ).where(
                        NeuroChatMessage.account_id == account_id,
                        NeuroChatMessage.peer_user_id == peer_user_id,
                        NeuroChatMessage.id > last_id,
                        NeuroChatMessage.id <= max_id,
                    ).order_by(NeuroChatMessage.id).limit(500)
                ).all()
            if not rows:
                break
            out = io.StringIO(newline="")
            writer = csv.writer(out) if format == "csv" else None
            for message_id, created_at, role, raw_content in rows:
                content = raw_content or ""
                if writer:
                    if content.lstrip().startswith(("=", "+", "-", "@")):
                        content = "'" + content
                    writer.writerow([message_id, timestamp(created_at), role, content])
                else:
                    content = content.replace("\r", "\\r").replace("\n", "\\n")
                    out.write(f"[{timestamp(created_at)}] {role} #{message_id}: {content}\n")
                last_id = message_id
            yield out.getvalue()

    media_type = "text/csv; charset=utf-8" if format == "csv" else "text/plain; charset=utf-8"
    filename = f"account-{account_id}-dialog-{peer_user_id}.{format}"
    return StreamingResponse(
        chunks(), media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"',
                 "Cache-Control": "no-store"},
    )


@router.get("/accounts/{account_id}/dialogs/{peer_user_id}/exports")
def list_dialog_exports(
    account_id: int,
    peer_user_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_bot_db),
    _user: User = Depends(require_operator_write),
):
    _ensure_account(db, account_id)
    rows = db.execute(
        select(DialogExportAudit).where(
            DialogExportAudit.account_id == account_id,
            DialogExportAudit.peer_user_id == peer_user_id,
        ).order_by(DialogExportAudit.id.desc()).limit(limit)
    ).scalars().all()
    return [{
        "id": row.id, "operator_user_id": row.operator_user_id,
        "format": row.export_format, "message_count": row.message_count,
        "created_at": row.created_at,
    } for row in rows]


@router.get("/accounts/{account_id}/dialogs/{peer_user_id}/views")
def list_dialog_views(
    account_id: int,
    peer_user_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_bot_db),
    _user: User = Depends(require_operator_write),
):
    _ensure_account(db, account_id)
    rows = db.execute(
        select(DialogViewAudit).where(
            DialogViewAudit.account_id == account_id,
            DialogViewAudit.peer_user_id == peer_user_id,
        ).order_by(DialogViewAudit.id.desc()).limit(limit)
    ).scalars().all()
    return [{
        "id": row.id, "operator_user_id": row.operator_user_id,
        "created_at": row.created_at,
    } for row in rows]


@router.post(
    "/accounts/{account_id}/dialogs/{peer_user_id}/send",
    response_model=SendMessageOut,
    status_code=status.HTTP_201_CREATED,
)
def enqueue_manual_send(
    account_id: int,
    peer_user_id: int,
    payload: SendMessageIn,
    db: Session = Depends(get_bot_db),
    user: User = Depends(require_operator_write),
):
    _ensure_account(db, account_id)
    text_clean = (payload.text or "").strip()
    if not text_clean:
        raise HTTPException(status_code=400, detail="empty text")

    client_id: Optional[int] = None
    client_row = db.execute(
        select(Client.id).where(Client.telegram_user_id == peer_user_id)
    ).first()
    if client_row:
        client_id = int(client_row[0])

    row = OutboundQueue(
        account_id=account_id,
        peer_user_id=peer_user_id,
        client_id=client_id,
        text=text_clean,
        status="pending",
        requested_by=getattr(user, "username", None),
    )
    db.add(row)
    run_sync_with_busy_retry(db.commit, op_name="outbound-enqueue")
    db.refresh(row)
    return SendMessageOut(
        queue_id=int(row.id), status=row.status, enqueued_at=row.created_at
    )


@router.post(
    "/queue/{queue_id}/retry",
    response_model=SendMessageOut,
)
def retry_queue_item(
    queue_id: int,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(require_operator_write),
):
    row = db.get(OutboundQueue, queue_id)
    if not row:
        raise HTTPException(status_code=404, detail="queue item not found")
    if row.status not in ("failed", "cancelled", "uncertain"):
        raise HTTPException(
            status_code=400,
            detail=f"can retry only failed/cancelled/uncertain, current status={row.status}",
        )
    result = db.execute(
        update(OutboundQueue)
        .where(
            OutboundQueue.id == queue_id,
            OutboundQueue.status.in_(("failed", "cancelled", "uncertain")),
        )
        .values(
            status="pending",
            error=None,
            attempts=0,
            next_attempt_at=None,
            sent_at=None,
        )
    )
    if not result.rowcount:
        db.rollback()
        raise HTTPException(status_code=409, detail="queue status changed; reload and retry")
    run_sync_with_busy_retry(db.commit, op_name="outbound-retry")
    db.refresh(row)
    return SendMessageOut(
        queue_id=int(row.id), status=row.status, enqueued_at=row.created_at
    )


@router.post(
    "/queue/{queue_id}/cancel",
    status_code=status.HTTP_204_NO_CONTENT,
)
def cancel_queue_item(
    queue_id: int,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(require_operator_write),
):
    row = db.get(OutboundQueue, queue_id)
    if not row:
        raise HTTPException(status_code=404, detail="queue item not found")
    if row.status not in ("pending", "failed"):
        raise HTTPException(
            status_code=400,
            detail=f"can cancel only pending/failed, current status={row.status}",
        )
    result = db.execute(
        update(OutboundQueue)
        .where(OutboundQueue.id == queue_id, OutboundQueue.status.in_(("pending", "failed")))
        .values(status="cancelled", next_attempt_at=None)
    )
    if not result.rowcount:
        db.rollback()
        raise HTTPException(status_code=409, detail="queue status changed; reload and retry")
    run_sync_with_busy_retry(db.commit, op_name="outbound-cancel")


@router.delete(
    "/accounts/{account_id}/dialogs/{peer_user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_dialog(
    account_id: int,
    peer_user_id: int,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(require_operator_write),
):
    _ensure_account(db, account_id)
    db.execute(
        delete(NeuroChatMessage).where(
            NeuroChatMessage.account_id == account_id,
            NeuroChatMessage.peer_user_id == peer_user_id,
        )
    )
    db.execute(
        delete(OutboundQueue).where(
            OutboundQueue.account_id == account_id,
            OutboundQueue.peer_user_id == peer_user_id,
            OutboundQueue.status.in_(["sent", "failed", "cancelled"]),
        )
    )
    commit_sync(db)


# ---------------------------- Cleanup ------------------------------------


@router.post("/cleanup", response_model=CleanupResult)
def cleanup_dialogs(
    payload: CleanupRequest,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(require_operator_write),
):
    if not any(
        [
            payload.account_id is not None,
            payload.peer_user_id is not None,
            payload.older_than_days is not None,
            payload.classes,
        ]
    ):
        raise HTTPException(
            status_code=400,
            detail="provide at least one filter: account_id/peer_user_id/older_than_days/classes",
        )

    msg_filters = []
    interaction_filters = []

    if payload.account_id is not None:
        msg_filters.append(NeuroChatMessage.account_id == int(payload.account_id))
        interaction_filters.append(
            ClientInteraction.account_id == int(payload.account_id)
        )
    if payload.peer_user_id is not None:
        msg_filters.append(NeuroChatMessage.peer_user_id == int(payload.peer_user_id))

    if payload.older_than_days is not None:
        threshold = utcnow_naive() - timedelta(days=int(payload.older_than_days))
        msg_filters.append(NeuroChatMessage.created_at < threshold)
        interaction_filters.append(ClientInteraction.created_at < threshold)

    target_client_ids: Optional[list[int]] = None
    if payload.classes:
        normalized = [str(c).strip().lstrip("{").rstrip("}").lower() for c in payload.classes]
        normalized = [c for c in normalized if c]
        if not normalized:
            raise HTTPException(status_code=400, detail="empty classes filter")
        rows = db.execute(
            select(ClientClassCounter.client_id)
            .where(
                ClientClassCounter.class_key.in_(normalized),
                ClientClassCounter.count > 0,
            )
            .distinct()
        ).all()
        target_client_ids = [int(r[0]) for r in rows]
        if not target_client_ids:
            return CleanupResult(
                dialogs_deleted=0,
                messages_deleted=0,
                interactions_deleted=0,
                dry_run=payload.dry_run,
            )
        peer_rows = db.execute(
            select(Client.telegram_user_id)
            .where(
                Client.id.in_(target_client_ids),
                Client.telegram_user_id.is_not(None),
            )
        ).all()
        peer_ids = [int(r[0]) for r in peer_rows]
        if peer_ids:
            msg_filters.append(NeuroChatMessage.peer_user_id.in_(peer_ids))
        else:
            msg_filters.append(NeuroChatMessage.peer_user_id == -1)
        interaction_filters.append(ClientInteraction.client_id.in_(target_client_ids))

    msg_count_q = select(func.count(NeuroChatMessage.id))
    if msg_filters:
        msg_count_q = msg_count_q.where(and_(*msg_filters))
    messages_to_delete = int(db.execute(msg_count_q).scalar_one() or 0)

    dialogs_q = select(
        func.count(func.distinct(NeuroChatMessage.account_id * 1000000 + NeuroChatMessage.peer_user_id))
    )
    if msg_filters:
        dialogs_q = dialogs_q.where(and_(*msg_filters))
    dialogs_to_delete = int(db.execute(dialogs_q).scalar_one() or 0)

    inter_count_q = select(func.count(ClientInteraction.id))
    if interaction_filters:
        inter_count_q = inter_count_q.where(and_(*interaction_filters))
    interactions_to_delete = int(db.execute(inter_count_q).scalar_one() or 0)

    if payload.dry_run:
        return CleanupResult(
            dialogs_deleted=dialogs_to_delete,
            messages_deleted=messages_to_delete,
            interactions_deleted=interactions_to_delete,
            dry_run=True,
        )

    batch_size = max(1, int(payload.batch_size))
    deleted_messages = 0
    while True:
        ids_stmt = select(NeuroChatMessage.id)
        if msg_filters:
            ids_stmt = ids_stmt.where(and_(*msg_filters))
        ids_stmt = ids_stmt.limit(batch_size)
        ids_chunk = [int(r[0]) for r in db.execute(ids_stmt).all()]
        if not ids_chunk:
            break
        result = db.execute(
            delete(NeuroChatMessage).where(NeuroChatMessage.id.in_(ids_chunk))
        )
        commit_sync(db)
        deleted_messages += int(result.rowcount or 0)
        if len(ids_chunk) < batch_size:
            break

    deleted_interactions = 0
    if interaction_filters:
        while True:
            ids_stmt = select(ClientInteraction.id).where(and_(*interaction_filters)).limit(
                batch_size
            )
            ids_chunk = [int(r[0]) for r in db.execute(ids_stmt).all()]
            if not ids_chunk:
                break
            result = db.execute(
                delete(ClientInteraction).where(ClientInteraction.id.in_(ids_chunk))
            )
            commit_sync(db)
            deleted_interactions += int(result.rowcount or 0)
            if len(ids_chunk) < batch_size:
                break

    return CleanupResult(
        dialogs_deleted=dialogs_to_delete,
        messages_deleted=deleted_messages,
        interactions_deleted=deleted_interactions,
        dry_run=False,
    )
