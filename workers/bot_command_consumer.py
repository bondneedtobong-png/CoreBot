"""
Воркер команд от веб-панели → к боту.

Поллит таблицу `bot_commands` (status='pending') и исполняет команды через
`worker_manager`. Сейчас поддерживает:

  mailing.start  args={"mailing_id": int}
  mailing.pause  args={"mailing_id": int}   (мягкий стоп + status=PAUSED)
  mailing.stop   args={"mailing_id": int}   (мягкий стоп + status=COMPLETED|CANCELLED по факту)

Любые ошибки записываются в bot_commands.error и status=failed.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Optional

from sqlalchemy import select, update

from database.models import BotCommand, Mailing, MailingStatus
from database.session import session_scope
from utils.logger import log


class BotCommandConsumer:
    """Бэкграунд-задача внутри процесса бота."""

    def __init__(self, *, idle_sleep_sec: float = 2.0):
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._idle_sleep_sec = float(idle_sleep_sec)

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="bot-command-consumer")
        log.info("BotCommandConsumer started")

    async def stop(self) -> None:
        self._stop.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("BotCommandConsumer stopped")

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                processed = await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning(f"BotCommandConsumer tick error: {e}")
                processed = 0
            if processed == 0:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self._idle_sleep_sec
                    )
                except asyncio.TimeoutError:
                    pass

    async def _tick(self) -> int:
        async with session_scope() as session:
            res = await session.execute(
                select(BotCommand)
                .where(BotCommand.status == "pending")
                .order_by(BotCommand.id.asc())
                .limit(20)
            )
            rows = list(res.scalars().all())
            if not rows:
                return 0
            ids = [int(r.id) for r in rows]
            await session.execute(
                update(BotCommand)
                .where(BotCommand.id.in_(ids))
                .values(status="processing")
            )
            await session.commit()

        for row in rows:
            await self._process_one(row)
        return len(rows)

    async def _process_one(self, row: BotCommand) -> None:
        from workers.manager import worker_manager  # локальный импорт

        cmd = (row.command or "").strip().lower()
        try:
            args = json.loads(row.args_json or "{}")
        except Exception:
            args = {}

        try:
            if cmd == "mailing.start":
                mailing_id = int(args.get("mailing_id") or 0)
                if mailing_id <= 0:
                    raise ValueError("mailing_id required")
                # запускаем рассылку как отдельную фоновую таску, чтобы консьюмер
                # не блокировался на длинной кампании
                asyncio.create_task(
                    worker_manager.start_mailing(mailing_id),
                    name=f"mailing-{mailing_id}",
                )
                await self._mark_done(row, "ok: scheduled")
                return

            if cmd in ("mailing.pause", "mailing.stop"):
                mailing_id = int(args.get("mailing_id") or 0)
                if mailing_id <= 0:
                    raise ValueError("mailing_id required")
                # текущий worker_manager не знает, какой именно mailing идёт —
                # stop_mailing() глобально шлёт _stop_event активной кампании
                worker_manager.stop_mailing()
                # после остановки выставляем нужный статус, если этот mailing
                # реально был активным
                async with session_scope() as session:
                    m = await session.get(Mailing, mailing_id)
                    if m is not None:
                        cur = getattr(m.status, "value", str(m.status)).upper()
                        if cur == "RUNNING":
                            target = (
                                MailingStatus.PAUSED
                                if cmd == "mailing.pause"
                                else MailingStatus.CANCELLED
                            )
                            m.status = target
                            await session.commit()
                await self._mark_done(row, "ok: stop signal sent")
                return

            raise ValueError(f"unknown command: {cmd}")
        except Exception as e:
            await self._mark_failed(row, str(e))

    async def _mark_done(self, row: BotCommand, detail: str = "") -> None:
        async with session_scope() as session:
            await session.execute(
                update(BotCommand)
                .where(BotCommand.id == int(row.id))
                .values(
                    status="done",
                    error=detail or None,
                    processed_at=datetime.utcnow(),
                )
            )
            await session.commit()

    async def _mark_failed(self, row: BotCommand, err: str) -> None:
        log.warning(f"BotCommand {row.id} failed: {err}")
        async with session_scope() as session:
            await session.execute(
                update(BotCommand)
                .where(BotCommand.id == int(row.id))
                .values(
                    status="failed",
                    error=err,
                    processed_at=datetime.utcnow(),
                )
            )
            await session.commit()


bot_command_consumer = BotCommandConsumer()
