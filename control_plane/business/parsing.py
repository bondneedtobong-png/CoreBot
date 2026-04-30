"""
Бизнес-API модуля парсинга Telegram (задачи, логи, результаты, экспорт).

Читает/пишет в corebot.db через sync Session (get_bot_db).
Реальное выполнение — отдельный процесс workers/parser_worker.py.
"""
from __future__ import annotations

from datetime import datetime
import asyncio
import json
from typing import Optional, AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, Query, status, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from control_plane.auth import decode_token
from control_plane.business.db import get_bot_db, BotSession
from control_plane.database import get_db as get_cp_db
from control_plane.models import User as CpUser
from control_plane.business.schemas import (
    ParsedChannelRow,
    ParsedGroupRow,
    ParsedUserRow,
    ParsingTaskCreate,
    ParsingTaskLogOut,
    ParsingTaskOut,
)
from control_plane.deps import get_current_user, require_operator_write
from control_plane.models import User
from database.models import (
    Account,
    ParsedChannel,
    ParsedGroup,
    ParsedUser,
    ParsingTask,
    ParsingTaskLog,
)


router = APIRouter(prefix="/business/parsing", tags=["business-parsing"])


def _resolve_user_from_jwt(token: str, cp_db: Session) -> CpUser:
    try:
        payload = decode_token(token)
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"invalid token: {e}") from e
    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="wrong token type")
    uid = int(payload.get("sub"))
    u = cp_db.query(CpUser).filter(CpUser.id == uid, CpUser.is_active == True).first()  # noqa: E712
    if not u:
        raise HTTPException(status_code=401, detail="user not found")
    return u


def _sse_event(event: str, payload: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n".encode("utf-8")


def _task_to_out(t: ParsingTask) -> ParsingTaskOut:
    accounts = t.accounts_json if isinstance(t.accounts_json, list) else []
    params = t.params_json if isinstance(t.params_json, dict) else {}
    return ParsingTaskOut(
        id=int(t.id),
        kind=t.kind,
        status=t.status,
        params=params,
        account_ids=[int(x) for x in accounts],
        progress_percent=int(t.progress_percent or 0),
        current_stage=t.current_stage,
        current_account_id=t.current_account_id,
        current_query=t.current_query,
        found_count=int(t.found_count or 0),
        filtered_count=int(t.filtered_count or 0),
        error_count=int(t.error_count or 0),
        depth=int(t.depth or 1),
        mode=t.mode or "max_coverage",
        created_at=t.created_at,
        started_at=t.started_at,
        finished_at=t.finished_at,
        requested_by=t.requested_by,
        last_error=t.last_error,
    )


def _log_to_out(row: ParsingTaskLog) -> ParsingTaskLogOut:
    return ParsingTaskLogOut(
        id=int(row.id),
        task_id=int(row.task_id),
        level=row.level or "info",
        account_id=row.account_id,
        event=row.event,
        message=row.message,
        payload=row.payload_json if isinstance(row.payload_json, dict) else None,
        created_at=row.created_at,
    )


@router.post("/tasks", response_model=ParsingTaskOut, status_code=status.HTTP_201_CREATED)
def create_task(
    payload: ParsingTaskCreate,
    db: Session = Depends(get_bot_db),
    user: User = Depends(require_operator_write),
):
    acc_ids = list(dict.fromkeys(payload.account_ids))  # stable unique
    found = db.execute(select(Account.id).where(Account.id.in_(acc_ids))).scalars().all()
    found_set = set(found)
    missing = [i for i in acc_ids if i not in found_set]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown account_ids: {missing[:20]}",
        )
    label = f"{user.username} (id={user.id})"
    t = ParsingTask(
        kind=payload.kind,
        status="pending",
        params_json=dict(payload.params or {}),
        accounts_json=acc_ids,
        depth=payload.depth,
        mode=payload.mode,
        requested_by=label,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return _task_to_out(t)


@router.get("/tasks", response_model=list[ParsingTaskOut])
def list_tasks(
    status_filter: Optional[str] = Query(None, alias="status"),
    kind: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    q = select(ParsingTask).order_by(ParsingTask.id.desc())
    if status_filter:
        q = q.where(ParsingTask.status == status_filter)
    if kind:
        q = q.where(ParsingTask.kind == kind)
    q = q.limit(limit)
    rows = db.execute(q).scalars().all()
    return [_task_to_out(t) for t in rows]


@router.get("/tasks/{task_id}", response_model=ParsingTaskOut)
def get_task(
    task_id: int,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    t = db.get(ParsingTask, task_id)
    if not t:
        raise HTTPException(status_code=404, detail="task not found")
    return _task_to_out(t)


@router.post("/tasks/{task_id}/cancel", response_model=ParsingTaskOut)
def cancel_task(
    task_id: int,
    db: Session = Depends(get_bot_db),
    user: User = Depends(require_operator_write),
):
    t = db.get(ParsingTask, task_id)
    if not t:
        raise HTTPException(status_code=404, detail="task not found")
    if t.status in ("completed", "failed", "cancelled"):
        return _task_to_out(t)
    t.status = "cancelled"
    t.finished_at = datetime.utcnow()
    t.current_stage = "cancelled"
    t.last_error = None
    db.add(t)
    db.commit()
    db.refresh(t)
    log = ParsingTaskLog(
        task_id=t.id,
        level="info",
        account_id=None,
        event="cancel",
        message=f"Cancelled by {user.username}",
        payload_json=None,
    )
    db.add(log)
    db.commit()
    return _task_to_out(t)


@router.get("/tasks/{task_id}/logs", response_model=list[ParsingTaskLogOut])
def task_logs(
    task_id: int,
    limit: int = Query(500, ge=1, le=2000),
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    t = db.get(ParsingTask, task_id)
    if not t:
        raise HTTPException(status_code=404, detail="task not found")
    rows = (
        db.execute(
            select(ParsingTaskLog)
            .where(ParsingTaskLog.task_id == task_id)
            .order_by(ParsingTaskLog.id.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    rows.reverse()
    return [_log_to_out(r) for r in rows]


def _rows_channels(rows) -> list[ParsedChannelRow]:
    return [
        ParsedChannelRow(
            id=int(r.id),
            telegram_id=int(r.telegram_id),
            username=r.username,
            title=r.title,
            subscribers=r.subscribers,
            is_public=r.is_public,
            has_discussion=r.has_discussion,
            lang=r.lang,
            last_post_at=r.last_post_at,
            is_active_7d=r.is_active_7d,
            source_task_id=r.source_task_id,
            updated_at=r.updated_at,
        )
        for r in rows
    ]


def _rows_groups(rows) -> list[ParsedGroupRow]:
    return [
        ParsedGroupRow(
            id=int(r.id),
            telegram_id=int(r.telegram_id),
            username=r.username,
            title=r.title,
            members_count=r.members_count,
            group_type=r.group_type,
            lang=r.lang,
            is_active_7d=r.is_active_7d,
            source_task_id=r.source_task_id,
            updated_at=r.updated_at,
        )
        for r in rows
    ]


def _rows_users(rows) -> list[ParsedUserRow]:
    return [
        ParsedUserRow(
            id=int(r.id),
            telegram_id=int(r.telegram_id),
            username=r.username,
            display_name=r.display_name,
            has_avatar=r.has_avatar,
            last_seen_at=r.last_seen_at,
            lang_guess=r.lang_guess,
            is_deleted=bool(r.is_deleted),
            is_suspicious=bool(r.is_suspicious),
            source_task_id=r.source_task_id,
            updated_at=r.updated_at,
        )
        for r in rows
    ]


@router.get("/results/channels", response_model=list[ParsedChannelRow])
def results_channels(
    task_id: Optional[int] = Query(None),
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    q = select(ParsedChannel).order_by(ParsedChannel.id.desc())
    if task_id is not None:
        q = q.where(ParsedChannel.source_task_id == task_id)
    q = q.offset(offset).limit(limit)
    rows = db.execute(q).scalars().all()
    return _rows_channels(rows)


@router.get("/results/groups", response_model=list[ParsedGroupRow])
def results_groups(
    task_id: Optional[int] = Query(None),
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    q = select(ParsedGroup).order_by(ParsedGroup.id.desc())
    if task_id is not None:
        q = q.where(ParsedGroup.source_task_id == task_id)
    q = q.offset(offset).limit(limit)
    rows = db.execute(q).scalars().all()
    return _rows_groups(rows)


@router.get("/results/users", response_model=list[ParsedUserRow])
def results_users(
    task_id: Optional[int] = Query(None),
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    q = select(ParsedUser).order_by(ParsedUser.id.desc())
    if task_id is not None:
        q = q.where(ParsedUser.source_task_id == task_id)
    q = q.offset(offset).limit(limit)
    rows = db.execute(q).scalars().all()
    return _rows_users(rows)


def _export_lines_channels(rows) -> str:
    lines = []
    for r in rows:
        un = (r.username or "").strip()
        if un:
            lines.append(f"@{un.lstrip('@')}")
    # Экспортируем только username-строки; приватные сущности без @ пропускаем.
    return "\n".join(lines) + ("\n" if lines else "")


def _export_lines_groups(rows) -> str:
    return _export_lines_channels(rows)  # same format


def _export_lines_users(rows) -> str:
    return _export_lines_channels(rows)


@router.get("/export/channels.txt")
def export_channels_txt(
    task_id: Optional[int] = Query(None),
    limit: int = Query(50_000, ge=1, le=100_000),
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    q = select(ParsedChannel).order_by(ParsedChannel.id.asc())
    if task_id is not None:
        q = q.where(ParsedChannel.source_task_id == task_id)
    q = q.limit(limit)
    rows = db.execute(q).scalars().all()
    return PlainTextResponse(_export_lines_channels(rows), media_type="text/plain; charset=utf-8")


@router.get("/export/groups.txt")
def export_groups_txt(
    task_id: Optional[int] = Query(None),
    limit: int = Query(50_000, ge=1, le=100_000),
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    q = select(ParsedGroup).order_by(ParsedGroup.id.asc())
    if task_id is not None:
        q = q.where(ParsedGroup.source_task_id == task_id)
    q = q.limit(limit)
    rows = db.execute(q).scalars().all()
    return PlainTextResponse(_export_lines_groups(rows), media_type="text/plain; charset=utf-8")


@router.get("/export/users.txt")
def export_users_txt(
    task_id: Optional[int] = Query(None),
    limit: int = Query(50_000, ge=1, le=100_000),
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    q = select(ParsedUser).order_by(ParsedUser.id.asc())
    if task_id is not None:
        q = q.where(ParsedUser.source_task_id == task_id)
    q = q.limit(limit)
    rows = db.execute(q).scalars().all()
    return PlainTextResponse(_export_lines_users(rows), media_type="text/plain; charset=utf-8")


@router.get("/stats/summary")
def parsing_stats(
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    """Сводка для UI: количества сущностей и задач по статусам."""
    ch = db.scalar(select(func.count()).select_from(ParsedChannel)) or 0
    gr = db.scalar(select(func.count()).select_from(ParsedGroup)) or 0
    us = db.scalar(select(func.count()).select_from(ParsedUser)) or 0
    by_status = dict(
        db.execute(
            select(ParsingTask.status, func.count())
            .group_by(ParsingTask.status)
        ).all()
    )
    return {
        "parsed_channels": int(ch),
        "parsed_groups": int(gr),
        "parsed_users": int(us),
        "tasks_by_status": {str(k): int(v) for k, v in by_status.items()},
    }


@router.get("/stream")
async def parsing_stream(
    request: Request,
    token: Optional[str] = Query(default=None),
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
    _resolve_user_from_jwt(raw_token, cp_db)

    async def gen() -> AsyncGenerator[bytes, None]:
        last_log_id = 0
        last_tasks_sig = ""
        yield _sse_event("hello", {"ts": datetime.utcnow().isoformat()})
        while True:
            if await request.is_disconnected():
                break
            try:
                with BotSession() as bot_db:
                    rows = (
                        bot_db.execute(
                            select(
                                ParsingTask.id,
                                ParsingTask.status,
                                ParsingTask.progress_percent,
                                ParsingTask.current_stage,
                                ParsingTask.current_account_id,
                                ParsingTask.current_query,
                                ParsingTask.found_count,
                                ParsingTask.filtered_count,
                                ParsingTask.error_count,
                                ParsingTask.started_at,
                                ParsingTask.finished_at,
                            )
                            .order_by(ParsingTask.id.desc())
                            .limit(120)
                        )
                    )
                    .all()
                    )
                    sig = json.dumps([tuple(r) for r in rows], default=str, ensure_ascii=False)
                    if sig != last_tasks_sig:
                        last_tasks_sig = sig
                        yield _sse_event("tasks_changed", {"count": len(rows)})

                    logs = (
                        bot_db.execute(
                            select(ParsingTaskLog)
                            .where(ParsingTaskLog.id > last_log_id)
                            .order_by(ParsingTaskLog.id.asc())
                            .limit(400)
                        )
                        .scalars()
                        .all()
                    )
                    for lg in logs:
                        last_log_id = max(last_log_id, int(lg.id))
                        yield _sse_event(
                            "log",
                            {
                                "id": int(lg.id),
                                "task_id": int(lg.task_id),
                                "level": lg.level,
                                "event": lg.event,
                                "message": lg.message,
                                "account_id": lg.account_id,
                                "created_at": lg.created_at.isoformat() if lg.created_at else None,
                            },
                        )
            except Exception as e:
                yield _sse_event("warn", {"message": f"stream_error: {e}"})
            await asyncio.sleep(1.0)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )
