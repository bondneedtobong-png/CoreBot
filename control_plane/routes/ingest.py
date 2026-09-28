from __future__ import annotations

import json
from utils.time import utcnow_naive
from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from control_plane.database import get_db
from control_plane.deps import resolve_agent_by_token
from control_plane.models import IngestEvent, MetricPoint
from control_plane.schemas import IngestBatchIn
from control_plane.services.alerts import upsert_alert, send_telegram_alert


router = APIRouter(prefix="/ingest", tags=["ingest"])

MAX_INGEST_BATCH = 500


def _truncate_payload_json(payload: dict | None, limit: int = 8000) -> str | None:
    if not payload:
        return None
    try:
        s = json.dumps(payload, ensure_ascii=False)
    except Exception:
        return None
    return s if len(s) <= limit else s[:limit]


@router.post("/batch")
async def ingest_batch(
    payload: IngestBatchIn,
    x_agent_token: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    if not x_agent_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing agent token")
    if len(payload.events) > MAX_INGEST_BATCH or len(payload.metrics) > MAX_INGEST_BATCH:
        raise HTTPException(status_code=413, detail="Batch too large (max 500)")
    agent = resolve_agent_by_token(x_agent_token, db)
    agent.last_seen_at = utcnow_naive()
    agent.is_online = True
    if payload.version:
        agent.version = str(payload.version)[:64]
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise

    inserted_events = 0
    inserted_metrics = 0
    try:
        for e in payload.events:
            # id не задаём вручную: autoincrement БД исключает коллизии
            # int(time*1e6) при параллельных батчах.
            row = IngestEvent(
                tenant_id=agent.tenant_id,
                agent_id=agent.id,
                level=str(e.level)[:16],
                category=str(e.category)[:128],
                code=str(e.code)[:128] if e.code else None,
                message=str(e.message)[:8000],
                payload_json=_truncate_payload_json(e.payload),
                schema_version=str(e.schema_version)[:16],
            )
            db.add(row)
            inserted_events += 1
            if str(e.level).lower() in ("error", "critical"):
                alert = upsert_alert(
                    db,
                    tenant_id=agent.tenant_id,
                    agent_id=agent.id,
                    kind="error_event",
                    severity="critical" if str(e.level).lower() == "critical" else "warning",
                    title=f"Agent error: {str(e.category)[:120]}",
                    details=str(e.message)[:2000],
                    fingerprint=f"{agent.id}:{e.category}:{e.code or '-'}",
                )
                await send_telegram_alert(alert.title, alert.details or "")
        for m in payload.metrics:
            db.add(
                MetricPoint(
                    tenant_id=agent.tenant_id,
                    agent_id=agent.id,
                    name=str(m.name)[:128],
                    value=float(m.value),
                    tags_json=_truncate_payload_json(m.tags),
                )
            )
            inserted_metrics += 1

        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"ok": True, "events": inserted_events, "metrics": inserted_metrics}
