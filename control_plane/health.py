"""Liveness and dependency readiness endpoints for the Control Plane."""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from control_plane.business.db import BotSession
from control_plane.database import SessionLocal
from utils.background_tasks import background_tasks

router = APIRouter()


def probe_control_plane_db() -> bool:
    try:
        with SessionLocal() as session:
            session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def _probe_bot_db_sync() -> bool:
    session = BotSession()
    try:
        session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
    finally:
        session.close()


async def probe_bot_db() -> bool:
    # The Control Plane can run with PARSER_EMBEDDED=0, in which case the
    # async bot_db engine is intentionally not connected. Probe the shared
    # corebot database through the Control Plane's sync session instead.
    return await asyncio.to_thread(_probe_bot_db_sync)


def _parser_status(task: asyncio.Task[Any] | None) -> tuple[str, bool]:
    if task is None:
        return "disabled", True
    if not task.done():
        return "running", True
    if task.cancelled():
        return "stopped", False
    try:
        if task.exception() is not None:
            return "failed", False
        return "stopped", False
    except asyncio.CancelledError:
        return "stopped", False


@router.get("/health")
def health() -> dict[str, bool]:
    """Backward-compatible lightweight liveness response."""
    return {"ok": True}


@router.get("/health/live")
def liveness() -> dict[str, bool]:
    return {"ok": True}


@router.get("/health/ready")
async def readiness(request: Request) -> JSONResponse:
    cp_ok = probe_control_plane_db()
    bot_ok = await probe_bot_db()
    parser_status, parser_ok = _parser_status(
        getattr(request.app.state, "parser_task", None)
    )
    task_snapshot = background_tasks.snapshot()
    background_ok = int(task_snapshot.get("failed", 0)) == 0

    components = {
        "control_plane_db": "ok" if cp_ok else "unavailable",
        "bot_db": "ok" if bot_ok else "unavailable",
        "parser": parser_status,
        "background_tasks": "ok" if background_ok else "failed",
    }
    ok = cp_ok and bot_ok and parser_ok and background_ok
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"ok": ok, "components": components},
    )
