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

from datetime import datetime, timedelta
from utils.time import utcnow_naive
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, delete, desc, func, insert, select, update
from sqlalchemy.orm import Session

from control_plane.business.db import get_bot_db
from control_plane.business.schemas import (
    AccountCreate,
    AccountDetail,
    AccountListItem,
    AccountModeIn,
    AccountPatch,
    CleanupRequest,
    CleanupResult,
    DialogListItem,
    MessageOut,
    QueueBulkActionIn,
    QueueBulkActionOut,
    QueueListItem,
    SendMessageIn,
    SendMessageOut,
)
from control_plane.deps import get_current_user, require_operator_write
from control_plane.models import User
from database.models import (
    Account,
    AccountStatus,
    Client,
    ClientClassCounter,
    ClientInteraction,
    ClientMailSession,
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
    db.commit()
    db.refresh(row)

    desired_groups = sorted({int(x) for x in (payload.group_ids or []) if int(x) > 0})
    if desired_groups:
        db.execute(
            insert(account_groups),
            [{"account_id": int(row.id), "group_id": gid} for gid in desired_groups],
        )
        db.commit()

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
    db.commit()
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
    proxy_label = None
    if a.proxy_id:
        p = db.get(Proxy, int(a.proxy_id))
        if p:
            proxy_label = f"{p.name} ({p.host}:{p.port})"
    base.update(
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
    db.commit()
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
        db.commit()

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
    db.commit()


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
            if row.status not in ("failed", "cancelled"):
                skipped += 1
                continue
            db.execute(
                update(OutboundQueue)
                .where(OutboundQueue.id == qid)
                .values(
                    status="pending",
                    error=None,
                    attempts=0,
                    next_attempt_at=None,
                    sent_at=None,
                )
            )
            updated += 1
            continue
        if payload.action == "cancel":
            if row.status not in ("pending", "failed"):
                skipped += 1
                continue
            db.execute(
                update(OutboundQueue)
                .where(OutboundQueue.id == qid)
                .values(status="cancelled", next_attempt_at=None)
            )
            updated += 1
            continue
        skipped += 1
    db.commit()
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
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
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

    rows = db.execute(
        select(
            last_msg_subq.c.peer_user_id,
            last_msg_subq.c.messages_count,
            NeuroChatMessage.id,
            NeuroChatMessage.content,
            NeuroChatMessage.created_at,
            NeuroChatMessage.role,
            Client.id,
            Client.username,
        )
        .join(NeuroChatMessage, NeuroChatMessage.id == last_msg_subq.c.last_id)
        .join(
            Client,
            Client.telegram_user_id == last_msg_subq.c.peer_user_id,
            isouter=True,
        )
        .order_by(desc(NeuroChatMessage.created_at))
        .limit(limit)
    ).all()

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
            )
        )
    return items


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
    _user: User = Depends(get_current_user),
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
    # очереди (pending / sending / failed / cancelled), чтобы пользователь
    # видел, что его ручные сообщения «в работе», а не пропали.
    if after_id is None:
        queue_rows = (
            db.execute(
                select(OutboundQueue)
                .where(
                    OutboundQueue.account_id == account_id,
                    OutboundQueue.peer_user_id == peer_user_id,
                    OutboundQueue.status.in_(
                        ["pending", "sending", "failed", "cancelled"]
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

    return items


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
    db.commit()
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
    if row.status not in ("failed", "cancelled"):
        raise HTTPException(
            status_code=400,
            detail=f"can retry only failed/cancelled, current status={row.status}",
        )
    db.execute(
        update(OutboundQueue)
        .where(OutboundQueue.id == queue_id)
        .values(
            status="pending",
            error=None,
            attempts=0,
            next_attempt_at=None,
            sent_at=None,
        )
    )
    db.commit()
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
    db.execute(
        update(OutboundQueue)
        .where(OutboundQueue.id == queue_id)
        .values(status="cancelled", next_attempt_at=None)
    )
    db.commit()


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
    db.commit()


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
        db.commit()
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
            db.commit()
            deleted_interactions += int(result.rowcount or 0)
            if len(ids_chunk) < batch_size:
                break

    return CleanupResult(
        dialogs_deleted=dialogs_to_delete,
        messages_deleted=deleted_messages,
        interactions_deleted=deleted_interactions,
        dry_run=False,
    )
