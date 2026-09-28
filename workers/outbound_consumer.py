"""
Воркер ручных исходящих сообщений из веб-панели.

Поллит таблицу `outbound_queue` (status='pending'), для каждой строки находит
соответствующий Worker в `worker_manager.workers` и отправляет сообщение через
Telethon. После успешной отправки:
  * запись помечается status='sent'
  * сообщение попадает в neuro_chat_messages (role='assistant') —
    чтобы при возврате аккаунта в AI_ACTIVE LLM видела ручной хвост диалога
  * пишется client_interactions(direction='out', kind='manual_send')

После начала отправки ошибка или остановка процесса оставляют исход на ручную
проверку: автоматически повторять такой запрос небезопасно.
"""
from __future__ import annotations

import asyncio
import random
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
        self._last_recovery_at = 0.0

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
            # Task 08: heartbeat для CP (shared corebot.db, без нового порта).
            from control_plane.services.heartbeat import (
                OUTBOUND_COMPONENT,
                record_tick_best_effort,
            )

            await record_tick_best_effort(OUTBOUND_COMPONENT)
            if processed == 0:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self._idle_sleep_sec
                    )
                except asyncio.TimeoutError:
                    pass

    async def _tick(self) -> int:
        from workers.manager import worker_manager  # локальный импорт против циклов

        now = asyncio.get_running_loop().time()
        if now - self._last_recovery_at >= 60:
            async with session_scope() as session:
                recovered = await OutboundQueueRepository.recover_stale_sending(session)
            if recovered:
                log.warning(f"OutboundConsumer: {recovered} stale sends require review")
            self._last_recovery_at = now

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

    # Число попыток подготовить соединение до первого Telegram RPC.
    MAX_ATTEMPTS = 5
    # База для экспоненциального бэк-оффа в секундах: 2, 4, 8, 16, 32.
    BACKOFF_BASE_SEC = 2.0
    BACKOFF_MAX_SEC = 60.0

    def _backoff_delay(self, attempts_done: int) -> float:
        delay = self.BACKOFF_BASE_SEC * (2 ** max(0, attempts_done - 1))
        return min(self.BACKOFF_MAX_SEC, delay) + random.uniform(0, 1.5)

    async def _ensure_worker(self, account_id: int, worker_manager):
        """
        Достать живой Worker для аккаунта. Если воркера нет — догрузить
        ТОЛЬКО его через ensure_worker (без disconnect_all+clear всего пула,
        чтобы не ронять активную рассылку).
        Если воркер есть, но disconnected — попробовать поднять.
        Возвращает Worker | None.
        """
        worker = worker_manager.workers.get(int(account_id))
        if worker is None:
            try:
                if hasattr(worker_manager, "ensure_worker"):
                    worker = await worker_manager.ensure_worker(int(account_id))
                else:
                    await worker_manager.load_accounts()
                    worker = worker_manager.workers.get(int(account_id))
            except Exception as e:
                log.warning(f"OutboundConsumer: ensure_worker failed: {e}")
            if worker is None:
                return None

        if not getattr(worker, "is_connected", False):
            try:
                ok = await worker.connect(quiet=True)
                if not ok:
                    return None
            except Exception as e:
                log.warning(
                    f"OutboundConsumer: lazy connect failed for account_id={account_id}: {e}"
                )
                return None

        return worker

    async def _process_one(self, row, worker_manager) -> None:
        attempts_done = int(getattr(row, "attempts", 0) or 0)
        worker = await self._ensure_worker(int(row.account_id), worker_manager)
        worker_ready = bool(worker and getattr(worker, "is_connected", False))

        if not worker_ready:
            # Не падаем моментально — может быть временный disconnect/restart.
            if attempts_done + 1 >= self.MAX_ATTEMPTS:
                async with session_scope() as session:
                    await OutboundQueueRepository.mark_failed(
                        session, row.id, "worker not connected (max attempts reached)"
                    )
                log.warning(
                    f"OutboundConsumer: worker for account_id={row.account_id} "
                    f"not connected after {self.MAX_ATTEMPTS} attempts, "
                    f"queue_id={row.id} -> failed"
                )
            else:
                delay = self._backoff_delay(attempts_done + 1)
                async with session_scope() as session:
                    await OutboundQueueRepository.reschedule(
                        session,
                        row.id,
                        delay_sec=delay,
                        last_error="worker not connected",
                    )
                log.info(
                    f"OutboundConsumer: worker not ready for queue_id={row.id} "
                    f"(account={row.account_id}), retry in {delay:.1f}s "
                    f"(attempt {attempts_done + 1}/{self.MAX_ATTEMPTS})"
                )
            return

        text = (row.text or "").strip()
        if not text:
            async with session_scope() as session:
                await OutboundQueueRepository.mark_failed(session, row.id, "empty text")
            return

        # Как в рассылочном пути (manager): предпочитаем @username строке,
        # иначе Telethon часто даёт «Could not find the input entity»,
        # если с этим аккаунтом ещё не было диалога.
        target_peer = int(row.peer_user_id)
        try:
            async with session_scope() as _s:
                _client = await ClientRepository.get_by_telegram_user_id(
                    _s, int(row.peer_user_id)
                )
                _uname = (getattr(_client, "username", None) or "").strip().lstrip("@") if _client else ""
                if _uname:
                    target_peer = _uname
                elif getattr(row, "client_id", None):
                    _c2 = await ClientRepository.get_by_id(_s, int(row.client_id))
                    _u2 = (getattr(_c2, "username", None) or "").strip().lstrip("@") if _c2 else ""
                    if _u2:
                        target_peer = _u2
        except Exception as e:
            log.debug(f"OutboundConsumer: username resolve failed, fallback to id: {e}")

        # Claim is committed before the external side effect. If cancellation or
        # another consumer won, this process must never call Telegram.
        async with session_scope() as session:
            claimed = await OutboundQueueRepository.claim_pending(session, row.id)
        if not claimed:
            return

        try:
            ok, msg_id, err, _peer_uid = await asyncio.wait_for(
                worker.send_message_with_typing(
                    target_peer,
                    text,
                    typing_delay=0.0,
                    use_typing=False,
                    parse_mode=None,
                    source="manual_queue",
                ),
                timeout=300,
            )
        except asyncio.CancelledError:
            # The in-flight RPC may have reached Telegram. Recovery will mark
            # this row uncertain; a restart must never resend it.
            raise
        except Exception as e:
            ok, msg_id, err = False, None, str(e)

        if not ok:
            err_str = err or "unknown send error"
            if err_str.startswith("SAFETY_STOP:"):
                async with session_scope() as session:
                    await OutboundQueueRepository.mark_blocked_before_rpc(session, row.id, err_str)
                log.warning(f"OutboundConsumer: safety stop queue_id={row.id}: {err_str}")
                return
            async with session_scope() as session:
                await OutboundQueueRepository.mark_uncertain(session, row.id, err_str)
            log.warning(
                f"OutboundConsumer: send outcome uncertain (queue_id={row.id}, "
                f"account={row.account_id}, peer={row.peer_user_id}): {err_str}"
            )
            return

        # Persist delivery first, separately from optional history and counters.
        # If this commit fails, the row stays sending and later becomes uncertain.
        async with session_scope() as session:
            marked = await OutboundQueueRepository.mark_sent(session, row.id, msg_id)
        if not marked:
            log.warning(f"OutboundConsumer: sent row {row.id} was no longer sending")
            return

        async with session_scope() as session:
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
