"""
SSE-эндпоинт для веб-панели: стримим новые сообщения из corebot.db
(neuro_chat_messages + outbound_queue.sent) клиенту.

Авторизация: либо `Authorization: Bearer <jwt>`, либо `?token=<jwt>` в URL
(EventSource API не позволяет задать кастомный header).
"""
from __future__ import annotations

import asyncio
import json
from utils.time import utcnow_aware
from typing import AsyncGenerator, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from control_plane.auth import decode_token
from control_plane.business.db import BotSession
from control_plane.config import (
    CP_BUSINESS_STREAM_BATCH,
    CP_BUSINESS_STREAM_INTERVAL,
)
from control_plane.database import get_db as get_cp_db
from control_plane.models import User
from database.models import Account, NeuroChatMessage

router = APIRouter(prefix="/business", tags=["business-stream"])


def _resolve_user_from_jwt(token: str, cp_db: Session) -> User:
    try:
        payload = decode_token(token)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=f"invalid token: {e}"
        ) from e
    if payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="wrong token type"
        )
    uid = int(payload.get("sub"))
    user = cp_db.query(User).filter(User.id == uid, User.is_active == True).first()  # noqa: E712
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="user not found")
    return user


def _format_event(event_name: str, data: dict) -> bytes:
    payload = json.dumps(data, default=str, ensure_ascii=False)
    return f"event: {event_name}\ndata: {payload}\n\n".encode("utf-8")


@router.get("/stream")
async def stream_messages(
    request: Request,
    token: Optional[str] = Query(default=None),
    account_id: Optional[int] = Query(default=None),
    cp_db: Session = Depends(get_cp_db),
):
    auth_header = request.headers.get("authorization", "")
    raw_token = ""
    if auth_header.lower().startswith("bearer "):
        raw_token = auth_header.split(" ", 1)[1].strip()
    if not raw_token and token:
        raw_token = token.strip()
    if not raw_token:
        raise HTTPException(status_code=401, detail="missing token")
    _ = _resolve_user_from_jwt(raw_token, cp_db)

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
