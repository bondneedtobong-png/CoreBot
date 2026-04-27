from datetime import datetime, timedelta
from collections import deque
from pathlib import Path
import re

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from control_plane.database import get_db
from control_plane.deps import get_current_user
from control_plane.models import Agent, IngestEvent, Alert
from control_plane.schemas import (
    DashboardSummaryOut,
    AgentStatusOut,
    LogItemOut,
    OpenRouterLogItemOut,
)


router = APIRouter(prefix="/dashboard", tags=["dashboard"])

_FILE_LOG_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| (?P<level>[A-Z]+)\s+\| (?P<src>[^|]+)\| (?P<msg>.*)$"
)
_MODEL_RE = re.compile(r"\bmodel=(?P<model>[A-Za-z0-9._:/-]+)")
_PROMPT_RE = re.compile(r"\bprompt_id=(?P<prompt>[A-Za-z0-9._:-]+)")


def _read_tail_lines(path: Path, max_lines: int) -> list[str]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        return list(deque(fh, maxlen=max_lines))


def _tenant_scope_id(user):
    return None if user.role == "super_admin" else user.tenant_id


@router.get("/summary", response_model=DashboardSummaryOut)
def summary(user=Depends(get_current_user), db: Session = Depends(get_db)):
    tenant_id = _tenant_scope_id(user)
    since = datetime.utcnow() - timedelta(hours=24)

    q_agents = db.query(func.count(Agent.id)).filter(Agent.is_online == True)
    q_events = db.query(func.count(IngestEvent.id)).filter(IngestEvent.created_at >= since)
    q_errors = db.query(func.count(IngestEvent.id)).filter(
        IngestEvent.created_at >= since,
        IngestEvent.level.in_(["error", "critical"]),
    )
    q_alerts = db.query(func.count(Alert.id)).filter(Alert.status == "open")
    if tenant_id is not None:
        q_agents = q_agents.filter(Agent.tenant_id == tenant_id)
        q_events = q_events.filter(IngestEvent.tenant_id == tenant_id)
        q_errors = q_errors.filter(IngestEvent.tenant_id == tenant_id)
        q_alerts = q_alerts.filter(Alert.tenant_id == tenant_id)

    return DashboardSummaryOut(
        tenant_id=user.tenant_id,
        active_agents=int(q_agents.scalar() or 0),
        events_24h=int(q_events.scalar() or 0),
        errors_24h=int(q_errors.scalar() or 0),
        alerts_open=int(q_alerts.scalar() or 0),
    )


@router.get("/agents", response_model=list[AgentStatusOut])
def agents(user=Depends(get_current_user), db: Session = Depends(get_db)):
    q = db.query(Agent).order_by(Agent.last_seen_at.desc().nullslast(), Agent.id.desc())
    tenant_id = _tenant_scope_id(user)
    if tenant_id is not None:
        q = q.filter(Agent.tenant_id == tenant_id)
    rows = q.limit(200).all()
    return [
        AgentStatusOut(
            id=r.id,
            name=r.name,
            version=r.version,
            is_online=bool(r.is_online),
            last_seen_at=r.last_seen_at,
        )
        for r in rows
    ]


@router.get("/logs", response_model=list[LogItemOut])
def logs(
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
    level: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
):
    q = db.query(IngestEvent)
    tenant_id = _tenant_scope_id(user)
    if tenant_id is not None:
        q = q.filter(IngestEvent.tenant_id == tenant_id)
    if level:
        q = q.filter(IngestEvent.level == level)
    rows = q.order_by(IngestEvent.created_at.desc()).limit(limit).all()
    return [
        LogItemOut(
            id=int(r.id),
            created_at=r.created_at,
            level=r.level,
            category=r.category,
            code=r.code,
            message=r.message,
        )
        for r in rows
    ]


@router.get("/logs/openrouter", response_model=list[OpenRouterLogItemOut])
def openrouter_logs(
    user=Depends(get_current_user),
    level: str | None = Query(default=None),
    provider: str | None = Query(default=None),
    model: str | None = Query(default=None),
    prompt_id: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
):
    # Только авторизованный пользователь; tenant-scope тут не нужен,
    # так как это локальные файловые логи инстанса.
    _ = user
    logs_path = Path(__file__).resolve().parents[2] / "logs" / "corebot.log"
    # Читаем с запасом для фильтрации, но не бесконечно.
    raw_lines = _read_tail_lines(logs_path, max(2000, limit * 12))

    want_level = (level or "").strip().upper()
    want_provider = (provider or "").strip().lower()
    want_model = (model or "").strip().lower()
    want_prompt = (prompt_id or "").strip().lower()
    want_q = (q or "").strip().lower()

    items: list[OpenRouterLogItemOut] = []
    seq = 1
    for line in reversed(raw_lines):
        m = _FILE_LOG_RE.match(line.strip())
        if not m:
            continue
        msg = m.group("msg")
        src = m.group("src")
        lvl = (m.group("level") or "").upper()
        lower_msg = msg.lower()

        # В OpenRouter-канал берём только релевантные записи.
        if (
            "openrouter" not in lower_msg
            and "neuro llm" not in lower_msg
            and "openrouter" not in src.lower()
        ):
            continue
        if want_level and lvl != want_level:
            continue

        model_match = _MODEL_RE.search(msg)
        model_value = model_match.group("model") if model_match else None
        provider_value = None
        if model_value and "/" in model_value:
            provider_value = model_value.split("/", 1)[0].lower()
        elif "openrouter" in lower_msg:
            provider_value = "openrouter"

        prompt_match = _PROMPT_RE.search(msg)
        prompt_value = prompt_match.group("prompt") if prompt_match else None

        if want_provider and (provider_value or "").lower() != want_provider:
            continue
        if want_model and want_model not in (model_value or "").lower():
            continue
        if want_prompt and want_prompt != (prompt_value or "").lower():
            continue
        if want_q and want_q not in lower_msg:
            continue

        try:
            created_at = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S")
        except Exception:
            created_at = datetime.utcnow()
        items.append(
            OpenRouterLogItemOut(
                id=seq,
                created_at=created_at,
                level=lvl.lower(),
                provider=provider_value,
                model=model_value,
                prompt_id=prompt_value,
                message=msg,
            )
        )
        seq += 1
        if len(items) >= limit:
            break
    return items


@router.get("/alerts")
def alerts(user=Depends(get_current_user), db: Session = Depends(get_db), limit: int = Query(default=100, ge=1, le=500)):
    q = db.query(Alert).order_by(Alert.last_triggered_at.desc(), Alert.id.desc())
    tenant_id = _tenant_scope_id(user)
    if tenant_id is not None:
        q = q.filter(Alert.tenant_id == tenant_id)
    rows = q.limit(limit).all()
    return [
        {
            "id": int(r.id),
            "kind": r.kind,
            "severity": r.severity,
            "title": r.title,
            "status": r.status,
            "count": int(r.count or 0),
            "last_triggered_at": r.last_triggered_at,
        }
        for r in rows
    ]
