"""
Расширенный cleanup и архив.

Все безопасные операции:
  POST /business/cleanup/v2          mode=archive|hard
  POST /business/archive/restore     восстановить из архива
  GET  /business/archive/dialogs     список архивных диалогов

Старый /business/cleanup сохранён в business.py для обратной совместимости и
сейчас по умолчанию работает в режиме hard. Новые клиенты должны звать /v2.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, asc, delete, desc, func, insert, select
from sqlalchemy.orm import Session

from control_plane.business.db import get_bot_db
from control_plane.business.schemas import (
    ArchivedDialogItem,
    ArchiveRestoreRequest,
    ArchiveRestoreResult,
    CleanupRequestV2,
    CleanupResult,
)
from control_plane.deps import get_current_user
from control_plane.models import User
from database.models import (
    Account,
    Client,
    ClientClassCounter,
    ClientInteraction,
    ClientInteractionArchive,
    NeuroChatMessage,
    NeuroChatMessageArchive,
)


router = APIRouter(prefix="/business", tags=["business-archive"])


def _build_filters(payload: CleanupRequestV2, db: Session):
    msg_filters = []
    interaction_filters = []

    if payload.account_id is not None:
        msg_filters.append(NeuroChatMessage.account_id == int(payload.account_id))
        interaction_filters.append(
            ClientInteraction.account_id == int(payload.account_id)
        )
    if payload.peer_user_id is not None:
        msg_filters.append(
            NeuroChatMessage.peer_user_id == int(payload.peer_user_id)
        )

    if payload.older_than_days is not None:
        threshold = datetime.utcnow() - timedelta(days=int(payload.older_than_days))
        msg_filters.append(NeuroChatMessage.created_at < threshold)
        interaction_filters.append(ClientInteraction.created_at < threshold)

    if payload.classes:
        normalized = [
            str(c).strip().lstrip("{").rstrip("}").lower() for c in payload.classes
        ]
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
            return msg_filters, interaction_filters, []
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
        interaction_filters.append(
            ClientInteraction.client_id.in_(target_client_ids)
        )
    return msg_filters, interaction_filters, None


@router.post("/cleanup/v2", response_model=CleanupResult)
def cleanup_v2(
    payload: CleanupRequestV2,
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

    built = _build_filters(payload, db)
    msg_filters, interaction_filters, empty = built[0], built[1], built[2]

    # подсчёт
    msg_count_q = select(func.count(NeuroChatMessage.id))
    if msg_filters:
        msg_count_q = msg_count_q.where(and_(*msg_filters))
    messages_to_delete = int(db.execute(msg_count_q).scalar_one() or 0)

    dialogs_q = select(
        func.count(
            func.distinct(
                NeuroChatMessage.account_id * 1000000 + NeuroChatMessage.peer_user_id
            )
        )
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
            messages_archived=0,
            interactions_archived=0,
            dry_run=True,
            mode=payload.mode,
        )

    archive_mode = payload.mode == "archive"
    batch_size = max(1, int(payload.batch_size))
    archived_at = datetime.utcnow()

    # ----- messages -----
    deleted_messages = 0
    archived_messages = 0
    while True:
        ids_stmt = select(NeuroChatMessage.id)
        if msg_filters:
            ids_stmt = ids_stmt.where(and_(*msg_filters))
        ids_stmt = ids_stmt.limit(batch_size)
        ids_chunk = [int(r[0]) for r in db.execute(ids_stmt).all()]
        if not ids_chunk:
            break

        if archive_mode:
            chunk_rows = db.execute(
                select(NeuroChatMessage).where(
                    NeuroChatMessage.id.in_(ids_chunk)
                )
            ).scalars().all()
            for m in chunk_rows:
                db.add(
                    NeuroChatMessageArchive(
                        original_id=int(m.id),
                        account_id=int(m.account_id),
                        peer_user_id=int(m.peer_user_id),
                        role=m.role,
                        content=m.content,
                        created_at=m.created_at,
                        archived_at=archived_at,
                    )
                )
            archived_messages += len(chunk_rows)

        result = db.execute(
            delete(NeuroChatMessage).where(NeuroChatMessage.id.in_(ids_chunk))
        )
        db.commit()
        deleted_messages += int(result.rowcount or 0)
        if len(ids_chunk) < batch_size:
            break

    # ----- interactions -----
    deleted_interactions = 0
    archived_interactions = 0
    if interaction_filters:
        while True:
            ids_stmt = (
                select(ClientInteraction.id)
                .where(and_(*interaction_filters))
                .limit(batch_size)
            )
            ids_chunk = [int(r[0]) for r in db.execute(ids_stmt).all()]
            if not ids_chunk:
                break

            if archive_mode:
                chunk_rows = db.execute(
                    select(ClientInteraction).where(
                        ClientInteraction.id.in_(ids_chunk)
                    )
                ).scalars().all()
                for ci in chunk_rows:
                    db.add(
                        ClientInteractionArchive(
                            original_id=int(ci.id),
                            client_id=int(ci.client_id),
                            account_id=int(ci.account_id) if ci.account_id else None,
                            mailing_id=int(ci.mailing_id) if ci.mailing_id else None,
                            direction=ci.direction,
                            kind=ci.kind,
                            body=ci.body,
                            payload_json=ci.payload_json,
                            telegram_message_id=(
                                int(ci.telegram_message_id)
                                if ci.telegram_message_id
                                else None
                            ),
                            created_at=ci.created_at,
                            archived_at=archived_at,
                        )
                    )
                archived_interactions += len(chunk_rows)

            result = db.execute(
                delete(ClientInteraction).where(
                    ClientInteraction.id.in_(ids_chunk)
                )
            )
            db.commit()
            deleted_interactions += int(result.rowcount or 0)
            if len(ids_chunk) < batch_size:
                break

    return CleanupResult(
        dialogs_deleted=dialogs_to_delete,
        messages_deleted=deleted_messages,
        interactions_deleted=deleted_interactions,
        messages_archived=archived_messages,
        interactions_archived=archived_interactions,
        dry_run=False,
        mode=payload.mode,
    )


# ---------------------------- Archive list -----------------------------


@router.get("/archive/dialogs", response_model=list[ArchivedDialogItem])
def list_archived_dialogs(
    account_id: Optional[int] = Query(default=None),
    archived_after: Optional[datetime] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    where = []
    if account_id is not None:
        where.append(NeuroChatMessageArchive.account_id == int(account_id))
    if archived_after is not None:
        where.append(NeuroChatMessageArchive.archived_at >= archived_after)

    stmt = (
        select(
            NeuroChatMessageArchive.account_id,
            NeuroChatMessageArchive.peer_user_id,
            func.count(NeuroChatMessageArchive.id).label("cnt"),
            func.max(NeuroChatMessageArchive.archived_at).label("last_at"),
        )
        .group_by(
            NeuroChatMessageArchive.account_id,
            NeuroChatMessageArchive.peer_user_id,
        )
        .order_by(desc("last_at"))
        .limit(limit)
    )
    if where:
        stmt = stmt.where(and_(*where))
    rows = db.execute(stmt).all()
    if not rows:
        return []
    account_ids = list({int(r[0]) for r in rows})
    accounts = {
        a.id: a
        for a in db.execute(select(Account).where(Account.id.in_(account_ids)))
        .scalars()
        .all()
    }
    peer_ids = list({int(r[1]) for r in rows})
    clients = {
        int(c.telegram_user_id): c
        for c in db.execute(
            select(Client).where(Client.telegram_user_id.in_(peer_ids))
        )
        .scalars()
        .all()
    }
    items: list[ArchivedDialogItem] = []
    for acc_id, peer, cnt, last_at in rows:
        a = accounts.get(int(acc_id))
        c = clients.get(int(peer))
        items.append(
            ArchivedDialogItem(
                account_id=int(acc_id),
                peer_user_id=int(peer),
                account_title=(
                    a.list_label or a.username or a.phone or f"#{acc_id}"
                ) if a else None,
                client_username=c.username if c else None,
                messages_count=int(cnt or 0),
                last_archived_at=last_at,
            )
        )
    return items


@router.post("/archive/restore", response_model=ArchiveRestoreResult)
def restore_archive(
    payload: ArchiveRestoreRequest,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    if not any(
        [
            payload.account_id is not None,
            payload.peer_user_id is not None,
            payload.archived_after is not None,
            payload.archived_before is not None,
        ]
    ):
        raise HTTPException(
            status_code=400,
            detail="provide at least one filter: account_id/peer_user_id/archived_after/archived_before",
        )

    # NeuroChatMessage restore
    msg_where = []
    if payload.account_id is not None:
        msg_where.append(
            NeuroChatMessageArchive.account_id == int(payload.account_id)
        )
    if payload.peer_user_id is not None:
        msg_where.append(
            NeuroChatMessageArchive.peer_user_id == int(payload.peer_user_id)
        )
    if payload.archived_after is not None:
        msg_where.append(
            NeuroChatMessageArchive.archived_at >= payload.archived_after
        )
    if payload.archived_before is not None:
        msg_where.append(
            NeuroChatMessageArchive.archived_at <= payload.archived_before
        )

    batch = max(1, int(payload.batch_size))
    restored_msgs = 0
    while True:
        rows = (
            db.execute(
                select(NeuroChatMessageArchive)
                .where(and_(*msg_where) if msg_where else None)
                .order_by(asc(NeuroChatMessageArchive.id))
                .limit(batch)
            )
            .scalars()
            .all()
        )
        if not rows:
            break
        for m in rows:
            db.add(
                NeuroChatMessage(
                    account_id=int(m.account_id),
                    peer_user_id=int(m.peer_user_id),
                    role=m.role,
                    content=m.content,
                    created_at=m.created_at,
                )
            )
        ids = [int(m.id) for m in rows]
        db.execute(
            delete(NeuroChatMessageArchive).where(
                NeuroChatMessageArchive.id.in_(ids)
            )
        )
        db.commit()
        restored_msgs += len(rows)
        if len(rows) < batch:
            break

    # ClientInteraction restore — по тем же account/archived_at, но peer_user_id
    # в interactions нет. Если фильтр был только peer_user_id — не трогаем.
    inter_where = []
    if payload.account_id is not None:
        inter_where.append(
            ClientInteractionArchive.account_id == int(payload.account_id)
        )
    if payload.archived_after is not None:
        inter_where.append(
            ClientInteractionArchive.archived_at >= payload.archived_after
        )
    if payload.archived_before is not None:
        inter_where.append(
            ClientInteractionArchive.archived_at <= payload.archived_before
        )

    restored_inters = 0
    if inter_where:
        while True:
            rows = (
                db.execute(
                    select(ClientInteractionArchive)
                    .where(and_(*inter_where))
                    .order_by(asc(ClientInteractionArchive.id))
                    .limit(batch)
                )
                .scalars()
                .all()
            )
            if not rows:
                break
            for ci in rows:
                db.add(
                    ClientInteraction(
                        client_id=int(ci.client_id),
                        account_id=int(ci.account_id) if ci.account_id else None,
                        mailing_id=int(ci.mailing_id) if ci.mailing_id else None,
                        direction=ci.direction,
                        kind=ci.kind,
                        body=ci.body,
                        payload_json=ci.payload_json,
                        telegram_message_id=ci.telegram_message_id,
                        created_at=ci.created_at,
                    )
                )
            ids = [int(ci.id) for ci in rows]
            db.execute(
                delete(ClientInteractionArchive).where(
                    ClientInteractionArchive.id.in_(ids)
                )
            )
            db.commit()
            restored_inters += len(rows)
            if len(rows) < batch:
                break

    return ArchiveRestoreResult(
        messages_restored=restored_msgs, interactions_restored=restored_inters
    )
