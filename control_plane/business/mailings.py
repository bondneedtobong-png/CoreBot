"""
Бизнес-API для рассылок.

Всё, что меняет состояние воркера бота (start/pause/stop), идёт через очередь
bot_commands — её поллит `workers/bot_command_consumer.py` внутри процесса
бота. Контрол-плейн только пишет команду в БД и сразу возвращает 200.
"""

from __future__ import annotations

import asyncio
import json
import hashlib
import re
from datetime import timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from utils.time import utcnow_naive
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response, status
from sqlalchemy import desc, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from control_plane.business.db import commit_sync, get_bot_db
from control_plane.business.schemas import (
    MailingActionResult,
    MailingStartIn,
    MailingCreate,
    MailingDetail,
    MailingListItem,
    MailingPatch,
    MailingPromptIn,
    MailingPromptExpectedIn,
    MailingPromptOut,
    NeuroPromptCompareIn,
    NeuroKnowledgeCreate,
    NeuroKnowledgePatch,
    MailingTestRecipientsIn,
    MailingTestRecipientsOut,
)
from control_plane.deps import get_current_user, require_operator_write
from control_plane.models import User
from database.models import (
    Account,
    BotCommand,
    Client,
    InstanceSettings,
    Mailing,
    MailingRun,
    MailingRunRecipient,
    MailingStatus,
    MailingTestRecipient,
    NeuroKnowledgeEntry,
)
from database.sqlite_pragmas import run_sync_with_busy_retry
from database.repositories import ClientRepository, MailingRepository
from services.neurochat.llm_service import generate_reply_with_retries_and_fallback
from services.neurochat.knowledge import entries_sync, keywords_for, reference_text
from services.neurochat.provider_registry import generation_for_provider, resolve_provider_sync
from services.neurochat.prompt_history import (
    PromptConflict,
    get_version,
    list_versions,
    prepared_text,
    prompt_state,
    save_version,
    version_metadata,
)
from utils.links import normalize_public_link
from utils.neuro_prompts import (
    apply_neuro_prompt_placeholders,
    prepare_system_prompt_text,
)
from utils.neuro_sampling import parse_sampling_mailing_column


router = APIRouter(prefix="/business/mailings", tags=["business-mailings"])


# Статусы рассылок, по которым допустимы соответствующие команды.
_STARTABLE = {"DRAFT", "PENDING", "PAUSED", "COMPLETED", "CANCELLED", "ERROR"}
_PAUSABLE = {"RUNNING"}
_STOPPABLE = {"RUNNING", "PAUSED"}


def _status_str(m: Mailing) -> str:
    return getattr(m.status, "value", str(m.status)) if m.status else "draft"


_DEFAULT_AUDIENCE = {
    "client_status": "new",
    "include_classes": [],
    "exclude_classes": ["bl"],
}

_USERNAME_RE = re.compile(r"^[a-z0-9_]{5,32}$")


def _parse_audience(raw: object) -> dict:
    try:
        data = json.loads(raw) if isinstance(raw, str) else {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    out = dict(_DEFAULT_AUDIENCE)
    if data.get("client_status") in ("new", "open"):
        out["client_status"] = data["client_status"]
    for key in ("include_classes", "exclude_classes"):
        vals = data.get(key)
        if isinstance(vals, list):
            out[key] = [str(x).strip() for x in vals if str(x).strip()][:50]
    return out


def _dump_audience(data: dict) -> str:
    return json.dumps(
        {
            "client_status": data.get("client_status", "new"),
            "include_classes": data.get("include_classes", []),
            "exclude_classes": data.get("exclude_classes", []),
        },
        ensure_ascii=False,
    )


def _normalize_username(raw: str) -> str | None:
    """Юзернейм как в боте: без @, lower, 5–32 [a-z0-9_], без telegram*/bot*."""
    s = (raw or "").strip().lstrip("@").lower()
    if not _USERNAME_RE.match(s):
        return None
    if s.startswith("telegram") or s.startswith("bot"):
        return None
    return s


def _serialize_list(m: Mailing, *, queued_start: bool = False) -> MailingListItem:
    return MailingListItem(
        id=int(m.id),
        name=m.name or f"#{m.id}",
        status=_status_str(m),
        total=int(m.total_messages or 0),
        sent=int(m.messages_sent or 0),
        failed=int(m.messages_failed or 0),
        audience_mode=m.audience_mode or "classes",
        neurochat_enabled=bool(m.neurochat_enabled),
        neuro_active_start_minute=m.neuro_active_start_minute,
        neuro_active_end_minute=m.neuro_active_end_minute,
        neuro_timezone=m.neuro_timezone or "UTC",
        queued_start=queued_start,
        started_at=m.started_at,
        completed_at=m.completed_at,
        created_at=m.created_at,
    )


def _serialize_detail(m: Mailing, *, queued_start: bool = False) -> MailingDetail:
    variants: list[str] = []
    try:
        raw = json.loads(m.message_variants_json or "[]")
        if isinstance(raw, list):
            variants = [str(x) for x in raw if str(x).strip()]
    except Exception:
        variants = []
    base = _serialize_list(m, queued_start=queued_start).model_dump()
    aud = _parse_audience(getattr(m, "audience_filter_json", None))
    base.update(
        message_text=m.message_text or "",
        message_variants=variants,
        variant_mode=(getattr(m, "variant_mode", None) or "random"),
        use_typing=bool(getattr(m, "use_typing", True)),
        smart_delay=bool(getattr(m, "smart_delay", False)),
        delay_between_messages=float(m.delay_between_messages or 0.0),
        delay_between_accounts=float(m.delay_between_accounts or 0.0),
        daily_limit=int(m.daily_limit or 0),
        neuro_daily_reply_limit=int(m.neuro_daily_reply_limit or 0),
        messages_per_batch=int(m.messages_per_batch or 0),
        batch_delay=float(m.batch_delay or 0.0),
        max_recipients=(
            int(m.max_recipients) if getattr(m, "max_recipients", None) else None
        ),
        mailing_cooldown_hours=float(
            getattr(m, "mailing_cooldown_hours", None) or 12.0
        ),
        auto_stop_hours=(
            float(m.auto_stop_hours) if m.auto_stop_hours is not None else None
        ),
        target_group_id=int(m.target_group_id) if m.target_group_id else None,
        community_link=m.community_link,
        audience_client_status=aud["client_status"],
        audience_include_classes=aud["include_classes"],
        audience_exclude_classes=aud["exclude_classes"],
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
    queued_ids = set(db.execute(select(MailingRun.mailing_id).where(
        MailingRun.status == "queued"
    )).scalars())
    return [_serialize_list(m, queued_start=m.id in queued_ids) for m in rows]


@router.get("/{mailing_id}", response_model=MailingDetail)
def get_mailing(
    mailing_id: int,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    m = db.get(Mailing, mailing_id)
    if not m:
        raise HTTPException(status_code=404, detail="mailing not found")
    queued = db.execute(select(MailingRun.id).where(
        MailingRun.mailing_id == mailing_id, MailingRun.status == "queued"
    ).limit(1)).first() is not None
    return _serialize_detail(m, queued_start=queued)


@router.post("", response_model=MailingDetail, status_code=status.HTTP_201_CREATED)
def create_mailing(
    payload: MailingCreate,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(require_operator_write),
):
    target_group_id = None
    if payload.target_group_id is not None and int(payload.target_group_id) > 0:
        target_group_id = int(payload.target_group_id)

    settings = db.get(InstanceSettings, 1)
    default_provider_id = settings.default_ai_provider_id if settings else None
    neurochat_enabled = (
        bool(payload.neurochat_enabled)
        if "neurochat_enabled" in payload.model_fields_set
        else bool(settings.mailing_neurochat_default) if settings else False
    )
    row = Mailing(
        name=(payload.name or "").strip(),
        message_text=(payload.message_text or "").strip(),
        status=MailingStatus.DRAFT,
        audience_mode=(payload.audience_mode or "classes"),
        target_group_id=target_group_id,
        neurochat_enabled=neurochat_enabled,
        neuro_provider_id=default_provider_id,
    )
    db.add(row)
    db.flush()
    from utils.neuro_prompts import archive_orphan_prompt

    archive_orphan_prompt(row.id, database_url=str(db.get_bind().url))
    commit_sync(db)
    db.refresh(row)
    return _serialize_detail(row)


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
    run_sync_with_busy_retry(db.commit, op_name="botcmd-enqueue")
    db.refresh(row)
    return int(row.id)


def _audience_query(mailing: Mailing):
    mode = (mailing.audience_mode or "classes").strip().lower()
    if mode == "test":
        return ClientRepository.test_audience_query(mailing.id)
    audience = ClientRepository.parse_mailing_audience(mailing)
    if mode == "new":
        audience["client_status"] = "new"
        audience["include_classes"] = []
    return ClientRepository.mailing_audience_query(audience, mailing_id=mailing.id)


def _audience_preview(db: Session, mailing: Mailing) -> dict:
    query = _audience_query(mailing)
    count = int(db.execute(select(func.count()).select_from(query.subquery())).scalar_one())
    sample = db.execute(query.limit(10)).scalars().all()
    return {
        "mailing_id": mailing.id,
        "audience_mode": mailing.audience_mode,
        "eligible_count": count,
        "sample": [{"client_id": row.id, "username": row.username} for row in sample],
        "permission_required": True,
    }


@router.get("/{mailing_id}/audience-preview")
def preview_mailing_audience(
    mailing_id: int,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    mailing = db.get(Mailing, mailing_id)
    if mailing is None:
        raise HTTPException(status_code=404, detail="mailing not found")
    return _audience_preview(db, mailing)


@router.get("/{mailing_id}/runs")
def list_mailing_runs(
    mailing_id: int,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    if db.get(Mailing, mailing_id) is None:
        raise HTTPException(status_code=404, detail="mailing not found")
    rows = db.execute(
        select(MailingRun)
        .where(MailingRun.mailing_id == mailing_id)
        .order_by(MailingRun.id.desc())
        .limit(50)
    ).scalars().all()
    return [{
        "run_id": row.id,
        "status": row.status,
        "scheduled_at": row.scheduled_at,
        "audience_mode": row.audience_mode,
        "audience_count": row.audience_count,
        "messages_sent": row.messages_sent,
        "messages_failed": row.messages_failed,
        "config_sha256": row.config_sha256,
        "created_at": row.created_at,
        "finished_at": row.finished_at,
    } for row in rows]


@router.get("/{mailing_id}/runs/{run_id}")
def get_mailing_run(
    mailing_id: int,
    run_id: int,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    row = db.get(MailingRun, run_id)
    if row is None or row.mailing_id != mailing_id:
        raise HTTPException(status_code=404, detail="mailing run not found")
    sample = db.execute(
        select(Client.id, Client.username)
        .join(MailingRunRecipient, MailingRunRecipient.client_id == Client.id)
        .where(MailingRunRecipient.run_id == run_id)
        .order_by(Client.id)
        .limit(20)
    ).all()
    return {
        "run_id": row.id,
        "mailing_id": row.mailing_id,
        "status": row.status,
        "scheduled_at": row.scheduled_at,
        "audience_mode": row.audience_mode,
        "audience_count": row.audience_count,
        "messages_sent": row.messages_sent,
        "messages_failed": row.messages_failed,
        "config_sha256": row.config_sha256,
        "created_at": row.created_at,
        "finished_at": row.finished_at,
        "sample": [{"client_id": cid, "username": username} for cid, username in sample],
    }


@router.post("/{mailing_id}/start", response_model=MailingActionResult)
def start_mailing(
    mailing_id: int,
    payload: MailingStartIn | None = None,
    db: Session = Depends(get_bot_db),
    user: User = Depends(require_operator_write),
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
    if db.execute(select(MailingRun.id).where(
        MailingRun.mailing_id == mailing_id, MailingRun.status == "queued"
    ).limit(1)).first():
        raise HTTPException(status_code=409, detail="mailing start already queued")
    if _audience_preview(db, m)["eligible_count"] == 0:
        raise HTTPException(
            status_code=409,
            detail="no eligible recipients; verify contact permission or choose test recipients",
        )
    scheduled_at = None
    if payload is not None and payload.scheduled_at is not None:
        value = payload.scheduled_at
        if value.tzinfo is None or value.utcoffset() is None:
            raise HTTPException(status_code=400, detail="scheduled_at must include timezone")
        scheduled_at = value.astimezone(timezone.utc).replace(tzinfo=None)
        if scheduled_at <= utcnow_naive():
            raise HTTPException(status_code=400, detail="scheduled_at must be in the future")
    # Freeze the previewed audience in the same transaction as the command.
    # A later opt-out still excludes that recipient at delivery time.
    recipient_ids = list(db.execute(_audience_query(m).with_only_columns(Client.id)).scalars())
    config_json = MailingRepository.run_config_json(m)
    run = MailingRun(
        mailing_id=mailing_id,
        audience_mode=(m.audience_mode or "classes").strip().lower(),
        config_json=config_json,
        config_sha256=hashlib.sha256(config_json.encode("utf-8")).hexdigest(),
        audience_count=len(recipient_ids),
        status="queued",
        scheduled_at=scheduled_at,
    )
    try:
        db.add(run)
        db.flush()
        if recipient_ids:
            db.execute(insert(MailingRunRecipient), [
                {"run_id": run.id, "client_id": client_id} for client_id in recipient_ids
            ])
        command = BotCommand(
            command="mailing.start",
            args_json=json.dumps({"mailing_id": mailing_id, "run_id": run.id}),
            status="pending",
            requested_by=getattr(user, "username", None),
            not_before=scheduled_at,
        )
        db.add(command)
        commit_sync(db, op_name="mailing-start-enqueue")
    except IntegrityError as exc:
        db.rollback()
        if "UNIQUE constraint failed: mailing_runs.mailing_id" not in str(exc.orig):
            raise
        raise HTTPException(status_code=409, detail="mailing start already queued") from exc
    cmd_id = int(command.id)
    return MailingActionResult(
        mailing_id=mailing_id, command="start", command_id=cmd_id,
        status="queued",
        detail=(f"scheduled for {scheduled_at.isoformat()} UTC" if scheduled_at else None),
    )


@router.post("/{mailing_id}/pause", response_model=MailingActionResult)
def pause_mailing(
    mailing_id: int,
    db: Session = Depends(get_bot_db),
    user: User = Depends(require_operator_write),
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


@router.post("/{mailing_id}/cancel-start", response_model=MailingActionResult)
def cancel_queued_mailing_start(
    mailing_id: int,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(require_operator_write),
):
    """Cancel an unclaimed start command and its frozen audience together."""
    run = db.execute(select(MailingRun).where(
        MailingRun.mailing_id == mailing_id, MailingRun.status == "queued"
    ).order_by(MailingRun.id.desc()).limit(1)).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=409, detail="no queued mailing start")
    commands = db.execute(select(BotCommand).where(
        BotCommand.command == "mailing.start", BotCommand.status == "pending"
    )).scalars().all()
    command = None
    for row in commands:
        try:
            args = json.loads(row.args_json or "{}")
            if isinstance(args, dict) and args.get("run_id") == run.id:
                command = row
                break
        except (TypeError, ValueError):
            continue
    if command is None:
        raise HTTPException(status_code=409, detail="mailing start already claimed")
    claimed = db.execute(update(BotCommand).where(
        BotCommand.id == command.id, BotCommand.status == "pending"
    ).values(status="cancelled", processed_at=utcnow_naive()))
    if claimed.rowcount != 1:
        db.rollback()
        raise HTTPException(status_code=409, detail="mailing start already claimed")
    run.status = "cancelled"
    run.finished_at = utcnow_naive()
    commit_sync(db, op_name="mailing-start-cancel")
    return MailingActionResult(
        mailing_id=mailing_id, command="cancel-start",
        command_id=int(command.id), status="cancelled",
    )


@router.post("/{mailing_id}/stop", response_model=MailingActionResult)
def stop_mailing(
    mailing_id: int,
    db: Session = Depends(get_bot_db),
    user: User = Depends(require_operator_write),
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
    _user: User = Depends(require_operator_write),
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
    if db.execute(select(MailingRun.id).where(
        MailingRun.mailing_id == mailing_id, MailingRun.status == "queued"
    ).limit(1)).first():
        raise HTTPException(status_code=409, detail="cancel queued start before editing")

    if payload.name is not None:
        m.name = payload.name.strip()
    if payload.message_text is not None:
        m.message_text = payload.message_text
    if payload.message_variants is not None:
        cleaned = [str(x) for x in payload.message_variants if str(x).strip()]
        m.message_variants_json = json.dumps(cleaned, ensure_ascii=False)
    if payload.variant_mode is not None:
        m.variant_mode = payload.variant_mode
    if payload.use_typing is not None:
        m.use_typing = bool(payload.use_typing)
    if payload.smart_delay is not None:
        m.smart_delay = bool(payload.smart_delay)
    if payload.max_recipients is not None:
        m.max_recipients = (
            int(payload.max_recipients) if payload.max_recipients > 0 else None
        )
    if payload.mailing_cooldown_hours is not None:
        m.mailing_cooldown_hours = float(payload.mailing_cooldown_hours)
    if (
        payload.audience_client_status is not None
        or payload.audience_include_classes is not None
        or payload.audience_exclude_classes is not None
    ):
        aud = _parse_audience(getattr(m, "audience_filter_json", None))
        if payload.audience_client_status is not None:
            aud["client_status"] = payload.audience_client_status
        if payload.audience_include_classes is not None:
            aud["include_classes"] = [
                s.strip() for s in payload.audience_include_classes if s.strip()
            ][:50]
        if payload.audience_exclude_classes is not None:
            aud["exclude_classes"] = [
                s.strip() for s in payload.audience_exclude_classes if s.strip()
            ][:50]
        m.audience_filter_json = _dump_audience(aud)
    if payload.delay_between_messages is not None:
        m.delay_between_messages = float(payload.delay_between_messages)
    if payload.delay_between_accounts is not None:
        m.delay_between_accounts = float(payload.delay_between_accounts)
    if payload.daily_limit is not None:
        m.daily_limit = int(payload.daily_limit)
    if "neuro_daily_reply_limit" in payload.model_fields_set:
        if payload.neuro_daily_reply_limit is None:
            raise HTTPException(status_code=422, detail="neuro_daily_reply_limit must be an integer")
        m.neuro_daily_reply_limit = int(payload.neuro_daily_reply_limit)
    if payload.messages_per_batch is not None:
        m.messages_per_batch = int(payload.messages_per_batch)
    if payload.batch_delay is not None:
        m.batch_delay = float(payload.batch_delay)
    if payload.auto_stop_hours is not None:
        m.auto_stop_hours = (
            float(payload.auto_stop_hours) if payload.auto_stop_hours > 0 else None
        )
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
    if "neuro_timezone" in payload.model_fields_set:
        if payload.neuro_timezone is None:
            raise HTTPException(status_code=422, detail="neuro_timezone must be an IANA timezone")
        try:
            ZoneInfo(payload.neuro_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise HTTPException(status_code=422, detail="neuro_timezone must be an IANA timezone")
        m.neuro_timezone = payload.neuro_timezone
    if "neuro_active_start_minute" in payload.model_fields_set:
        m.neuro_active_start_minute = payload.neuro_active_start_minute
    if "neuro_active_end_minute" in payload.model_fields_set:
        m.neuro_active_end_minute = payload.neuro_active_end_minute
    if (m.neuro_active_start_minute is None) != (m.neuro_active_end_minute is None):
        raise HTTPException(status_code=422, detail="both neuro active minutes must be set or cleared together")
    if m.neuro_active_start_minute is not None and m.neuro_active_start_minute == m.neuro_active_end_minute:
        raise HTTPException(status_code=422, detail="neuro active start and end must differ")
    if payload.audience_mode is not None:
        m.audience_mode = payload.audience_mode

    if hasattr(m, "updated_at"):
        try:
            m.updated_at = utcnow_naive()
        except Exception:
            pass

    commit_sync(db)
    db.refresh(m)
    return _serialize_detail(m)


# ---------------------------- System prompt -----------------------------


@router.get("/{mailing_id}/prompt", response_model=MailingPromptOut)
def get_mailing_prompt(
    mailing_id: int,
    response: Response,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    m = db.get(Mailing, mailing_id)
    if not m:
        raise HTTPException(status_code=404, detail="mailing not found")
    response.headers["Cache-Control"] = "no-store"
    return MailingPromptOut(**prompt_state(db, m))


@router.put("/{mailing_id}/prompt", response_model=MailingPromptOut)
def put_mailing_prompt(
    mailing_id: int,
    payload: MailingPromptIn,
    response: Response,
    db: Session = Depends(get_bot_db),
    user: User = Depends(require_operator_write),
):
    m = db.get(Mailing, mailing_id)
    if not m:
        raise HTTPException(status_code=404, detail="mailing not found")
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty prompt")
    response.headers["Cache-Control"] = "no-store"
    try:
        return MailingPromptOut(**save_version(
            db, m, raw_text=text, action="save", actor=str(user.username),
            expected_version_id=payload.expected_version_id,
            check_expected="expected_version_id" in payload.model_fields_set,
        ))
    except PromptConflict as exc:
        raise HTTPException(status_code=409, detail="prompt version changed") from exc


@router.delete(
    "/{mailing_id}/prompt",
    response_model=MailingPromptOut,
)
def delete_mailing_prompt(
    mailing_id: int,
    response: Response,
    payload: MailingPromptExpectedIn | None = Body(default=None),
    db: Session = Depends(get_bot_db),
    user: User = Depends(require_operator_write),
):
    m = db.get(Mailing, mailing_id)
    if not m:
        raise HTTPException(status_code=404, detail="mailing not found")
    response.headers["Cache-Control"] = "no-store"
    try:
        return MailingPromptOut(**save_version(
            db, m, raw_text=None, action="reset", actor=str(user.username),
            expected_version_id=payload.expected_version_id if payload else None,
            check_expected=bool(payload and "expected_version_id" in payload.model_fields_set),
        ))
    except PromptConflict as exc:
        raise HTTPException(status_code=409, detail="prompt version changed") from exc


@router.get("/{mailing_id}/prompt/versions")
def get_mailing_prompt_versions(
    mailing_id: int,
    response: Response,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    mailing = db.get(Mailing, mailing_id)
    if mailing is None:
        raise HTTPException(status_code=404, detail="mailing not found")
    response.headers["Cache-Control"] = "no-store"
    return list_versions(db, mailing)


@router.get("/{mailing_id}/prompt/versions/{version_id}")
def get_mailing_prompt_version(
    mailing_id: int,
    version_id: int,
    response: Response,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    mailing = db.get(Mailing, mailing_id)
    if mailing is None:
        raise HTTPException(status_code=404, detail="mailing not found")
    version = get_version(db, mailing, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="prompt version not found")
    response.headers["Cache-Control"] = "no-store"
    current_id = prompt_state(db, mailing)["version_id"]
    return {
        **version_metadata(version, current_id),
        "text": version.raw_text if version.raw_text is not None else prepared_text(None),
        "is_default": version.raw_text is None,
    }


@router.post("/{mailing_id}/prompt/versions/{version_id}/restore", response_model=MailingPromptOut)
def restore_mailing_prompt_version(
    mailing_id: int,
    version_id: int,
    response: Response,
    payload: MailingPromptExpectedIn | None = Body(default=None),
    db: Session = Depends(get_bot_db),
    user: User = Depends(require_operator_write),
):
    mailing = db.get(Mailing, mailing_id)
    if mailing is None:
        raise HTTPException(status_code=404, detail="mailing not found")
    version = get_version(db, mailing, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="prompt version not found")
    response.headers["Cache-Control"] = "no-store"
    try:
        return MailingPromptOut(**save_version(
            db, mailing, raw_text=version.raw_text, action="restore", actor=str(user.username),
            expected_version_id=payload.expected_version_id if payload else None,
            check_expected=bool(payload and "expected_version_id" in payload.model_fields_set),
            restored_from_version_id=version.id,
        ))
    except PromptConflict as exc:
        raise HTTPException(status_code=409, detail="prompt version changed") from exc


def _knowledge_mailing(db: Session, mailing_id: int) -> Mailing:
    mailing = db.get(Mailing, mailing_id)
    if mailing is None:
        raise HTTPException(status_code=404, detail="mailing not found")
    return mailing


def _knowledge_item(db: Session, mailing_id: int, entry_id: int) -> NeuroKnowledgeEntry:
    entry = db.get(NeuroKnowledgeEntry, entry_id)
    if entry is None or entry.mailing_id != mailing_id:
        raise HTTPException(status_code=404, detail="knowledge entry not found")
    return entry


def _knowledge_out(entry: NeuroKnowledgeEntry) -> dict:
    return {"id": entry.id, "title": entry.title, "content": entry.content,
            "keywords": keywords_for(entry), "enabled": bool(entry.enabled),
            "updated_at": entry.updated_at}


@router.get("/{mailing_id}/knowledge")
def list_mailing_knowledge(
    mailing_id: int, response: Response, db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    response.headers["Cache-Control"] = "no-store"
    _knowledge_mailing(db, mailing_id)
    return [_knowledge_out(entry) for entry in entries_sync(db, mailing_id)]


@router.post("/{mailing_id}/knowledge", status_code=201)
def create_mailing_knowledge(
    mailing_id: int, payload: NeuroKnowledgeCreate, response: Response,
    db: Session = Depends(get_bot_db), _user: User = Depends(require_operator_write),
):
    response.headers["Cache-Control"] = "no-store"
    _knowledge_mailing(db, mailing_id)
    count = db.scalar(select(func.count()).select_from(NeuroKnowledgeEntry).where(
        NeuroKnowledgeEntry.mailing_id == mailing_id,
    ))
    if count >= 100:
        raise HTTPException(status_code=409, detail="knowledge entry limit reached")
    entry = NeuroKnowledgeEntry(
        mailing_id=mailing_id, title=payload.title, content=payload.content,
        keywords_json=json.dumps(payload.keywords, ensure_ascii=False), enabled=payload.enabled,
    )
    db.add(entry)
    commit_sync(db, op_name="knowledge-create")
    db.refresh(entry)
    return _knowledge_out(entry)


@router.patch("/{mailing_id}/knowledge/{entry_id}")
def patch_mailing_knowledge(
    mailing_id: int, entry_id: int, payload: NeuroKnowledgePatch, response: Response,
    db: Session = Depends(get_bot_db), _user: User = Depends(require_operator_write),
):
    response.headers["Cache-Control"] = "no-store"
    _knowledge_mailing(db, mailing_id)
    entry = _knowledge_item(db, mailing_id, entry_id)
    changes = payload.model_dump(exclude_unset=True)
    for field in ("title", "content", "keywords", "enabled"):
        if field in changes and changes[field] is None:
            raise HTTPException(status_code=422, detail=f"{field} cannot be null")
    for field in ("title", "content", "enabled"):
        if field in changes:
            setattr(entry, field, changes[field])
    if "keywords" in changes:
        entry.keywords_json = json.dumps(changes["keywords"], ensure_ascii=False)
    if changes:
        entry.updated_at = utcnow_naive()
        commit_sync(db, op_name="knowledge-patch")
        db.refresh(entry)
    return _knowledge_out(entry)


@router.delete("/{mailing_id}/knowledge/{entry_id}", status_code=204)
def delete_mailing_knowledge(
    mailing_id: int, entry_id: int, response: Response,
    db: Session = Depends(get_bot_db), _user: User = Depends(require_operator_write),
):
    response.headers["Cache-Control"] = "no-store"
    _knowledge_mailing(db, mailing_id)
    db.delete(_knowledge_item(db, mailing_id, entry_id))
    commit_sync(db, op_name="knowledge-delete")


@router.post("/{mailing_id}/prompt/compare")
async def compare_mailing_prompt(
    mailing_id: int,
    payload: NeuroPromptCompareIn,
    response: Response,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(require_operator_write),
):
    """Compare two draft answers without saving a prompt or contacting Telegram."""
    mailing = db.get(Mailing, mailing_id)
    response.headers["Cache-Control"] = "no-store"
    if mailing is None:
        raise HTTPException(status_code=404, detail="mailing not found")
    account = db.get(Account, int(payload.account_id)) if payload.account_id else None
    if payload.account_id and account is None:
        raise HTTPException(status_code=404, detail="account not found")
    try:
        provider = resolve_provider_sync(db, mailing)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="AI provider is not configured") from exc
    if not provider.api_key:
        raise HTTPException(status_code=409, detail="AI provider key is not configured")
    if not payload.sample_message.strip() or not payload.candidate_text.strip():
        raise HTTPException(status_code=400, detail="sample message and candidate prompt are required")

    link = normalize_public_link((mailing.community_link or "").strip())
    if not re.match(r"^https?://\S+$", link, re.I):
        link = ""
    prompt_args = {"link": link, "account": account, "mailing": mailing}
    saved_system = apply_neuro_prompt_placeholders(
        prompt_state(db, mailing)["text"], **prompt_args,
    )
    candidate_system = apply_neuro_prompt_placeholders(
        prepare_system_prompt_text(payload.candidate_text.strip()), **prompt_args,
    )
    saved_base_system = saved_system
    candidate_base_system = candidate_system
    knowledge_entries = entries_sync(db, mailing_id)
    reference = reference_text(knowledge_entries, payload.sample_message)
    saved_system += reference
    candidate_system += reference
    saved_hash = hashlib.sha256(saved_system.encode("utf-8")).hexdigest()
    candidate_hash = hashlib.sha256(candidate_system.encode("utf-8")).hexdigest()
    model = provider.model
    generation = generation_for_provider(
        provider, parse_sampling_mailing_column(mailing.neuro_sampling_json),
    )
    generation["max_tokens"] = min(max(int(generation.get("max_tokens") or 300), 1), 300)
    samples = payload.sample_messages if payload.sample_messages is not None else [payload.sample_message]
    same_prompt = saved_hash == candidate_hash
    db.rollback()  # avoid an open SQLite read transaction during network requests

    async def generate(messages: list[dict[str, str]]) -> str:
        try:
            reply, _error = await generate_reply_with_retries_and_fallback(
                messages, model, api_key=provider.api_key,
                generation=dict(generation), provider=provider,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail="prompt test failed") from exc
        if not reply:
            raise HTTPException(status_code=502, detail="prompt test failed")
        return reply.strip()[:4000]

    saved_messages = [{"role": "system", "content": saved_system}]
    candidate_messages = [{"role": "system", "content": candidate_system}]
    turns = []
    try:
        async with asyncio.timeout(120):
            for sample in samples:
                sample_reference = reference_text(knowledge_entries, sample)
                saved_messages[0] = {"role": "system", "content": saved_base_system + sample_reference}
                candidate_messages[0] = {"role": "system", "content": candidate_base_system + sample_reference}
                saved_messages.append({"role": "user", "content": sample})
                saved_reply = await generate(saved_messages)
                if same_prompt:
                    candidate_reply = saved_reply
                else:
                    candidate_messages.append({"role": "user", "content": sample})
                    candidate_reply = await generate(candidate_messages)
                turns.append({"user_message": sample, "saved_reply": saved_reply,
                              "candidate_reply": candidate_reply})
                saved_messages.append({"role": "assistant", "content": saved_reply})
                if not same_prompt:
                    candidate_messages.append({"role": "assistant", "content": candidate_reply})
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="prompt test timed out") from exc
    return {
        "mailing_id": mailing_id, "account_id": payload.account_id,
        "model": model, "saved_prompt_sha256": saved_hash,
        "candidate_prompt_sha256": candidate_hash,
        "saved_reply": saved_reply, "candidate_reply": candidate_reply,
        "turns": turns, "same_prompt": same_prompt,
        "telegram_sent": False,
        "note": "Synthetic prompt test only; no Telegram message or campaign state changed.",
    }


# ---------------------------- Test recipients ----------------------------


@router.get("/{mailing_id}/test-recipients", response_model=MailingTestRecipientsOut)
def get_test_recipients(
    mailing_id: int,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    m = db.get(Mailing, mailing_id)
    if not m:
        raise HTTPException(status_code=404, detail="mailing not found")
    rows = (
        db.execute(
            select(MailingTestRecipient.username)
            .where(MailingTestRecipient.mailing_id == mailing_id)
            .order_by(MailingTestRecipient.id)
        )
        .scalars()
        .all()
    )
    return MailingTestRecipientsOut(
        mailing_id=int(mailing_id),
        usernames=list(rows),
        unique=len(rows),
    )


@router.put("/{mailing_id}/test-recipients", response_model=MailingTestRecipientsOut)
def put_test_recipients(
    mailing_id: int,
    payload: MailingTestRecipientsIn,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(require_operator_write),
):
    """Замена тестовой аудитории (как ввод txt в боте): нормализация,
    дедуп, автосоздание Client(NEW). Полная замена списка."""
    m = db.get(Mailing, mailing_id)
    if not m:
        raise HTTPException(status_code=404, detail="mailing not found")
    seen: set[str] = set()
    unique: list[str] = []
    dups = 0
    for raw in payload.usernames:
        norm = _normalize_username(raw)
        if norm is None:
            continue
        if norm in seen:
            dups += 1
            continue
        seen.add(norm)
        unique.append(norm)
    created_clients = 0
    for uname in unique:
        c = db.execute(select(Client).where(Client.username == uname)).scalars().first()
        if c is None:
            c = Client(username=uname)  # status по умолчанию NEW (Enum default)
            db.add(c)
            db.flush()
            created_clients += 1
    db.query(MailingTestRecipient).filter(
        MailingTestRecipient.mailing_id == mailing_id
    ).delete(synchronize_session=False)
    for uname in unique:
        c = db.execute(select(Client).where(Client.username == uname)).scalars().first()
        db.add(
            MailingTestRecipient(
                mailing_id=mailing_id, username=uname, client_id=int(c.id)
            )
        )
    commit_sync(db)
    return MailingTestRecipientsOut(
        mailing_id=int(mailing_id),
        usernames=unique,
        unique=len(unique),
        duplicates=dups,
        created_clients=created_clients,
    )
