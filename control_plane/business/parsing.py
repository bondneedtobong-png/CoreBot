"""
Бизнес-API модуля парсинга Telegram (задачи, логи, результаты, экспорт).

Читает/пишет в corebot.db через sync Session (get_bot_db).
Реальное выполнение — отдельный процесс workers/parser_worker.py.
"""
from __future__ import annotations

from utils.time import utcnow_aware, utcnow_naive
import asyncio
import csv
import io
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from typing import Optional, AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, Query, status, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, case, delete, func, literal, or_, select, union_all
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask
from xlsxwriter import Workbook

from control_plane.business.db import BotSession, commit_sync, get_bot_db
from control_plane.database import get_db as get_cp_db
from control_plane.routes.stream import resolve_stream_user
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
    CatalogFolder,
    CatalogFolderEntry,
    ParsedChannel,
    ParsedGroup,
    ParsedUser,
    ParsedUserSource,
    ParsingFilterReasonCount,
    ParsingTask,
    ParsingTaskLog,
)
from database.sqlite_pragmas import run_sync_with_busy_retry
from workers.parser import filters, querygen


router = APIRouter(prefix="/business/parsing", tags=["business-parsing"])


class ParsingSourcesPreviewIn(BaseModel):
    text: str = Field(max_length=100000)


class ParsingSearchPreviewIn(BaseModel):
    kind: str = Field(pattern="^(channels|groups)$")
    params: dict = Field(default_factory=dict, max_length=50)


@router.post("/search-preview")
def preview_search_queries(
    payload: ParsingSearchPreviewIn,
    _user: User = Depends(get_current_user),
):
    result = querygen.preview_channel_or_group_queries(payload.params)
    return {**result, "error_count": len(result["errors"])}


@router.post("/sources-preview")
def preview_user_sources(
    payload: ParsingSourcesPreviewIn,
    _user: User = Depends(get_current_user),
):
    result = querygen.preview_manual_sources(payload.text)
    return {
        "count": len(result["sources"]),
        "sources": result["sources"][:50],
        "duplicates": result["duplicates"],
        "errors": result["errors"][:100],
        "error_count": len(result["errors"]),
    }


class CatalogFolderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class CatalogFolderEntryCreate(BaseModel):
    kind: str = Field(pattern="^(channel|group)$")
    telegram_id: int


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
    params = dict(payload.params or {})
    if payload.kind == "users":
        source_text = "\n".join(str(params.get(key) or "") for key in (
            "user_inputs_text", "peers_text", "manual_usernames_text",
        ))
        if len(source_text) > 100000:
            raise HTTPException(status_code=422, detail="user sources too long")
        preview = querygen.preview_manual_sources(source_text)
        if preview["errors"] or not preview["sources"]:
            raise HTTPException(status_code=422, detail={
                "message": "invalid or empty user sources",
                "errors": preview["errors"][:20],
            })
        params["user_inputs_text"] = "\n".join(preview["sources"])
        params.pop("peers_text", None)
        params.pop("manual_usernames_text", None)
        if "message_filters" in params:
            message_filters, filter_errors = filters.normalize_message_filters(
                params["message_filters"]
            )
            if filter_errors:
                raise HTTPException(status_code=422, detail={
                    "message": "invalid message filters",
                    "errors": filter_errors,
                })
            params["message_filters"] = message_filters
    elif payload.kind in ("channels", "groups"):
        preview = querygen.preview_channel_or_group_queries(params)
        if preview["errors"]:
            raise HTTPException(status_code=422, detail={
                "message": "invalid or empty search queries",
                "count": preview["count"],
                "errors": preview["errors"][:20],
            })
        raw_filters = params.get("filters")
        if raw_filters is None:
            raw_filters = {}
        if not isinstance(raw_filters, dict):
            raise HTTPException(status_code=422, detail="filters must be an object")
        size_prefix = "members" if payload.kind == "groups" else "subscribers"
        size_filters = dict(raw_filters)
        for suffix in ("min", "max"):
            key = f"{size_prefix}_{suffix}"
            value = size_filters.get(key)
            if value is None or value == "":
                size_filters[key] = None
                continue
            raw_number = str(value)
            if isinstance(value, bool) or len(raw_number) > 16 or not raw_number.isdigit():
                raise HTTPException(status_code=422, detail=f"{key} must be a non-negative integer")
            number = int(raw_number)
            if number > 2**53 - 1:
                raise HTTPException(status_code=422, detail=f"{key} is too large")
            size_filters[key] = number
        lower = size_filters[f"{size_prefix}_min"]
        upper = size_filters[f"{size_prefix}_max"]
        if lower is not None and upper is not None and lower > upper:
            raise HTTPException(status_code=422, detail=f"{size_prefix}_min exceeds max")
        params["filters"] = size_filters
        if params.get("manual_usernames_text"):
            manual = querygen.preview_manual_sources(params["manual_usernames_text"])
            params["manual_usernames_text"] = "\n".join(manual["sources"])
    label = f"{user.username} (id={user.id})"
    t = ParsingTask(
        kind=payload.kind,
        status="pending",
        params_json=params,
        accounts_json=acc_ids,
        depth=payload.depth,
        mode=payload.mode,
        requested_by=label,
    )
    db.add(t)
    commit_sync(db)
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
    t.finished_at = utcnow_naive()
    t.current_stage = "cancelled"
    t.last_error = None
    db.add(t)
    commit_sync(db)
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
    commit_sync(db)
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


@router.get("/tasks/{task_id}/filter-reasons")
def task_filter_reasons(
    task_id: int,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    if db.get(ParsingTask, task_id) is None:
        raise HTTPException(status_code=404, detail="task not found")
    rows = db.execute(select(ParsingFilterReasonCount).where(
        ParsingFilterReasonCount.task_id == task_id
    ).order_by(ParsingFilterReasonCount.count.desc(), ParsingFilterReasonCount.reason)).scalars().all()
    return [{"reason": row.reason, "count": row.count} for row in rows]


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


@router.get("/catalog/search")
def search_local_catalog(
    kind: str = Query("all", pattern="^(all|channels|groups)$"),
    q: str = Query("", max_length=100),
    lang: str | None = Query(None, max_length=16),
    min_count: int | None = Query(None, ge=0),
    max_count: int | None = Query(None, ge=0),
    active_7d: bool | None = Query(None),
    has_discussion: bool | None = Query(None),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0, le=10000),
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    """Search only locally collected public metadata; no Telegram client is opened."""
    if min_count is not None and max_count is not None and min_count > max_count:
        raise HTTPException(status_code=422, detail="min_count exceeds max_count")
    term = q.strip()
    statements = []
    if kind in ("all", "channels"):
        channels = select(
            literal("channel").label("kind"), ParsedChannel.telegram_id.label("telegram_id"),
            ParsedChannel.username.label("username"), ParsedChannel.title.label("title"),
            ParsedChannel.lang.label("lang"), ParsedChannel.subscribers.label("audience_count"),
            ParsedChannel.is_active_7d.label("active_7d"),
            ParsedChannel.has_discussion.label("has_discussion"),
            ParsedChannel.is_public.label("is_public"),
            ParsedChannel.source_task_id.label("source_task_id"),
            ParsedChannel.updated_at.label("last_seen"),
        )
        if term:
            channels = channels.where(or_(
                ParsedChannel.title.contains(term, autoescape=True),
                ParsedChannel.username.contains(term, autoescape=True),
            ))
        if lang:
            channels = channels.where(ParsedChannel.lang == lang.lower())
        if min_count is not None:
            channels = channels.where(ParsedChannel.subscribers >= min_count)
        if max_count is not None:
            channels = channels.where(ParsedChannel.subscribers <= max_count)
        if active_7d is not None:
            channels = channels.where(ParsedChannel.is_active_7d.is_(active_7d))
        if has_discussion is not None:
            channels = channels.where(ParsedChannel.has_discussion.is_(has_discussion))
        statements.append(channels)
    if kind in ("all", "groups") and has_discussion is None:
        groups = select(
            literal("group").label("kind"), ParsedGroup.telegram_id.label("telegram_id"),
            ParsedGroup.username.label("username"), ParsedGroup.title.label("title"),
            ParsedGroup.lang.label("lang"), ParsedGroup.members_count.label("audience_count"),
            ParsedGroup.is_active_7d.label("active_7d"), literal(None).label("has_discussion"),
            case((ParsedGroup.group_type == "public", True), else_=False).label("is_public"),
            ParsedGroup.source_task_id.label("source_task_id"),
            ParsedGroup.updated_at.label("last_seen"),
        )
        if term:
            groups = groups.where(or_(
                ParsedGroup.title.contains(term, autoescape=True),
                ParsedGroup.username.contains(term, autoescape=True),
            ))
        if lang:
            groups = groups.where(ParsedGroup.lang == lang.lower())
        if min_count is not None:
            groups = groups.where(ParsedGroup.members_count >= min_count)
        if max_count is not None:
            groups = groups.where(ParsedGroup.members_count <= max_count)
        if active_7d is not None:
            groups = groups.where(ParsedGroup.is_active_7d.is_(active_7d))
        statements.append(groups)
    if not statements:
        return {"total": 0, "items": []}
    source = (union_all(*statements) if len(statements) > 1 else statements[0]).subquery()
    total = int(db.execute(select(func.count()).select_from(source)).scalar_one())
    rows = db.execute(
        select(source).order_by(source.c.last_seen.desc(), source.c.telegram_id).offset(offset).limit(limit)
    ).mappings().all()
    return {
        "total": total,
        "items": [
            {**dict(row), "link": f"https://t.me/{row['username']}" if row["username"] else None}
            for row in rows
        ],
    }


def _catalog_words(value: str | None) -> set[str]:
    return set(re.findall(r"[^\W\d_]{3,}", (value or "").casefold(), flags=re.UNICODE))


@router.get("/catalog/similar")
def similar_local_catalog(
    kind: str = Query(..., pattern="^(channel|group)$"),
    telegram_id: int = Query(...),
    limit: int = Query(20, ge=1, le=50),
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    """Explainable title similarity over cached rows only, not a Telegram search."""
    model = ParsedChannel if kind == "channel" else ParsedGroup
    seed = db.execute(select(model).where(model.telegram_id == telegram_id)).scalar_one_or_none()
    if seed is None:
        raise HTTPException(status_code=404, detail="catalog entry not found")
    words = _catalog_words(seed.title)
    if not words:
        return {"seed_id": telegram_id, "kind": kind, "items": [], "method": "title_tokens"}
    query = select(model).where(model.telegram_id != telegram_id)
    if seed.lang:
        query = query.where(model.lang == seed.lang)
    # Bounded local scan; recent entries win a tie but the score is title based.
    candidates = db.execute(query.order_by(model.updated_at.desc()).limit(2000)).scalars().all()
    scored = []
    for row in candidates:
        common = words & _catalog_words(row.title)
        if not common:
            continue
        union = words | _catalog_words(row.title)
        score = round(100 * len(common) / len(union))
        audience_count = row.subscribers if kind == "channel" else row.members_count
        scored.append({
            "kind": kind,
            "telegram_id": row.telegram_id,
            "title": row.title,
            "username": row.username,
            "lang": row.lang,
            "audience_count": audience_count,
            "source_task_id": row.source_task_id,
            "last_seen": row.updated_at,
            "score": score,
            "shared_title_words": sorted(common),
            "link": f"https://t.me/{row.username}" if row.username else None,
        })
    scored.sort(key=lambda item: (-item["score"], -len(item["shared_title_words"]), item["telegram_id"]))
    return {
        "seed_id": telegram_id,
        "kind": kind,
        "method": "title_tokens",
        "candidate_limit": 2000,
        "items": scored[:limit],
    }


@router.get("/catalog/folders")
def list_catalog_folders(
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    rows = db.execute(
        select(CatalogFolder.id, CatalogFolder.name, CatalogFolder.created_at,
               func.count(CatalogFolderEntry.id).label("entry_count"))
        .outerjoin(CatalogFolderEntry, CatalogFolderEntry.folder_id == CatalogFolder.id)
        .group_by(CatalogFolder.id)
        .order_by(CatalogFolder.name)
    ).all()
    return [{"id": rid, "name": name, "created_at": created,
             "entry_count": count} for rid, name, created, count in rows]


@router.post("/catalog/folders", status_code=status.HTTP_201_CREATED)
def create_catalog_folder(
    payload: CatalogFolderCreate,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(require_operator_write),
):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="folder name is blank")
    inserted = run_sync_with_busy_retry(
        lambda: db.execute(
            sqlite_insert(CatalogFolder).values(name=name).on_conflict_do_nothing(
                index_elements=[CatalogFolder.name],
            )
        ), op_name="catalog-folder-insert",
    )
    if inserted.rowcount == 0:
        db.rollback()
        raise HTTPException(status_code=409, detail="folder name already exists")
    commit_sync(db)
    row = db.get(CatalogFolder, inserted.inserted_primary_key[0])
    return {"id": row.id, "name": row.name, "entry_count": 0}


@router.get("/catalog/folders/{folder_id}")
def get_catalog_folder(
    folder_id: int,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    folder = db.get(CatalogFolder, folder_id)
    if folder is None:
        raise HTTPException(status_code=404, detail="folder not found")
    entries = db.execute(
        select(CatalogFolderEntry)
        .where(CatalogFolderEntry.folder_id == folder_id)
        .order_by(CatalogFolderEntry.id)
        .limit(500)
    ).scalars().all()
    channel_ids = [row.telegram_id for row in entries if row.kind == "channel"]
    group_ids = [row.telegram_id for row in entries if row.kind == "group"]
    channels = {row.telegram_id: row for row in db.execute(
        select(ParsedChannel).where(ParsedChannel.telegram_id.in_(channel_ids))
    ).scalars()}
    groups = {row.telegram_id: row for row in db.execute(
        select(ParsedGroup).where(ParsedGroup.telegram_id.in_(group_ids))
    ).scalars()}
    items = []
    for entry in entries:
        venue = (channels if entry.kind == "channel" else groups).get(entry.telegram_id)
        items.append({
            "kind": entry.kind,
            "telegram_id": entry.telegram_id,
            "title": venue.title if venue else None,
            "username": venue.username if venue else None,
            "lang": venue.lang if venue else None,
            "audience_count": (
                venue.subscribers if entry.kind == "channel" else venue.members_count
            ) if venue else None,
            "last_seen": venue.updated_at if venue else None,
            "added_at": entry.added_at,
            "available": venue is not None,
        })
    return {"id": folder.id, "name": folder.name, "items": items}


def _spreadsheet_cell(value) -> str:
    """Prevent titles/usernames from becoming formulas in spreadsheet apps."""
    text = "" if value is None else str(value)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


@router.get("/catalog/folders/{folder_id}/export.csv")
def export_catalog_folder_csv(
    folder_id: int,
    db: Session = Depends(get_bot_db),
    user: User = Depends(get_current_user),
):
    folder = get_catalog_folder(folder_id, db, user)
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow([
        "kind", "telegram_id", "username", "title", "lang", "audience_count",
        "last_seen", "added_at", "available",
    ])
    for item in folder["items"]:
        writer.writerow([_spreadsheet_cell(item.get(key)) for key in (
            "kind", "telegram_id", "username", "title", "lang", "audience_count",
            "last_seen", "added_at", "available",
        )])
    return PlainTextResponse(
        "\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="catalog-folder-{folder_id}.csv"'},
    )


@router.post("/catalog/folders/{folder_id}/entries")
def add_catalog_folder_entry(
    folder_id: int,
    payload: CatalogFolderEntryCreate,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(require_operator_write),
):
    if db.get(CatalogFolder, folder_id) is None:
        raise HTTPException(status_code=404, detail="folder not found")
    model = ParsedChannel if payload.kind == "channel" else ParsedGroup
    venue = db.execute(
        select(model.id).where(model.telegram_id == payload.telegram_id)
    ).scalar_one_or_none()
    if venue is None:
        raise HTTPException(status_code=404, detail="catalog entry not found")
    existing = db.execute(
        select(CatalogFolderEntry).where(
            CatalogFolderEntry.folder_id == folder_id,
            CatalogFolderEntry.kind == payload.kind,
            CatalogFolderEntry.telegram_id == payload.telegram_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return {"id": existing.id, "added": False}
    count = db.execute(select(func.count()).select_from(CatalogFolderEntry).where(
        CatalogFolderEntry.folder_id == folder_id,
    )).scalar_one()
    if count >= 500:
        raise HTTPException(status_code=409, detail="folder limit reached")
    inserted = run_sync_with_busy_retry(
        lambda: db.execute(
            sqlite_insert(CatalogFolderEntry).values(
                folder_id=folder_id, kind=payload.kind, telegram_id=payload.telegram_id,
            ).on_conflict_do_nothing(index_elements=[
                CatalogFolderEntry.folder_id,
                CatalogFolderEntry.kind,
                CatalogFolderEntry.telegram_id,
            ])
        ), op_name="catalog-folder-entry-insert",
    )
    if inserted.rowcount == 0:
        db.rollback()
        raise HTTPException(status_code=409, detail="entry already exists")
    commit_sync(db)
    row = db.get(CatalogFolderEntry, inserted.inserted_primary_key[0])
    return {"id": row.id, "added": True}


@router.delete("/catalog/folders/{folder_id}/entries/{kind}/{telegram_id}")
def remove_catalog_folder_entry(
    folder_id: int,
    kind: str,
    telegram_id: int,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(require_operator_write),
):
    if kind not in ("channel", "group"):
        raise HTTPException(status_code=422, detail="invalid catalog kind")
    if db.get(CatalogFolder, folder_id) is None:
        raise HTTPException(status_code=404, detail="folder not found")
    result = db.execute(delete(CatalogFolderEntry).where(
        CatalogFolderEntry.folder_id == folder_id,
        CatalogFolderEntry.kind == kind,
        CatalogFolderEntry.telegram_id == telegram_id,
    ))
    commit_sync(db)
    return {"removed": bool(result.rowcount)}


@router.delete("/catalog/folders/{folder_id}")
def delete_catalog_folder(
    folder_id: int,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(require_operator_write),
):
    if db.get(CatalogFolder, folder_id) is None:
        raise HTTPException(status_code=404, detail="folder not found")
    db.execute(delete(CatalogFolderEntry).where(CatalogFolderEntry.folder_id == folder_id))
    db.execute(delete(CatalogFolder).where(CatalogFolder.id == folder_id))
    commit_sync(db)
    return {"deleted": True}


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
        source_for_task = select(ParsedUserSource.id).where(
            ParsedUserSource.parsed_user_id == ParsedUser.id,
            ParsedUserSource.source_task_id == task_id,
        ).exists()
        q = q.where(or_(ParsedUser.source_task_id == task_id, source_for_task))
    q = q.offset(offset).limit(limit)
    rows = db.execute(q).scalars().all()
    return _rows_users(rows)


@router.get("/results/users/{user_id}/sources")
def user_source_evidence(
    user_id: int,
    task_id: Optional[int] = Query(None),
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    if db.get(ParsedUser, user_id) is None:
        raise HTTPException(status_code=404, detail="parsed user not found")
    q = select(ParsedUserSource).where(
        ParsedUserSource.parsed_user_id == user_id
    )
    if task_id is not None:
        q = q.where(ParsedUserSource.source_task_id == task_id)
    rows = db.execute(q.order_by(
        ParsedUserSource.observed_at.desc(), ParsedUserSource.id.desc()
    ).limit(100)).scalars().all()
    return [{
        "source_entity_id": int(row.source_entity_id),
        "source_entity_kind": row.source_entity_kind,
        "source_kind": row.source_kind,
        "source_task_id": row.source_task_id,
        "message_id": row.message_id,
        "post_id": row.post_id,
        "message_at": row.message_at,
        "observed_at": row.observed_at or row.created_at,
    } for row in rows]


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
        source_for_task = select(ParsedUserSource.id).where(
            ParsedUserSource.parsed_user_id == ParsedUser.id,
            ParsedUserSource.source_task_id == task_id,
        ).exists()
        q = q.where(or_(ParsedUser.source_task_id == task_id, source_for_task))
    q = q.limit(limit)
    rows = db.execute(q).scalars().all()
    return PlainTextResponse(_export_lines_users(rows), media_type="text/plain; charset=utf-8")


_EXPORT_FIELDS = {
    "channels": (
        "telegram_id", "username", "title", "lang", "audience_count",
        "is_active_7d", "is_public", "has_discussion", "last_post_at",
        "source_task_id", "updated_at",
    ),
    "groups": (
        "telegram_id", "username", "title", "lang", "audience_count",
        "is_active_7d", "group_type", "source_task_id", "updated_at",
    ),
    "users": (
        "telegram_id", "username", "display_name", "has_avatar",
        "last_seen_at", "lang_guess", "is_deleted", "is_suspicious",
        "source_entity_id", "source_entity_kind", "source_kind", "message_id",
        "post_id", "message_at", "observed_at", "source_task_id", "updated_at",
    ),
}
_XLSX_MAX_ROWS = 50_000
_XLSX_MAX_BYTES = 25 * 1024 * 1024
_XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _export_result_rows(kind: str, task_id: int | None, limit: int, db: Session):
    """Yield the same task-filtered rows to CSV and XLSX without loading the result set."""
    if kind in ("channels", "groups"):
        model = ParsedChannel if kind == "channels" else ParsedGroup
        query = select(model).order_by(model.id).limit(limit)
        if task_id is not None:
            query = query.where(model.source_task_id == task_id)
        for row in db.execute(query.execution_options(yield_per=500)).scalars():
            values = {
                "telegram_id": row.telegram_id,
                "username": row.username,
                "title": row.title,
                "lang": row.lang,
                "audience_count": row.subscribers if kind == "channels" else row.members_count,
                "is_active_7d": row.is_active_7d,
                "source_task_id": row.source_task_id,
                "updated_at": row.updated_at,
            }
            if kind == "channels":
                values.update(is_public=row.is_public, has_discussion=row.has_discussion,
                              last_post_at=row.last_post_at)
            else:
                values["group_type"] = row.group_type
            yield values
    elif kind == "users":
        query = (select(ParsedUser, ParsedUserSource)
                 .outerjoin(ParsedUserSource, ParsedUserSource.parsed_user_id == ParsedUser.id)
                 .order_by(ParsedUser.id, ParsedUserSource.id).limit(limit))
        if task_id is not None:
            query = query.where(or_(
                ParsedUserSource.source_task_id == task_id,
                and_(ParsedUser.source_task_id == task_id, ParsedUserSource.id.is_(None)),
            ))
        for user, source in db.execute(query.execution_options(yield_per=500)):
            yield {
                "telegram_id": user.telegram_id,
                "username": user.username,
                "display_name": user.display_name,
                "has_avatar": user.has_avatar,
                "last_seen_at": user.last_seen_at,
                "lang_guess": user.lang_guess,
                "is_deleted": user.is_deleted,
                "is_suspicious": user.is_suspicious,
                "source_entity_id": source.source_entity_id if source else None,
                "source_entity_kind": source.source_entity_kind if source else None,
                "source_kind": source.source_kind if source else None,
                "message_id": source.message_id if source else None,
                "post_id": source.post_id if source else None,
                "message_at": source.message_at if source else None,
                "observed_at": source.observed_at if source else None,
                "source_task_id": source.source_task_id if source else user.source_task_id,
                "updated_at": user.updated_at,
            }


@router.get("/export/{kind}.csv")
def export_results_csv(
    kind: str,
    task_id: Optional[int] = Query(None),
    limit: int = Query(50_000, ge=1, le=100_000),
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    """Excel-readable result export; each user source gets its own evidence row."""
    fields = _EXPORT_FIELDS.get(kind)
    if fields is None:
        raise HTTPException(status_code=404, detail="unknown export kind")
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(fields)
    for values in _export_result_rows(kind, task_id, limit, db):
        writer.writerow([_spreadsheet_cell(values[field]) for field in fields])
    return PlainTextResponse(
        "\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="parsed-{kind}.csv"'},
    )


def _write_xlsx_value(worksheet, row: int, col: int, field: str, value, date_format):
    """Write literal text explicitly so untrusted titles cannot become formulas."""
    if value is None:
        return
    if field in ("telegram_id", "source_entity_id"):
        worksheet.write_string(row, col, str(value))
    elif isinstance(value, bool):
        worksheet.write_boolean(row, col, value)
    elif isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        worksheet.write_datetime(row, col, value, date_format)
    elif isinstance(value, (int, float)):
        worksheet.write_number(row, col, value)
    else:
        # write_string is literal even for =, +, -, @ and URL prefixes.
        worksheet.write_string(row, col, str(value)[:32767])


@router.get("/export/{kind}.xlsx")
def export_results_xlsx(
    kind: str,
    task_id: Optional[int] = Query(None),
    limit: int = Query(_XLSX_MAX_ROWS, ge=1, le=_XLSX_MAX_ROWS),
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    """Bounded-memory workbook with one evidence row per user source."""
    fields = _EXPORT_FIELDS.get(kind)
    if fields is None:
        raise HTTPException(status_code=404, detail="unknown export kind")
    descriptor, path = tempfile.mkstemp(prefix="corebot-parsed-", suffix=".xlsx")
    os.close(descriptor)
    workbook = None
    workbook_open = False
    try:
        workbook = Workbook(path, {
            "constant_memory": True,
            "strings_to_formulas": False,
            "strings_to_urls": False,
        })
        workbook_open = True
        worksheet = workbook.add_worksheet(kind.capitalize())
        header_format = workbook.add_format({"bold": True, "bg_color": "#E8EEF7"})
        date_format = workbook.add_format({"num_format": "yyyy-mm-dd hh:mm:ss"})
        worksheet.freeze_panes(1, 0)
        worksheet.set_column(0, len(fields) - 1, 18)
        for col, field in enumerate(fields):
            worksheet.write_string(0, col, field, header_format)
        row_number = 0
        for row_number, values in enumerate(_export_result_rows(kind, task_id, limit, db), start=1):
            for col, field in enumerate(fields):
                _write_xlsx_value(worksheet, row_number, col, field, values[field], date_format)
        worksheet.autofilter(0, 0, row_number, len(fields) - 1)
        workbook.close()
        workbook_open = False
        if os.path.getsize(path) > _XLSX_MAX_BYTES:
            raise HTTPException(status_code=413, detail="XLSX export exceeds 25 MiB; reduce limit")
    except Exception:
        if workbook_open:
            try:
                workbook.close()
            except Exception:
                pass
        if os.path.exists(path):
            os.unlink(path)
        raise
    return FileResponse(
        path,
        media_type=_XLSX_MEDIA_TYPE,
        filename=f"parsed-{kind}.xlsx",
        background=BackgroundTask(os.unlink, path),
    )


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
    cp_db: Session = Depends(get_cp_db),
):
    resolve_stream_user(request, cp_db)

    async def gen() -> AsyncGenerator[bytes, None]:
        last_log_id = 0
        last_tasks_sig = ""
        yield _sse_event("hello", {"ts": utcnow_aware().isoformat()})
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
                        ).all()
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
