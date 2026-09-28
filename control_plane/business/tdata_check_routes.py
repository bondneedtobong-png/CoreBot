"""TData-check endpoint (задача 12): проверка БЕЗ создания Account.

Граница с import: этот модуль НЕ использует ``/business/tdata/import`` и НЕ
вызывает ``_create_account_from_tdata``. Любой Telegram-connect внутри
проверки идёт только через proxy из TDATA_CHECK pool (см.
:mod:`services.tdata_check`); пустой pool → 400, а не прямой коннект.

Sync/job: SYNC bounded + pollable run store (обоснование — в docstring
:mod:`services.tdata_check`). POST выполняет проверку синхронно в пределах
лимитов, кладёт результат в in-memory store и возвращает ``run_id``;
GET ``/business/tdata/check/{run_id}`` — polling.
"""

from __future__ import annotations

from collections import OrderedDict
import asyncio
from typing import Any, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Query, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from control_plane.business.db import get_bot_db
from control_plane.deps import require_operator_write
from control_plane.models import User
from database.models import Proxy, ProxyGroup, ProxyType
from services.tdata_check import limits as check_limits
from services.tdata_check.checker import (
    check_proxy_from_orm,
    run_check_archive,
    run_check_session,
)
from services.tdata_check.history import (
    list_check_history,
    load_check_history,
    save_check_history,
)
from utils.time import utcnow_naive
from utils.logger import log

router = APIRouter(prefix="/business/tdata", tags=["business-tdata-check"])

_RUNS: OrderedDict[str, dict[str, Any]] = OrderedDict()


def clear_check_runs() -> None:
    """Очистка in-memory store (тесты)."""
    _RUNS.clear()


def store_check_run(run: dict[str, Any]) -> None:
    _RUNS[run["run_id"]] = run
    while len(_RUNS) > check_limits.RUN_STORE_CAP:
        _RUNS.popitem(last=False)


def get_check_run(run_id: str) -> Optional[dict[str, Any]]:
    return _RUNS.get(run_id)


def _active_check_proxies(db: Session, group_id: int) -> list[Proxy]:
    rows = (
        db.execute(
            select(Proxy)
            .where(
                Proxy.group_id == group_id,
                Proxy.proxy_type == ProxyType.SOCKS5,
                Proxy.is_active == True,  # noqa: E712 — SQLAlchemy builds IS TRUE
                Proxy.is_working == True,  # noqa: E712
            )
            .order_by(Proxy.id)
        )
        .scalars()
        .all()
    )
    return list(rows)


def _check_pool(db: Session, group_id: int) -> list[Proxy]:
    group = db.get(ProxyGroup, int(group_id))
    if group is None:
        raise HTTPException(status_code=400, detail="check proxy group not found")
    purpose = (getattr(group, "purpose", None) or "ACCOUNT_RUNTIME").upper()
    if purpose != "TDATA_CHECK":
        raise HTTPException(
            status_code=400,
            detail="group is not a TDATA_CHECK pool (runtime pool is forbidden for check)",
        )
    proxies = _active_check_proxies(db, int(group_id))
    if not proxies:
        raise HTTPException(
            status_code=400,
            detail="check proxy pool is empty — direct connect is forbidden",
        )
    return proxies


async def execute_check_run(
    db: Session,
    *,
    group_id: int,
    data: bytes,
    requested_by: str,
    run_id: Optional[str] = None,
) -> dict[str, Any]:
    """Валидация pool + запуск проверки + сохранение run. Без создания Account."""
    proxies = _check_pool(db, group_id)
    if not data:
        raise HTTPException(status_code=400, detail="archive is empty")

    check_proxies = [check_proxy_from_orm(p) for p in proxies]
    try:
        run = await run_check_archive(data, proxies=check_proxies)
    except asyncio.CancelledError:
        save_check_history(
            db,
            {
                "run_id": run_id or uuid4().hex[:12],
                "requested_by": requested_by,
                "check_group_id": int(group_id),
                "created_at": utcnow_naive().isoformat(),
                "error_code": "check_interrupted",
            },
            status="interrupted",
        )
        raise
    except Exception:
        save_check_history(
            db,
            {
                "run_id": run_id or uuid4().hex[:12],
                "requested_by": requested_by,
                "check_group_id": int(group_id),
                "created_at": utcnow_naive().isoformat(),
                "error_code": "check_failed",
            },
            status="failed",
        )
        raise HTTPException(status_code=500, detail="check_failed") from None
    payload = run.to_dict()
    payload["check_group_id"] = int(group_id)
    payload["requested_by"] = requested_by
    save_check_history(db, payload, status="failed" if run.error_code else "completed")
    store_check_run(payload)
    if run.error_code:
        raise HTTPException(status_code=400, detail=run.error_code)
    log.info(
        f"TData check by {requested_by}: group={group_id} "
        f"total={run.total} ok={run.ok_count} failed={run.failed_count}"
    )
    return payload


@router.post("/check")
async def check_tdata(
    file: UploadFile,
    group_id: int = Form(...),
    db: Session = Depends(get_bot_db),
    user: User = Depends(require_operator_write),
) -> dict:
    data = await file.read(check_limits.MAX_ARCHIVE_BYTES + 1)
    if len(data) > check_limits.MAX_ARCHIVE_BYTES:
        raise HTTPException(status_code=413, detail="archive exceeds size limit")
    return await execute_check_run(
        db, group_id=int(group_id), data=data, requested_by=user.username
    )


async def execute_session_check_run(
    db: Session,
    *,
    group_id: int,
    data: bytes,
    requested_by: str,
    run_id: Optional[str] = None,
) -> dict[str, Any]:
    """Check uploaded session and record an operator-scoped durable run."""
    if len(data) > check_limits.MAX_SESSION_BYTES:
        raise HTTPException(status_code=413, detail="session exceeds size limit")
    if not data:
        raise HTTPException(status_code=400, detail="session is empty")
    proxies = _check_pool(db, group_id)
    check_proxies = [check_proxy_from_orm(proxy) for proxy in proxies]
    rid = run_id or uuid4().hex[:12]
    try:
        run = await run_check_session(data, proxies=check_proxies, run_id=rid)
    except asyncio.CancelledError:
        save_check_history(
            db,
            {
                "run_id": rid,
                "requested_by": requested_by,
                "check_group_id": int(group_id),
                "created_at": utcnow_naive().isoformat(),
                "error_code": "check_interrupted",
            },
            status="interrupted",
        )
        raise
    except Exception:
        save_check_history(
            db,
            {
                "run_id": rid,
                "requested_by": requested_by,
                "check_group_id": int(group_id),
                "created_at": utcnow_naive().isoformat(),
                "error_code": "check_failed",
            },
            status="failed",
        )
        raise HTTPException(status_code=500, detail="check_failed") from None
    payload = run.to_dict()
    payload["check_group_id"] = int(group_id)
    payload["requested_by"] = requested_by
    save_check_history(db, payload, status="completed")
    store_check_run(payload)
    return payload


@router.post("/check-session")
async def check_session(
    file: UploadFile,
    group_id: int = Form(...),
    db: Session = Depends(get_bot_db),
    user: User = Depends(require_operator_write),
) -> dict:
    data = await file.read(check_limits.MAX_SESSION_BYTES + 1)
    if len(data) > check_limits.MAX_SESSION_BYTES:
        raise HTTPException(status_code=413, detail="session exceeds size limit")
    return await execute_session_check_run(
        db, group_id=int(group_id), data=data, requested_by=user.username
    )


@router.get("/check/history")
async def get_check_history(
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_bot_db),
    user: User = Depends(require_operator_write),
) -> dict:
    return {"runs": list_check_history(db, user.username, limit)}


@router.get("/check/{run_id}")
async def get_check_result(
    run_id: str,
    db: Session = Depends(get_bot_db),
    user: User = Depends(require_operator_write),
) -> dict:
    run = load_check_history(db, run_id, user.username)
    if run is None:
        raise HTTPException(status_code=404, detail="check run not found")
    return run


__all__ = [
    "router",
    "execute_check_run",
    "execute_session_check_run",
    "store_check_run",
    "get_check_run",
    "clear_check_runs",
    "get_check_history",
]
