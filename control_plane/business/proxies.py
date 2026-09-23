"""
Бизнес-API прокси (`proxies` + `proxy_groups`).

Тест соединения — лёгкий TCP-connect на host:port; не валидирует SOCKS5
handshake. Этого достаточно, чтобы быстро понять «есть ли вообще пакеты
до прокси». Для полноценной проверки используйте кнопку «Спам-чек» в
аккаунте — она поднимает Telethon с конкретным прокси.

Эндпоинты:
    GET    /business/proxies                  — список с группой и счётчиком аккаунтов
    POST   /business/proxies                  — создать
    PATCH  /business/proxies/{id}             — изменить
    DELETE /business/proxies/{id}             — удалить (Account.proxy_id → NULL)
    POST   /business/proxies/{id}/test        — TCP-проверка
    GET    /business/proxy-groups             — список групп прокси
"""
from __future__ import annotations

import socket
import time
from utils.time import utcnow_naive

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from control_plane.business.db import get_bot_db
from control_plane.business.schemas import (
    ProxyCreate,
    ProxyGroupItem,
    ProxyItem,
    ProxyPatch,
    ProxyTestResult,
)
from control_plane.deps import get_current_user, require_operator_write
from control_plane.models import User
from database.models import Account, Proxy, ProxyGroup, ProxyType


router = APIRouter(tags=["business-proxies"])


# ----------------------------- helpers -----------------------------------


def _proxy_type_to_str(value: object) -> str:
    if isinstance(value, ProxyType):
        return value.value
    if value is None:
        return "socks5"
    s = str(value).lower()
    return s if s in ("socks5", "http") else "socks5"


def _proxy_type_from_str(value: str | None) -> ProxyType:
    s = (value or "").strip().lower()
    if s == "http":
        return ProxyType.HTTP
    return ProxyType.SOCKS5


def _serialize_proxy(
    p: Proxy,
    *,
    accounts_count: int = 0,
    group_name: str | None = None,
) -> ProxyItem:
    return ProxyItem(
        id=int(p.id),
        name=p.name,
        host=p.host,
        port=int(p.port),
        username=p.username,
        proxy_type=_proxy_type_to_str(p.proxy_type),
        is_active=bool(p.is_active),
        is_working=bool(p.is_working),
        group_id=int(p.group_id) if p.group_id else None,
        group_name=group_name,
        accounts_count=int(accounts_count),
        last_checked=p.last_checked,
    )


# ----------------------------- proxies -----------------------------------


@router.get("/business/proxies", response_model=list[ProxyItem])
def list_proxies(
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    counts = dict(
        db.execute(
            select(Account.proxy_id, func.count(Account.id))
            .where(Account.proxy_id.is_not(None))
            .group_by(Account.proxy_id)
        ).all()
    )
    groups = {
        int(g.id): g.name
        for g in db.execute(select(ProxyGroup)).scalars().all()
    }
    rows = db.execute(select(Proxy).order_by(Proxy.id)).scalars().all()
    return [
        _serialize_proxy(
            p,
            accounts_count=counts.get(p.id, 0),
            group_name=groups.get(int(p.group_id)) if p.group_id else None,
        )
        for p in rows
    ]


@router.post(
    "/business/proxies",
    response_model=ProxyItem,
    status_code=status.HTTP_201_CREATED,
)
def create_proxy(
    payload: ProxyCreate,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(require_operator_write),
):
    if payload.group_id is not None and payload.group_id > 0:
        if not db.get(ProxyGroup, int(payload.group_id)):
            raise HTTPException(status_code=400, detail="proxy group not found")
    p = Proxy(
        name=payload.name.strip(),
        host=payload.host.strip(),
        port=int(payload.port),
        username=(payload.username or None),
        password=(payload.password or None),
        proxy_type=_proxy_type_from_str(payload.proxy_type),
        is_active=bool(payload.is_active),
        is_working=True,
        group_id=int(payload.group_id) if payload.group_id else None,
    )
    db.add(p)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="proxy with this name exists")
    db.refresh(p)
    group_name = None
    if p.group_id:
        gg = db.get(ProxyGroup, int(p.group_id))
        group_name = gg.name if gg else None
    return _serialize_proxy(p, accounts_count=0, group_name=group_name)


@router.patch("/business/proxies/{proxy_id}", response_model=ProxyItem)
def patch_proxy(
    proxy_id: int,
    payload: ProxyPatch,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(require_operator_write),
):
    p = db.get(Proxy, proxy_id)
    if not p:
        raise HTTPException(status_code=404, detail="proxy not found")
    if payload.name is not None:
        p.name = payload.name.strip()
    if payload.host is not None:
        p.host = payload.host.strip()
    if payload.port is not None:
        p.port = int(payload.port)
    if payload.username is not None:
        p.username = payload.username or None
    if payload.password is not None:
        p.password = payload.password or None
    if payload.proxy_type is not None:
        p.proxy_type = _proxy_type_from_str(payload.proxy_type)
    if payload.group_id is not None:
        if int(payload.group_id) <= 0:
            p.group_id = None
        else:
            if not db.get(ProxyGroup, int(payload.group_id)):
                raise HTTPException(status_code=400, detail="proxy group not found")
            p.group_id = int(payload.group_id)
    if payload.is_active is not None:
        p.is_active = bool(payload.is_active)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="proxy with this name exists")
    db.refresh(p)
    group_name = None
    if p.group_id:
        gg = db.get(ProxyGroup, int(p.group_id))
        group_name = gg.name if gg else None
    cnt = int(
        db.execute(
            select(func.count(Account.id)).where(Account.proxy_id == p.id)
        ).scalar_one()
        or 0
    )
    return _serialize_proxy(p, accounts_count=cnt, group_name=group_name)


@router.delete(
    "/business/proxies/{proxy_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_proxy(
    proxy_id: int,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(require_operator_write),
):
    p = db.get(Proxy, proxy_id)
    if not p:
        raise HTTPException(status_code=404, detail="proxy not found")
    db.execute(
        update(Account).where(Account.proxy_id == proxy_id).values(proxy_id=None)
    )
    db.delete(p)
    db.commit()


@router.post("/business/proxies/{proxy_id}/test", response_model=ProxyTestResult)
def test_proxy(
    proxy_id: int,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(require_operator_write),
):
    p = db.get(Proxy, proxy_id)
    if not p:
        raise HTTPException(status_code=404, detail="proxy not found")
    started = time.perf_counter()
    ok = False
    detail: str | None = None
    try:
        with socket.create_connection((p.host, int(p.port)), timeout=5.0):
            ok = True
            detail = "tcp connect ok"
    except OSError as e:
        ok = False
        detail = f"{type(e).__name__}: {e}"
    elapsed = int((time.perf_counter() - started) * 1000)
    p.last_checked = utcnow_naive()
    p.is_working = bool(ok)
    db.commit()
    return ProxyTestResult(
        proxy_id=int(proxy_id),
        ok=ok,
        elapsed_ms=elapsed,
        detail=detail,
    )


# ----------------------------- proxy-groups ------------------------------


@router.get("/business/proxy-groups", response_model=list[ProxyGroupItem])
def list_proxy_groups(
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    counts = dict(
        db.execute(
            select(Proxy.group_id, func.count(Proxy.id))
            .where(Proxy.group_id.is_not(None))
            .group_by(Proxy.group_id)
        ).all()
    )
    rows = db.execute(select(ProxyGroup).order_by(ProxyGroup.id)).scalars().all()
    return [
        ProxyGroupItem(
            id=int(g.id),
            name=g.name or f"#{g.id}",
            proxies_count=int(counts.get(g.id, 0)),
        )
        for g in rows
    ]
