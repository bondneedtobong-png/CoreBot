# -*- coding: utf-8 -*-
"""Бизнес-API трекинг-ссылок + публичный редирект /r/{code}.

Механика: владелец создаёт короткую ссылку на канал/пост, вставляет её
в рекламу (или в community_link рассылки через {link}) — каждый переход
GET /r/{code} → 307 на target_url с записью хита. Счётчики — в дашборде.

Ограничение: ссылка кликабельна извне, только если хост Control Plane
доступен из интернета (домен/VPS). На локальном 127.0.0.1 — для проверки.
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from control_plane.business.db import get_bot_db
from control_plane.business.schemas import LinkCreate, LinkItem
from control_plane.deps import get_current_user, require_operator_write
from control_plane.models import User
from database.models import LinkHit, Mailing, TrackedLink
from utils.time import utcnow_naive


router = APIRouter(tags=["business-links"])


def _gen_code(db: Session) -> str:
    for _ in range(20):
        code = secrets.token_urlsafe(6)[:8]
        exists = (
            db.execute(select(TrackedLink.id).where(TrackedLink.code == code))
            .scalars()
            .first()
        )
        if not exists:
            return code
    raise HTTPException(status_code=500, detail="cannot generate unique link code")


def _counts(db: Session, link_ids: list[int]) -> tuple[dict[int, int], dict[int, int]]:
    if not link_ids:
        return {}, {}
    total = dict(
        db.execute(
            select(LinkHit.link_id, func.count(LinkHit.id))
            .where(LinkHit.link_id.in_(link_ids))
            .group_by(LinkHit.link_id)
        ).all()
    )
    since = utcnow_naive() - timedelta(hours=24)
    day = dict(
        db.execute(
            select(LinkHit.link_id, func.count(LinkHit.id))
            .where(LinkHit.link_id.in_(link_ids), LinkHit.created_at >= since)
            .group_by(LinkHit.link_id)
        ).all()
    )
    return total, day


def _serialize(link: TrackedLink, total: int = 0, day: int = 0) -> LinkItem:
    return LinkItem(
        id=int(link.id),
        code=link.code,
        name=link.name or "",
        target_url=link.target_url,
        mailing_id=int(link.mailing_id) if link.mailing_id else None,
        created_at=link.created_at,
        clicks_total=int(total),
        clicks_24h=int(day),
    )


@router.post(
    "/business/links", response_model=LinkItem, status_code=status.HTTP_201_CREATED
)
def create_link(
    payload: LinkCreate,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(require_operator_write),
):
    target = (payload.target_url or "").strip()
    if not target.lower().startswith(("http://", "https://")):
        raise HTTPException(
            status_code=400, detail="target_url must start with http(s)://"
        )
    mailing_id = None
    if payload.mailing_id:
        m = db.get(Mailing, int(payload.mailing_id))
        if not m:
            raise HTTPException(status_code=400, detail="mailing not found")
        mailing_id = int(m.id)
    link = TrackedLink(
        code=_gen_code(db),
        name=(payload.name or "").strip()[:255],
        target_url=target[:2048],
        mailing_id=mailing_id,
    )
    db.add(link)
    db.commit()
    db.refresh(link)
    return _serialize(link)


@router.get("/business/links", response_model=list[LinkItem])
def list_links(
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    rows = (
        db.execute(select(TrackedLink).order_by(TrackedLink.id.desc())).scalars().all()
    )
    ids = [int(r.id) for r in rows]
    total, day = _counts(db, ids)
    return [_serialize(r, total.get(int(r.id), 0), day.get(int(r.id), 0)) for r in rows]


@router.delete("/business/links/{link_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_link(
    link_id: int,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(require_operator_write),
):
    link = db.get(TrackedLink, link_id)
    if not link:
        raise HTTPException(status_code=404, detail="link not found")
    db.execute(delete(LinkHit).where(LinkHit.link_id == link_id))
    db.delete(link)
    db.commit()


@router.get("/r/{code}", include_in_schema=False)
def redirect_link(
    code: str,
    request: Request,
    db: Session = Depends(get_bot_db),
):
    """Публичный редирект БЕЗ авторизации: иначе внешние клики не пройдут."""
    link = (
        db.execute(select(TrackedLink).where(TrackedLink.code == code))
        .scalars()
        .first()
    )
    if not link:
        raise HTTPException(status_code=404, detail="unknown link")
    try:
        client_ip = request.client.host if request.client else None
        ua = (request.headers.get("user-agent") or "")[:255]
        db.add(
            LinkHit(
                link_id=int(link.id),
                ip=(client_ip or "")[:64] or None,
                ua=ua or None,
            )
        )
        db.commit()
    except Exception:
        db.rollback()
    return RedirectResponse(url=link.target_url, status_code=307)
