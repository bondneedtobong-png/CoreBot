"""
SSE-эндпоинт для веб-панели: стримим новые сообщения из corebot.db
(neuro_chat_messages + outbound_queue.sent) клиенту.

Авторизация: `Authorization: Bearer <jwt>` либо HttpOnly cookie `corebot_stream`.
"""
from __future__ import annotations

import asyncio
import json
from utils.time import utcnow_aware
from typing import AsyncGenerator, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from control_plane.business.db import BotSession
from control_plane.config import (
    CP_BUSINESS_STREAM_BATCH,
    CP_BUSINESS_STREAM_INTERVAL,
)
from control_plane.database import get_db as get_cp_db
from control_plane.deps import get_current_user
from control_plane.models import User
from control_plane.routes.auth import STREAM_COOKIE_NAME
from database.models import Account, NeuroChatMessage

router = APIRouter(prefix="/business", tags=["business-stream"])


def _resolve_user_from_jwt(token: str, cp_db: Session) -> User:
    """Apply the same access-token and active-user checks as normal CP routes."""
    try:
        return get_current_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials=token), cp_db)
    except HTTPException:
        raise
    except (TypeError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from None


def resolve_stream_user(request: Request, cp_db: Session) -> User:
    if "token" in request.query_params:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="query token is not supported")
    auth_header = request.headers.get("authorization", "")
    if auth_header:
        scheme, _, token = auth_header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    else:
        token = request.cookies.get(STREAM_COOKIE_NAME, "")
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")
    return _resolve_user_from_jwt(token.strip(), cp_db)


def _format_event(event_name: str, data: dict) -> bytes:
    payload = json.dumps(data, default=str, ensure_ascii=False)
    return f"event: {event_name}\ndata: {payload}\n\n".encode("utf-8")


@router.get("/stream")
async def stream_messages(
    request: Request,
    account_id: Optional[int] = Query(default=None),
    cp_db: Session = Depends(get_cp_db),
):
    resolve_stream_user(request, cp_db)

    account_filter = int(account_id) if account_id else None

    async def event_generator() -> AsyncGenerator[bytes, None]:
        last_id = 0
        with BotSession() as bot_db:
            stmt = select(NeuroChatMessage.id).order_by(NeuroChatMessage.id.desc()).limit(1)
            row = bot_db.execute(stmt).first()
            if row:
                last_id = int(row[0])

        yield _format_event(
            "hello",
            {"ts": utcnow_aware().isoformat(), "cursor": last_id},
        )

        idle_ticks = 0
        while True:
            if await request.is_disconnected():
                break

            with BotSession() as bot_db:
                q = (
                    select(NeuroChatMessage, Account.username, Account.list_label)
                    .join(Account, Account.id == NeuroChatMessage.account_id, isouter=True)
                    .where(NeuroChatMessage.id > last_id)
                    .order_by(NeuroChatMessage.id.asc())
                    .limit(CP_BUSINESS_STREAM_BATCH)
                )
                if account_filter is not None:
                    q = q.where(NeuroChatMessage.account_id == account_filter)
                rows = bot_db.execute(q).all()

            if rows:
                idle_ticks = 0
                for msg, account_username, account_label in rows:
                    last_id = max(last_id, int(msg.id))
                    yield _format_event(
                        "message",
                        {
                            "id": int(msg.id),
                            "account_id": int(msg.account_id),
                            "account_username": account_username,
                            "account_label": account_label,
                            "peer_user_id": int(msg.peer_user_id),
                            "role": msg.role,
                            "content": msg.content,
                            "created_at": (
                                msg.created_at.isoformat() if msg.created_at else None
                            ),
                            "source": "neuro",
                        },
                    )
            else:
                idle_ticks += 1
                if idle_ticks % 12 == 0:
                    # лёгкий keep-alive раз в ~18 сек, чтобы прокси не закрыли соединение
                    yield _format_event("ping", {"ts": utcnow_aware().isoformat()})

            try:
                await asyncio.sleep(CP_BUSINESS_STREAM_INTERVAL)
            except asyncio.CancelledError:
                break

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # отключаем буферизацию nginx
        },
    )
