from __future__ import annotations

import json
import time
from datetime import datetime
from utils.time import utcnow_naive
from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from control_plane.database import get_db
from control_plane.deps import resolve_agent_by_token
from control_plane.models import Agent, IngestEvent, MetricPoint
from control_plane.schemas import IngestBatchIn
from control_plane.services.alerts import upsert_alert, send_telegram_alert


router = APIRouter(prefix="/ingest", tags=["ingest"])


@router.post("/batch")
async def ingest_batch(
    payload: IngestBatchIn,
    x_agent_token: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    if not x_agent_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing agent token")
    agent = resolve_agent_by_token(x_agent_token, db)
    agent.last_seen_at = utcnow_naive()
    agent.is_online = True
    if payload.version:
        agent.version = payload.version
    db.commit()

    inserted_events = 0
    inserted_metrics = 0
    for e in payload.events:
        row = IngestEvent(
            id=int(time.time() * 1000000),
            tenant_id=agent.tenant_id,
            agent_id=agent.id,
            level=e.level,
            category=e.category,
            code=e.code,
            message=e.message,
            payload_json=json.dumps(e.payload, ensure_ascii=False) if e.payload else None,
            schema_version=e.schema_version,
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
                title=f"Agent error: {e.category}",
                details=e.message,
                fingerprint=f"{agent.id}:{e.category}:{e.code or '-'}",
            )
            await send_telegram_alert(alert.title, alert.details or "")
    for m in payload.metrics:
        db.add(
            MetricPoint(
                id=int(time.time() * 1000000) + inserted_metrics + 1,
                tenant_id=agent.tenant_id,
                agent_id=agent.id,
                name=m.name,
                value=m.value,
                tags_json=json.dumps(m.tags, ensure_ascii=False) if m.tags else None,
            )
        )
        inserted_metrics += 1

    db.commit()
    return {"ok": True, "events": inserted_events, "metrics": inserted_metrics}
