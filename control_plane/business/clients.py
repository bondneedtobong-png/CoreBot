"""
Бизнес-API для клиентов: список с фильтрами, детали, последние взаимодействия,
правка классов (без миграции старой логики бота — мы только править/удалять
строки в client_class_counters), удаление клиента.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, desc, func, or_, select, update
from sqlalchemy.orm import Session

from control_plane.business.db import get_bot_db
from control_plane.business.schemas import (
    ClientClassCount,
    ClientClassUpdate,
    ClientClassUpdateResult,
    ClientDetail,
    ClientInteractionItem,
    ClientListItem,
)
from control_plane.deps import get_current_user, require_operator_write
from control_plane.models import User
from database.models import (
    Client,
    ClientClassCounter,
    ClientInteraction,
    ClientTag,
)


router = APIRouter(prefix="/business/clients", tags=["business-clients"])


def _classes_for(db: Session, client_id: int) -> list[ClientClassCount]:
    rows = db.execute(
        select(ClientClassCounter.class_key, ClientClassCounter.count).where(
            ClientClassCounter.client_id == client_id,
            ClientClassCounter.count > 0,
        )
    ).all()
    return [ClientClassCount(class_key=str(k), count=int(c)) for (k, c) in rows]


def _classes_bulk(db: Session, client_ids: list[int]) -> dict[int, list[ClientClassCount]]:
    if not client_ids:
        return {}
    rows = db.execute(
        select(
            ClientClassCounter.client_id,
            ClientClassCounter.class_key,
            ClientClassCounter.count,
        ).where(
            ClientClassCounter.client_id.in_(client_ids),
            ClientClassCounter.count > 0,
        )
    ).all()
    out: dict[int, list[ClientClassCount]] = {cid: [] for cid in client_ids}
    for cid, key, cnt in rows:
        out.setdefault(int(cid), []).append(
            ClientClassCount(class_key=str(key), count=int(cnt))
        )
    return out


def _serialize_list(c: Client, classes: list[ClientClassCount]) -> ClientListItem:
    status_val = (
        getattr(c.status, "value", str(c.status)) if c.status else "new"
    )
    return ClientListItem(
        id=int(c.id),
        username=c.username,
        telegram_user_id=int(c.telegram_user_id) if c.telegram_user_id else None,
        status=status_val,
        added_at=c.added_at,
        last_contacted_at=c.last_contacted_at,
        classes=classes,
    )


@router.get("", response_model=list[ClientListItem])
def list_clients(
    q: Optional[str] = Query(default=None, description="поиск по username"),
    class_key: Optional[str] = Query(default=None, description="фильтр по классу"),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    stmt = select(Client)
    if q:
        like = f"%{q.strip().lstrip('@').lower()}%"
        stmt = stmt.where(func.lower(Client.username).like(like))
    if status_filter:
        stmt = stmt.where(Client.status == status_filter.lower())
    if class_key:
        normalized = class_key.strip().lstrip("{").rstrip("}").lower()
        sub = (
            select(ClientClassCounter.client_id)
            .where(
                func.lower(ClientClassCounter.class_key) == normalized,
                ClientClassCounter.count > 0,
            )
            .distinct()
        )
        stmt = stmt.where(Client.id.in_(sub))
    stmt = stmt.order_by(desc(Client.id)).limit(limit).offset(offset)

    rows = list(db.execute(stmt).scalars().all())
    classes_map = _classes_bulk(db, [int(c.id) for c in rows])
    return [_serialize_list(c, classes_map.get(int(c.id), [])) for c in rows]


@router.get("/{client_id}", response_model=ClientDetail)
def get_client(
    client_id: int,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    c = db.get(Client, client_id)
    if not c:
        raise HTTPException(status_code=404, detail="client not found")
    classes = _classes_for(db, client_id)
    interactions_count = int(
        db.execute(
            select(func.count(ClientInteraction.id)).where(
                ClientInteraction.client_id == client_id
            )
        ).scalar_one()
        or 0
    )
    tag_rows = db.execute(
        select(ClientTag.tag).where(ClientTag.client_id == client_id)
    ).all()
    tags = [str(t[0]) for t in tag_rows]
    base = _serialize_list(c, classes).model_dump()
    base.update(interactions_count=interactions_count, tags=tags)
    return ClientDetail(**base)


@router.get(
    "/{client_id}/interactions", response_model=list[ClientInteractionItem]
)
def list_client_interactions(
    client_id: int,
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    if not db.get(Client, client_id):
        raise HTTPException(status_code=404, detail="client not found")
    rows = (
        db.execute(
            select(ClientInteraction)
            .where(ClientInteraction.client_id == client_id)
            .order_by(desc(ClientInteraction.id))
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return [
        ClientInteractionItem(
            id=int(r.id),
            direction=r.direction,
            kind=r.kind,
            body=r.body,
            account_id=int(r.account_id) if r.account_id else None,
            mailing_id=int(r.mailing_id) if r.mailing_id else None,
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.post("/{client_id}/class", response_model=ClientClassUpdateResult)
def update_client_class(
    client_id: int,
    payload: ClientClassUpdate,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(require_operator_write),
):
    if not db.get(Client, client_id):
        raise HTTPException(status_code=404, detail="client not found")
    key = payload.class_key.strip().lstrip("{").rstrip("}").lower()
    if not key:
        raise HTTPException(status_code=400, detail="empty class_key")

    existing = db.execute(
        select(ClientClassCounter).where(
            ClientClassCounter.client_id == client_id,
            ClientClassCounter.class_key == key,
        )
    ).scalar_one_or_none()

    if payload.set_value is not None:
        new_value = max(0, int(payload.set_value))
    else:
        cur = int(existing.count) if existing else 0
        new_value = max(0, cur + int(payload.delta or 0))

    if existing is None:
        if new_value > 0:
            db.add(
                ClientClassCounter(
                    client_id=client_id, class_key=key, count=new_value
                )
            )
    else:
        if new_value <= 0:
            db.execute(
                delete(ClientClassCounter).where(
                    ClientClassCounter.id == existing.id
                )
            )
            new_value = 0
        else:
            db.execute(
                update(ClientClassCounter)
                .where(ClientClassCounter.id == existing.id)
                .values(count=new_value)
            )
    db.commit()
    return ClientClassUpdateResult(
        client_id=client_id, class_key=key, new_count=new_value
    )


@router.delete("/{client_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_client(
    client_id: int,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(require_operator_write),
):
    """
    Удаление клиента. Каскадно зачищает class_counters/tags/interactions.
    NeuroChatMessages привязаны к peer_user_id (а не к client_id), поэтому
    тут не трогаются — для них есть отдельный cleanup.
    """
    if not db.get(Client, client_id):
        raise HTTPException(status_code=404, detail="client not found")
    db.execute(delete(Client).where(Client.id == client_id))
    db.commit()
