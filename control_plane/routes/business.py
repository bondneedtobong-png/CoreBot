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
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, delete, desc, func, select, update
from sqlalchemy.orm import Session

from control_plane.business.db import get_bot_db
from control_plane.business.schemas import (
    AccountListItem,
    AccountModeIn,
    CleanupRequest,
    CleanupResult,
    DialogListItem,
    MessageOut,
    SendMessageIn,
    SendMessageOut,
)
from control_plane.deps import get_current_user
from control_plane.models import User
from database.models import (
    Account,
    Client,
    ClientClassCounter,
    ClientInteraction,
    NeuroChatMessage,
    OutboundQueue,
)

router = APIRouter(prefix="/business", tags=["business"])


# ---------------------------- Accounts -----------------------------------


def _serialize_account(
    a: Account,
    *,
    dialogs_count: int = 0,
    pending_outbound: int = 0,
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

    return [
        _serialize_account(
            a,
            dialogs_count=dialog_counts.get(a.id, 0),
            pending_outbound=pending_counts.get(a.id, 0),
        )
        for a in accounts
    ]


@router.post("/accounts/{account_id}/mode", response_model=AccountListItem)
def set_account_mode(
    account_id: int,
    payload: AccountModeIn,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="account not found")
    new_mode = payload.mode.upper()
    db.execute(
        update(Account)
        .where(Account.id == account_id)
        .values(ai_mode=new_mode, updated_at=datetime.utcnow())
    )
    db.commit()
    db.refresh(account)
    return _serialize_account(account)


# ---------------------------- Dialogs ------------------------------------


def _ensure_account(db: Session, account_id: int) -> Account:
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="account not found")
    return account


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
                    created_at=q.created_at or datetime.utcnow(),
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
    user: User = Depends(get_current_user),
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
    _user: User = Depends(get_current_user),
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
    _user: User = Depends(get_current_user),
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
    _user: User = Depends(get_current_user),
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
    _user: User = Depends(get_current_user),
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
        threshold = datetime.utcnow() - timedelta(days=int(payload.older_than_days))
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
