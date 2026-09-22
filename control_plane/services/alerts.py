from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from utils.time import utcnow_naive

import aiohttp
from sqlalchemy.orm import Session

from control_plane.config import CP_INGEST_SUPPRESSION_SEC, CP_TELEGRAM_BOT_TOKEN, CP_TELEGRAM_ALERT_CHAT_ID
from control_plane.models import Alert


def upsert_alert(
    db: Session,
    *,
    tenant_id: int,
    agent_id: int | None,
    kind: str,
    severity: str,
    title: str,
    details: str,
    fingerprint: str,
) -> Alert:
    now = utcnow_naive()
    existing = (
        db.query(Alert)
        .filter(Alert.tenant_id == tenant_id, Alert.fingerprint == fingerprint)
        .first()
    )
    if existing:
        if existing.last_triggered_at and now - existing.last_triggered_at < timedelta(seconds=CP_INGEST_SUPPRESSION_SEC):
            return existing
        existing.count = int(existing.count or 0) + 1
        existing.last_triggered_at = now
        existing.title = title
        existing.details = details
        existing.status = "open"
        db.commit()
        db.refresh(existing)
        return existing

    row = Alert(
        id=int(time.time() * 1000000),
        tenant_id=tenant_id,
        agent_id=agent_id,
        kind=kind,
        severity=severity,
        title=title,
        details=details,
        fingerprint=fingerprint,
        status="open",
        count=1,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


async def send_telegram_alert(title: str, details: str) -> None:
    if not CP_TELEGRAM_BOT_TOKEN or not CP_TELEGRAM_ALERT_CHAT_ID:
        return
    text = f"<b>{title}</b>\n<pre>{details[:3000]}</pre>"
    url = f"https://api.telegram.org/bot{CP_TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CP_TELEGRAM_ALERT_CHAT_ID, "text": text, "parse_mode": "HTML"}
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=payload) as resp:
            _ = await resp.text()
