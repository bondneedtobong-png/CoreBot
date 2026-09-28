"""Durable, deliberately narrow TData check reports."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from control_plane.business.db import commit_sync
from database.models import TDataCheckHistory
from database.sqlite_pragmas import run_sync_with_busy_retry
from services.tdata_check.models import CHECK_STATUSES
from utils.time import utcnow_naive

_REASONS = frozenset({
    "not_tdata_structure", "convert_error", "no_check_proxy",
    "proxy_lease_timeout", "session_unauthorized", "empty_profile",
    "rpc_error", "spambot_probe_error", "spam_restricted",
    "auth_key_unregistered", "session_revoked", "account_deactivated",
    "password_needed", "connect_timeout", "proxy_connect_error",
    "flood_wait", "not_a_zip", "archive_empty", "archive_too_large",
    "path_traversal", "no_files", "no_tdata_roots", "check_failed",
    "check_interrupted",
    "invalid_session", "session_too_large", "session_empty",
})
_RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


def _reason(value: Any) -> str | None:
    """Accept only machine codes, never raw exception text."""
    code = str(value or "")
    if code.startswith("archive_invalid:"):
        suffix = code.partition(":")[2]
        return code if suffix in _REASONS else "archive_invalid"
    return code if code in _REASONS else None


def _profile(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    return value[:limit] or None


def _safe_item(item: dict[str, Any]) -> dict[str, Any]:
    status = str(item.get("status") or "unknown")
    if status not in CHECK_STATUSES:
        status = "unknown"
    return {
        "item_id": _profile(item.get("item_id"), 40),
        "status": status,
        "phone": _profile(item.get("phone"), 40),
        "username": _profile(item.get("username"), 64),
        "first_name": _profile(item.get("first_name"), 120),
        "last_name": _profile(item.get("last_name"), 120),
        "country": _profile(item.get("country"), 8),
        "user_id": item.get("user_id") if isinstance(item.get("user_id"), int) else None,
        "error_code": _reason(item.get("error_code")),
        "retry_after": item.get("retry_after") if isinstance(item.get("retry_after"), int) else None,
        "spam_restricted": item.get("spam_restricted") if isinstance(item.get("spam_restricted"), bool) else None,
    }


def _created_at(value: Any) -> datetime:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return utcnow_naive()


def save_check_history(
    db: Session, payload: dict[str, Any], *, status: str | None = None
) -> None:
    """Persist an allowlisted snapshot after a check ends."""
    safe_status = status or str(payload.get("status") or "completed")
    if safe_status not in {"completed", "failed", "interrupted"}:
        safe_status = "failed"
    items = [_safe_item(i) for i in payload.get("items", []) if isinstance(i, dict)]
    run_id = str(payload["run_id"])
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError("invalid check run id")
    row = TDataCheckHistory(
        run_id=run_id,
        requested_by=str(payload["requested_by"])[:120],
        check_group_id=int(payload["check_group_id"]),
        status=safe_status,
        created_at=_created_at(payload.get("created_at")),
        finished_at=utcnow_naive(),
        total=int(payload.get("total") or 0),
        ok_count=int(payload.get("ok_count") or 0),
        failed_count=int(payload.get("failed_count") or 0),
        truncated=bool(payload.get("truncated")),
        reason=_reason(payload.get("error_code")),
        items_json=json.dumps(items, ensure_ascii=False),
    )
    db.merge(row)
    commit_sync(db, op_name="tdata-check-history")


def _row_dict(row: TDataCheckHistory, *, include_items: bool) -> dict[str, Any]:
    result = {
        "run_id": row.run_id,
        "requested_by": row.requested_by,
        "check_group_id": row.check_group_id,
        "status": row.status,
        "created_at": row.created_at.isoformat(),
        "finished_at": row.finished_at.isoformat(),
        "total": row.total,
        "ok_count": row.ok_count,
        "failed_count": row.failed_count,
        "truncated": row.truncated,
        "error_code": row.reason,
    }
    if include_items:
        result["items"] = json.loads(row.items_json)
    return result


def load_check_history(db: Session, run_id: str, requested_by: str) -> dict[str, Any] | None:
    if not _RUN_ID.fullmatch(run_id):
        return None
    row = run_sync_with_busy_retry(
        lambda: db.execute(select(TDataCheckHistory).where(
            TDataCheckHistory.run_id == run_id,
            TDataCheckHistory.requested_by == requested_by,
        )).scalar_one_or_none(),
        op_name="tdata-check-history-read",
    )
    return _row_dict(row, include_items=True) if row else None


def list_check_history(db: Session, requested_by: str, limit: int = 20) -> list[dict[str, Any]]:
    bounded = max(1, min(int(limit), 100))
    rows = run_sync_with_busy_retry(
        lambda: db.execute(
            select(TDataCheckHistory)
            .where(TDataCheckHistory.requested_by == requested_by)
            .order_by(TDataCheckHistory.created_at.desc(), TDataCheckHistory.run_id.desc())
            .limit(bounded)
        ).scalars().all(),
        op_name="tdata-check-history-list",
    )
    return [_row_dict(row, include_items=False) for row in rows]
