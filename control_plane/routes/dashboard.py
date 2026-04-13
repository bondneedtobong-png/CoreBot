from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from control_plane.database import get_db
from control_plane.deps import get_current_user
from control_plane.models import Agent, IngestEvent, Alert
from control_plane.schemas import DashboardSummaryOut, AgentStatusOut, LogItemOut


router = APIRouter(prefix="/dashboard", tags=["dashboard"])


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
