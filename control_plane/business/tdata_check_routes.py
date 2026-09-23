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
from typing import Any, Optional

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from control_plane.business.db import get_bot_db
from control_plane.deps import require_operator_write
from control_plane.models import User
from database.models import Proxy, ProxyGroup
from services.tdata_check import limits as check_limits
from services.tdata_check.checker import (
    check_proxy_from_orm,
    run_check_archive,
)
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
                Proxy.is_active == True,  # noqa: E712 — SQLAlchemy builds IS TRUE
                Proxy.is_working == True,  # noqa: E712
            )
            .order_by(Proxy.id)
        )
        .scalars()
        .all()
    )
    return list(rows)


async def execute_check_run(
    db: Session,
    *,
    group_id: int,
    data: bytes,
    requested_by: str,
    run_id: Optional[str] = None,
) -> dict[str, Any]:
    """Валидация pool + запуск проверки + сохранение run. Без создания Account."""
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
    if not data:
        raise HTTPException(status_code=400, detail="archive is empty")

    check_proxies = [check_proxy_from_orm(p) for p in proxies]
    run = await run_check_archive(data, proxies=check_proxies)
    if run.error_code:
        raise HTTPException(status_code=400, detail=run.error_code)
    payload = run.to_dict()
    payload["check_group_id"] = int(group_id)
    payload["requested_by"] = requested_by
    store_check_run(payload)
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
    data = await file.read()
    return await execute_check_run(
        db, group_id=int(group_id), data=data, requested_by=user.username
    )


@router.get("/check/{run_id}")
async def get_check_result(
    run_id: str,
    db: Session = Depends(get_bot_db),
    user: User = Depends(require_operator_write),
) -> dict:
    _ = db
    _ = user
    run = get_check_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="check run not found")
    return run


__all__ = [
    "router",
    "execute_check_run",
    "store_check_run",
    "get_check_run",
    "clear_check_runs",
]
