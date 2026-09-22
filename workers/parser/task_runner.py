"""Поллинг parsing_tasks и выполнение сценариев (одна задача за раз по умолчанию)."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select, update

from database.models import ParsingTask, ParsingTaskLog
from database.repositories import AccountRepository
from database.session import session_scope
from utils.logger import log

from workers.parser.account_pool import AccountSnap, RotatingClients
from workers.parser import collect_channels, collect_groups, collect_users

POLL_SEC = float(os.getenv("PARSER_POLL_SEC", "2.0"))


async def claim_pending_task(session, task_id: int) -> bool:
    """Atomically claim one pending task; False means another worker won."""
    result = await session.execute(
        update(ParsingTask)
        .where(ParsingTask.id == task_id, ParsingTask.status == "pending")
        .values(
            status="running",
            started_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0) == 1


async def _append_log(
    session,
    task_id: int,
    *,
    account_id: Optional[int],
    level: str,
    event: str,
    message: str,
    payload: Optional[dict[str, Any]] = None,
) -> None:
    session.add(
        ParsingTaskLog(
            task_id=task_id,
            level=level or "info",
            account_id=account_id,
            event=event,
            message=message,
            payload_json=payload,
        )
    )
    await session.commit()


async def process_task(task_id: int) -> None:
    pool: Optional[RotatingClients] = None
    try:
        async with session_scope() as session:
            if not await claim_pending_task(session, task_id):
                await session.rollback()
                return
            await session.commit()

        async with session_scope() as session:
            task = await session.get(ParsingTask, task_id)
            if not task:
                return
            if task.status == "cancelled":
                return
            ids = [int(x) for x in (task.accounts_json or [])]
            snaps: list[AccountSnap] = []
            for aid in ids:
                a = await AccountRepository.get_by_id(session, aid)
                if a:
                    snaps.append(AccountSnap(id=a.id, session_name=a.session_name, proxy=a.proxy))
            if not snaps:
                task.status = "failed"
                task.finished_at = datetime.utcnow()
                task.last_error = "no valid accounts / sessions"
                await session.commit()
                await _append_log(
                    session,
                    task_id,
                    account_id=None,
                    level="error",
                    event="no_accounts",
                    message="Нет валидных аккаунтов в accounts_json",
                )
                return

            pool = RotatingClients(snaps)

            async def logfn(
                account_id: Optional[int],
                level: str,
                event: str,
                message: str,
                payload: Optional[dict[str, Any]] = None,
            ) -> None:
                await _append_log(
                    session,
                    task_id,
                    account_id=account_id,
                    level=level,
                    event=event,
                    message=message,
                    payload=payload,
                )

            await logfn(None, "info", "start", f"kind={task.kind} depth={task.depth}")

            if task.kind == "channels":
                await collect_channels.run_channel_task(session, task, pool, logfn)
            elif task.kind == "groups":
                await collect_groups.run_group_task(session, task, pool, logfn)
            elif task.kind == "users":
                await collect_users.run_users_task(session, task, pool, logfn)
            else:
                await logfn(None, "error", "bad_kind", f"Unknown kind {task.kind}")
                task.status = "failed"
                task.last_error = "unknown kind"
                task.finished_at = datetime.utcnow()
                await session.commit()
                return

            await session.refresh(task)
            if task.status == "cancelled":
                await logfn(None, "info", "done", "cancelled during run")
            else:
                task.status = "completed"
                task.progress_percent = 100
                task.finished_at = datetime.utcnow()
                task.current_stage = "done"
                await logfn(None, "info", "completed", "OK")
            await session.commit()

    except Exception as e:
        log.exception(f"Parsing task {task_id} failed: {e}")
        async with session_scope() as session:
            task = await session.get(ParsingTask, task_id)
            if task and task.status not in ("completed", "cancelled"):
                task.status = "failed"
                task.last_error = str(e)[:2000]
                task.finished_at = datetime.utcnow()
                await session.commit()
            await _append_log(
                session,
                task_id,
                account_id=None,
                level="error",
                event="fatal",
                message=str(e)[:1500],
            )
    finally:
        if pool:
            await pool.disconnect_all()


async def run_forever() -> None:
    log.info(
        f"parser-worker poll={POLL_SEC}s (set PARSER_POLL_SEC). "
        "Ожидание pending задач в parsing_tasks…"
    )
    while True:
        tid: Optional[int] = None
        try:
            async with session_scope() as session:
                tid = await session.scalar(
                    select(ParsingTask.id)
                    .where(ParsingTask.status == "pending")
                    .order_by(ParsingTask.id.asc())
                    .limit(1)
                )
        except Exception as e:
            log.warning(f"parser poll error: {e}")
            await asyncio.sleep(POLL_SEC)
            continue

        if tid is None:
            await asyncio.sleep(POLL_SEC)
            continue

        await process_task(int(tid))
