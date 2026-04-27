"""
Бизнес-API групп аккаунтов (`groups` + many-to-many `account_groups`).

Эндпоинты:
    GET    /business/groups                              — список с количеством аккаунтов
    POST   /business/groups                              — создать
    PATCH  /business/groups/{id}                         — переименовать
    DELETE /business/groups/{id}                         — удалить (привязки очищаются)
    GET    /business/groups/{id}/accounts                — аккаунты группы
    PUT    /business/groups/{id}/accounts                — заменить список аккаунтов
    POST   /business/groups/{id}/accounts/{account_id}   — добавить аккаунт
    DELETE /business/groups/{id}/accounts/{account_id}   — убрать аккаунт
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from control_plane.business.db import get_bot_db
from control_plane.business.schemas import (
    GroupAccountsItem,
    GroupAccountsSet,
    GroupCreate,
    GroupItem,
    GroupRename,
)
from control_plane.deps import get_current_user, require_operator_write
from control_plane.models import User
from database.models import Account, Group, account_groups


router = APIRouter(prefix="/business/groups", tags=["business-groups"])


def _serialize(g: Group, accounts_count: int) -> GroupItem:
    return GroupItem(
        id=int(g.id),
        name=g.name or f"#{g.id}",
        accounts_count=int(accounts_count),
        created_at=g.created_at,
    )


def _account_label(a: Account) -> str:
    label = (a.list_label or "").strip() if a.list_label else ""
    if label:
        return label
    if a.username:
        return f"@{a.username}"
    return a.phone or f"#{a.id}"


@router.get("", response_model=list[GroupItem])
def list_groups(
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    counts = dict(
        db.execute(
            select(account_groups.c.group_id, func.count(account_groups.c.account_id))
            .group_by(account_groups.c.group_id)
        ).all()
    )
    rows = db.execute(select(Group).order_by(Group.id)).scalars().all()
    return [_serialize(g, counts.get(g.id, 0)) for g in rows]


@router.post("", response_model=GroupItem, status_code=status.HTTP_201_CREATED)
def create_group(
    payload: GroupCreate,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(require_operator_write),
):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="empty name")
    g = Group(name=name)
    db.add(g)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="group with this name exists")
    db.refresh(g)
    return _serialize(g, 0)


@router.patch("/{group_id}", response_model=GroupItem)
def rename_group(
    group_id: int,
    payload: GroupRename,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(require_operator_write),
):
    g = db.get(Group, group_id)
    if not g:
        raise HTTPException(status_code=404, detail="group not found")
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="empty name")
    g.name = name
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="group with this name exists")
    db.refresh(g)
    cnt = int(
        db.execute(
            select(func.count(account_groups.c.account_id)).where(
                account_groups.c.group_id == group_id
            )
        ).scalar_one()
        or 0
    )
    return _serialize(g, cnt)


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_group(
    group_id: int,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(require_operator_write),
):
    g = db.get(Group, group_id)
    if not g:
        raise HTTPException(status_code=404, detail="group not found")
    db.execute(
        delete(account_groups).where(account_groups.c.group_id == group_id)
    )
    db.delete(g)
    db.commit()


@router.get("/{group_id}/accounts", response_model=list[GroupAccountsItem])
def list_group_accounts(
    group_id: int,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    g = db.get(Group, group_id)
    if not g:
        raise HTTPException(status_code=404, detail="group not found")
    rows = (
        db.execute(
            select(Account)
            .join(account_groups, Account.id == account_groups.c.account_id)
            .where(account_groups.c.group_id == group_id)
            .order_by(Account.id)
        )
        .scalars()
        .all()
    )
    return [
        GroupAccountsItem(
            id=int(a.id),
            title=_account_label(a),
            username=a.username,
            phone=a.phone,
        )
        for a in rows
    ]


@router.put("/{group_id}/accounts", response_model=list[GroupAccountsItem])
def set_group_accounts(
    group_id: int,
    payload: GroupAccountsSet,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(require_operator_write),
):
    g = db.get(Group, group_id)
    if not g:
        raise HTTPException(status_code=404, detail="group not found")
    desired_ids = sorted({int(x) for x in (payload.account_ids or []) if int(x) > 0})
    if desired_ids:
        existing = (
            db.execute(select(Account.id).where(Account.id.in_(desired_ids)))
            .scalars()
            .all()
        )
        if len(existing) != len(desired_ids):
            missing = sorted(set(desired_ids) - set(int(x) for x in existing))
            raise HTTPException(
                status_code=400,
                detail=f"unknown account ids: {missing}",
            )
    db.execute(delete(account_groups).where(account_groups.c.group_id == group_id))
    if desired_ids:
        db.execute(
            insert(account_groups),
            [{"account_id": aid, "group_id": group_id} for aid in desired_ids],
        )
    db.commit()
    return list_group_accounts(group_id, db, _user)  # type: ignore[arg-type]


@router.post(
    "/{group_id}/accounts/{account_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def add_group_account(
    group_id: int,
    account_id: int,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(require_operator_write),
):
    g = db.get(Group, group_id)
    if not g:
        raise HTTPException(status_code=404, detail="group not found")
    a = db.get(Account, account_id)
    if not a:
        raise HTTPException(status_code=404, detail="account not found")
    existing = db.execute(
        select(account_groups.c.account_id).where(
            account_groups.c.group_id == group_id,
            account_groups.c.account_id == account_id,
        )
    ).first()
    if existing:
        return
    db.execute(
        insert(account_groups).values(account_id=account_id, group_id=group_id)
    )
    db.commit()


@router.delete(
    "/{group_id}/accounts/{account_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def remove_group_account(
    group_id: int,
    account_id: int,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(require_operator_write),
):
    g = db.get(Group, group_id)
    if not g:
        raise HTTPException(status_code=404, detail="group not found")
    db.execute(
        delete(account_groups).where(
            account_groups.c.group_id == group_id,
            account_groups.c.account_id == account_id,
        )
    )
    db.commit()
