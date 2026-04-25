"""
Воркер ручных исходящих сообщений из веб-панели.

Поллит таблицу `outbound_queue` (status='pending'), для каждой строки находит
соответствующий Worker в `worker_manager.workers` и отправляет сообщение через
Telethon. После успешной отправки:
  * запись помечается status='sent'
  * сообщение попадает в neuro_chat_messages (role='assistant') —
    чтобы при возврате аккаунта в AI_ACTIVE LLM видела ручной хвост диалога
  * пишется client_interactions(direction='out', kind='manual_send')

При ошибке — status='failed' + причина.
"""
from __future__ import annotations

import asyncio
import random
from datetime import datetime
from typing import Optional

from database.crm_repositories import ClientInteractionRepository
from database.repositories import (
    AccountRepository,
    ClientRepository,
    NeuroChatRepository,
    OutboundQueueRepository,
)
from database.session import session_scope
from utils.logger import log


class OutboundConsumer:
    """Бэкграунд-задача: разбирает очередь ручных отправок из веб-панели."""

    def __init__(self, *, batch_size: int = 10, idle_sleep_sec: float = 1.5):
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._batch_size = int(batch_size)
        self._idle_sleep_sec = float(idle_sleep_sec)

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="outbound-consumer")
        log.info("OutboundConsumer started")

    async def stop(self) -> None:
        self._stop.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("OutboundConsumer stopped")

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                processed = await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # защищаемся от падения цикла
                log.warning(f"OutboundConsumer tick error: {e}")
                processed = 0
            if processed == 0:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self._idle_sleep_sec
                    )
                except asyncio.TimeoutError:
                    pass

    async def _tick(self) -> int:
        from workers.manager import worker_manager  # локальный импорт против циклов

        async with session_scope() as session:
            batch = await OutboundQueueRepository.fetch_pending_batch(
                session, limit=self._batch_size
            )
        if not batch:
            return 0

        for row in batch:
            await self._process_one(row, worker_manager)
            # Лёгкий джиттер между отправками от разных аккаунтов
            await asyncio.sleep(random.uniform(0.2, 0.6))
        return len(batch)

    async def _process_one(self, row, worker_manager) -> None:
        worker = worker_manager.workers.get(int(row.account_id))
        if worker is None or not getattr(worker, "is_connected", False):
            async with session_scope() as session:
                await OutboundQueueRepository.mark_failed(
                    session, row.id, "worker not connected"
                )
            log.warning(
                f"OutboundConsumer: worker for account_id={row.account_id} "
                f"not connected, queue_id={row.id} failed"
            )
            return

        text = (row.text or "").strip()
        if not text:
            async with session_scope() as session:
                await OutboundQueueRepository.mark_failed(session, row.id, "empty text")
            return

        ok, msg_id, err, _peer_uid = await worker.send_message_with_typing(
            int(row.peer_user_id),
            text,
            typing_delay=0.0,
            use_typing=False,
            parse_mode=None,
        )

        if not ok:
            async with session_scope() as session:
                await OutboundQueueRepository.mark_failed(
                    session, row.id, err or "unknown send error"
                )
            log.warning(
                f"OutboundConsumer: send failed (queue_id={row.id}, "
                f"account={row.account_id}, peer={row.peer_user_id}): {err}"
            )
            return

        async with session_scope() as session:
            try:
                await OutboundQueueRepository.mark_sent(session, row.id, msg_id)
            except Exception as e:
                log.warning(f"OutboundConsumer: mark_sent failed: {e}")

            try:
                await NeuroChatRepository.append(
                    session,
                    int(row.account_id),
                    int(row.peer_user_id),
                    "assistant",
                    text,
                )
            except Exception as e:
                log.warning(f"OutboundConsumer: history append failed: {e}")

            client_id = row.client_id
            if client_id is None:
                client = await ClientRepository.get_by_telegram_user_id(
                    session, int(row.peer_user_id)
                )
                if client:
                    client_id = client.id
            if client_id is not None:
                try:
                    await ClientInteractionRepository.add(
                        session,
                        client_id=int(client_id),
                        account_id=int(row.account_id),
                        mailing_id=None,
                        direction="out",
                        kind="manual_send",
                        body=(text[:4000] if text else None),
                        telegram_message_id=int(msg_id) if msg_id else None,
                    )
                except Exception as e:
                    log.warning(
                        f"OutboundConsumer: client_interactions add failed: {e}"
                    )

            try:
                await AccountRepository.increment_messages_sent(
                    session, int(row.account_id)
                )
            except AttributeError:
                pass
            except Exception as e:
                log.warning(f"OutboundConsumer: counter inc failed: {e}")

        log.info(
            f"OutboundConsumer: sent queue_id={row.id} "
            f"account={row.account_id} peer={row.peer_user_id} msg_id={msg_id}"
        )


outbound_consumer = OutboundConsumer()
