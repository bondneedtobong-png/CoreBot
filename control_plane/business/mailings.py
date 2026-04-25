"""
Бизнес-API для рассылок.

Всё, что меняет состояние воркера бота (start/pause/stop), идёт через очередь
bot_commands — её поллит `workers/bot_command_consumer.py` внутри процесса
бота. Контрол-плейн только пишет команду в БД и сразу возвращает 200.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from control_plane.business.db import get_bot_db
from control_plane.business.schemas import (
    MailingActionResult,
    MailingDetail,
    MailingListItem,
    MailingPatch,
    MailingPromptIn,
    MailingPromptOut,
)
from control_plane.deps import get_current_user
from control_plane.models import User
from database.models import BotCommand, Mailing
from utils.neuro_prompts import (
    load_system_prompt,
    neuro_prompt_file_path,
    prompt_file_exists,
)


router = APIRouter(prefix="/business/mailings", tags=["business-mailings"])


# Статусы рассылок, по которым допустимы соответствующие команды.
_STARTABLE = {"DRAFT", "PENDING", "PAUSED", "COMPLETED", "CANCELLED", "ERROR"}
_PAUSABLE = {"RUNNING"}
_STOPPABLE = {"RUNNING", "PAUSED"}


def _status_str(m: Mailing) -> str:
    return getattr(m.status, "value", str(m.status)) if m.status else "draft"


def _serialize_list(m: Mailing) -> MailingListItem:
    return MailingListItem(
        id=int(m.id),
        name=m.name or f"#{m.id}",
        status=_status_str(m),
        total=int(m.total_messages or 0),
        sent=int(m.messages_sent or 0),
        failed=int(m.messages_failed or 0),
        audience_mode=m.audience_mode or "classes",
        neurochat_enabled=bool(m.neurochat_enabled),
        started_at=m.started_at,
        completed_at=m.completed_at,
        created_at=m.created_at,
    )


def _serialize_detail(m: Mailing) -> MailingDetail:
    variants: list[str] = []
    try:
        raw = json.loads(m.message_variants_json or "[]")
        if isinstance(raw, list):
            variants = [str(x) for x in raw if str(x).strip()]
    except Exception:
        variants = []
    base = _serialize_list(m).model_dump()
    base.update(
        message_text=m.message_text or "",
        message_variants=variants,
        delay_between_messages=float(m.delay_between_messages or 0.0),
        delay_between_accounts=float(m.delay_between_accounts or 0.0),
        daily_limit=int(m.daily_limit or 0),
        messages_per_batch=int(m.messages_per_batch or 0),
        batch_delay=float(m.batch_delay or 0.0),
        auto_stop_hours=(
            float(m.auto_stop_hours) if m.auto_stop_hours is not None else None
        ),
        target_group_id=int(m.target_group_id) if m.target_group_id else None,
        community_link=m.community_link,
    )
    return MailingDetail(**base)


@router.get("", response_model=list[MailingListItem])
def list_mailings(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    stmt = select(Mailing)
    if status_filter:
        # MailingStatus values are lowercase ('running', 'paused', …)
        stmt = stmt.where(Mailing.status == status_filter.lower())
    stmt = stmt.order_by(desc(Mailing.id)).limit(limit)
    rows = db.execute(stmt).scalars().all()
    return [_serialize_list(m) for m in rows]


@router.get("/{mailing_id}", response_model=MailingDetail)
def get_mailing(
    mailing_id: int,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    m = db.get(Mailing, mailing_id)
    if not m:
        raise HTTPException(status_code=404, detail="mailing not found")
    return _serialize_detail(m)


def _enqueue_command(
    db: Session, command: str, mailing_id: int, requested_by: Optional[str]
) -> int:
    row = BotCommand(
        command=command,
        args_json=json.dumps({"mailing_id": int(mailing_id)}),
        status="pending",
        requested_by=requested_by,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return int(row.id)


@router.post("/{mailing_id}/start", response_model=MailingActionResult)
def start_mailing(
    mailing_id: int,
    db: Session = Depends(get_bot_db),
    user: User = Depends(get_current_user),
):
    m = db.get(Mailing, mailing_id)
    if not m:
        raise HTTPException(status_code=404, detail="mailing not found")
    cur = _status_str(m).upper()
    if cur not in _STARTABLE and cur != "RUNNING":
        raise HTTPException(
            status_code=400,
            detail=f"cannot start: current status={cur}",
        )
    if cur == "RUNNING":
        return MailingActionResult(
            mailing_id=mailing_id,
            command="start",
            command_id=0,
            status="rejected",
            detail="already running",
        )
    cmd_id = _enqueue_command(
        db, "mailing.start", mailing_id, getattr(user, "username", None)
    )
    return MailingActionResult(
        mailing_id=mailing_id, command="start", command_id=cmd_id, status="queued"
    )


@router.post("/{mailing_id}/pause", response_model=MailingActionResult)
def pause_mailing(
    mailing_id: int,
    db: Session = Depends(get_bot_db),
    user: User = Depends(get_current_user),
):
    m = db.get(Mailing, mailing_id)
    if not m:
        raise HTTPException(status_code=404, detail="mailing not found")
    cur = _status_str(m).upper()
    if cur not in _PAUSABLE:
        raise HTTPException(
            status_code=400,
            detail=f"cannot pause: current status={cur}",
        )
    cmd_id = _enqueue_command(
        db, "mailing.pause", mailing_id, getattr(user, "username", None)
    )
    return MailingActionResult(
        mailing_id=mailing_id, command="pause", command_id=cmd_id, status="queued"
    )


@router.post("/{mailing_id}/stop", response_model=MailingActionResult)
def stop_mailing(
    mailing_id: int,
    db: Session = Depends(get_bot_db),
    user: User = Depends(get_current_user),
):
    m = db.get(Mailing, mailing_id)
    if not m:
        raise HTTPException(status_code=404, detail="mailing not found")
    cur = _status_str(m).upper()
    if cur not in _STOPPABLE:
        raise HTTPException(
            status_code=400,
            detail=f"cannot stop: current status={cur}",
        )
    cmd_id = _enqueue_command(
        db, "mailing.stop", mailing_id, getattr(user, "username", None)
    )
    return MailingActionResult(
        mailing_id=mailing_id, command="stop", command_id=cmd_id, status="queued"
    )


# ---------------------------- Edit -----------------------------


@router.patch("/{mailing_id}", response_model=MailingDetail)
def patch_mailing(
    mailing_id: int,
    payload: MailingPatch,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    m = db.get(Mailing, mailing_id)
    if not m:
        raise HTTPException(status_code=404, detail="mailing not found")

    cur = _status_str(m).upper()
    if cur == "RUNNING":
        # Безопаснее не позволять менять текст/задержки на лету — много граней.
        raise HTTPException(
            status_code=400,
            detail="cannot edit while mailing is RUNNING; pause/stop first",
        )

    if payload.name is not None:
        m.name = payload.name.strip()
    if payload.message_text is not None:
        m.message_text = payload.message_text
    if payload.message_variants is not None:
        cleaned = [str(x) for x in payload.message_variants if str(x).strip()]
        m.message_variants_json = json.dumps(cleaned, ensure_ascii=False)
    if payload.delay_between_messages is not None:
        m.delay_between_messages = float(payload.delay_between_messages)
    if payload.delay_between_accounts is not None:
        m.delay_between_accounts = float(payload.delay_between_accounts)
    if payload.daily_limit is not None:
        m.daily_limit = int(payload.daily_limit)
    if payload.messages_per_batch is not None:
        m.messages_per_batch = int(payload.messages_per_batch)
    if payload.batch_delay is not None:
        m.batch_delay = float(payload.batch_delay)
    if payload.auto_stop_hours is not None:
        m.auto_stop_hours = float(payload.auto_stop_hours) if payload.auto_stop_hours > 0 else None
    if payload.target_group_id is not None:
        m.target_group_id = (
            int(payload.target_group_id) if payload.target_group_id > 0 else None
        )
    if payload.community_link is not None:
        m.community_link = payload.community_link.strip() or None
    if payload.neurochat_enabled is not None:
        m.neurochat_enabled = bool(payload.neurochat_enabled)
    if payload.neuro_model is not None:
        m.neuro_model = payload.neuro_model.strip() or None
    if payload.neuro_sampling_json is not None:
        # Валидируем как JSON-объект; если пусто — считаем «{}».
        raw = payload.neuro_sampling_json.strip() or "{}"
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("must be json object")
        except Exception as e:
            raise HTTPException(
                status_code=400, detail=f"neuro_sampling_json invalid: {e}"
            )
        m.neuro_sampling_json = raw
    if payload.audience_mode is not None:
        m.audience_mode = payload.audience_mode

    if hasattr(m, "updated_at"):
        try:
            m.updated_at = datetime.utcnow()
        except Exception:
            pass

    db.commit()
    db.refresh(m)
    return _serialize_detail(m)


# ---------------------------- System prompt -----------------------------


@router.get("/{mailing_id}/prompt", response_model=MailingPromptOut)
def get_mailing_prompt(
    mailing_id: int,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    m = db.get(Mailing, mailing_id)
    if not m:
        raise HTTPException(status_code=404, detail="mailing not found")
    text = load_system_prompt(int(mailing_id))
    return MailingPromptOut(
        mailing_id=int(mailing_id),
        text=text,
        has_custom_file=prompt_file_exists(int(mailing_id)),
    )


@router.put("/{mailing_id}/prompt", response_model=MailingPromptOut)
def put_mailing_prompt(
    mailing_id: int,
    payload: MailingPromptIn,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    m = db.get(Mailing, mailing_id)
    if not m:
        raise HTTPException(status_code=404, detail="mailing not found")
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty prompt")
    path = neuro_prompt_file_path(int(mailing_id))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return MailingPromptOut(
        mailing_id=int(mailing_id),
        text=load_system_prompt(int(mailing_id)),
        has_custom_file=True,
    )


@router.delete(
    "/{mailing_id}/prompt",
    response_model=MailingPromptOut,
)
def delete_mailing_prompt(
    mailing_id: int,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    m = db.get(Mailing, mailing_id)
    if not m:
        raise HTTPException(status_code=404, detail="mailing not found")
    path = neuro_prompt_file_path(int(mailing_id))
    if path.exists():
        try:
            path.unlink()
        except OSError as e:
            raise HTTPException(
                status_code=500, detail=f"cannot remove prompt file: {e}"
            )
    return MailingPromptOut(
        mailing_id=int(mailing_id),
        text=load_system_prompt(int(mailing_id)),
        has_custom_file=False,
    )
