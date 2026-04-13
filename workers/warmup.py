"""
Фаза 2: безопасный scheduler прогрева аккаунтов.
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from telethon import errors
from telethon.tl.functions.messages import SendReactionRequest
from telethon.tl.types import ReactionEmoji

from bot.config import (
    WARMUP_BASE_DELAY_SEC,
    WARMUP_DAILY_ACTION_LIMIT,
    WARMUP_ENABLED,
    WARMUP_JITTER_SEC,
)
from database.session import session_scope
from database.repositories import (
    AccountRepository,
    WarmupLogRepository,
    WarmupProfileRepository,
)
from utils.logger import log
from utils.telemetry import telemetry_emitter


@dataclass
class WarmupPolicy:
    enabled: bool = WARMUP_ENABLED
    base_delay_sec: float = WARMUP_BASE_DELAY_SEC
    jitter_sec: float = WARMUP_JITTER_SEC
    daily_action_limit: int = WARMUP_DAILY_ACTION_LIMIT

    def next_delay(self) -> float:
        """Случайная пауза между действиями с jitter."""
        if self.base_delay_sec <= 0:
            return 0.0
        jitter = random.uniform(-self.jitter_sec, self.jitter_sec) if self.jitter_sec > 0 else 0.0
        return max(1.0, self.base_delay_sec + jitter)

    def allow_action(self, actions_today: int) -> bool:
        """Проверка дневного лимита."""
        return actions_today < max(1, int(self.daily_action_limit))


def should_pause_on_signal(
    *,
    flood_wait_seconds: Optional[int] = None,
    spam_block_detected: bool = False,
) -> bool:
    """
    Stop-правила прогрева.
    Любой FloodWait/спам-сигнал — приостановить действия аккаунта.
    """
    if spam_block_detected:
        return True
    if flood_wait_seconds and flood_wait_seconds > 0:
        return True
    return False


def choose_warmup_action(now: Optional[datetime] = None) -> str:
    """
    Умеренный профиль действий (без агрессивного поведения).
    """
    _ = now or datetime.utcnow()
    actions = (
        "read_dialogs",
        "read_channels",
        "set_reaction",
        "short_reply",
    )
    # Сдвиг в сторону низкорисковых действий.
    weights = (0.45, 0.35, 0.15, 0.05)
    return random.choices(actions, weights=weights, k=1)[0]


class WarmupRunner:
    """Минимальный безопасный планировщик прогрева."""

    def __init__(self):
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._policy = WarmupPolicy()

    def start(self) -> None:
        if not self._policy.enabled:
            log.info("WarmupRunner disabled by config")
            return
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="warmup-runner")
        log.info("WarmupRunner started")

    async def stop(self) -> None:
        self._stop.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("WarmupRunner stopped")

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as e:
                log.error(f"WarmupRunner tick error: {e}")
            await asyncio.sleep(5)

    @staticmethod
    def _parse_targets(raw: str) -> list[str]:
        out: list[str] = []
        for line in (raw or "").splitlines():
            item = line.strip()
            if not item:
                continue
            item = item.replace("https://t.me/", "").replace("http://t.me/", "")
            item = item.split("/", 1)[0].strip()
            if not item:
                continue
            if not item.startswith("@"):
                item = f"@{item}"
            out.append(item)
        return out

    async def _ensure_worker_connected(self, account) -> Optional["Worker"]:
        from workers.manager import Worker, worker_manager

        worker = worker_manager.workers.get(account.id)
        if worker is None:
            session_path = Path("data/sessions") / f"{account.session_name}.session"
            if not session_path.exists():
                return None
            worker = Worker(account, session_path, account.proxy)
            worker_manager.workers[account.id] = worker
        if not worker.is_connected:
            ok = await worker.connect()
            if not ok:
                return None
        return worker

    async def _do_action(
        self,
        worker: "Worker",
        action: str,
        targets: list[str],
    ) -> tuple[str, str]:
        client = worker.client
        if client is None:
            return "skip", "no_client"

        if action == "read_dialogs":
            await client.get_dialogs(limit=8)
            return "ok", "dialogs_refreshed"

        if action in ("read_channels", "short_reply"):
            if not targets:
                await client.get_dialogs(limit=5)
                return "skip", "no_targets"
            target = random.choice(targets)
            entity = await client.get_entity(target)
            msgs = await client.get_messages(entity, limit=3)
            if msgs:
                await client.send_read_acknowledge(entity, max_id=msgs[0].id)
            return "ok", f"read:{target}"

        if action == "set_reaction":
            if not targets:
                return "skip", "no_targets"
            target = random.choice(targets)
            entity = await client.get_entity(target)
            msgs = await client.get_messages(entity, limit=10)
            msg = next((m for m in msgs if getattr(m, "id", None)), None)
            if not msg:
                return "skip", f"empty:{target}"
            emoji = random.choice(["👍", "🔥", "❤️"])
            await client(
                SendReactionRequest(
                    peer=entity,
                    msg_id=msg.id,
                    reaction=[ReactionEmoji(emoticon=emoji)],
                    add_to_recent=False,
                )
            )
            return "ok", f"reaction:{target}:{emoji}"

        await client.get_dialogs(limit=5)
        return "skip", "unknown_action"

    async def _tick(self) -> None:
        async with session_scope() as session:
            await AccountRepository.reset_warmup_daily(session)
            candidates = await AccountRepository.list_warmup_candidates(session, limit=20)
            if not candidates:
                return

            for account in candidates:
                profile = await WarmupProfileRepository.get_effective_for_account(session, account)
                if profile and not profile.enabled:
                    await WarmupLogRepository.create(
                        session,
                        account_id=account.id,
                        action="skip_profile_disabled",
                        status="skip",
                    )
                    continue
                active_policy = WarmupPolicy(
                    enabled=self._policy.enabled,
                    base_delay_sec=float(getattr(profile, "base_delay_sec", self._policy.base_delay_sec) or self._policy.base_delay_sec),
                    jitter_sec=float(getattr(profile, "jitter_sec", self._policy.jitter_sec) or self._policy.jitter_sec),
                    daily_action_limit=int(getattr(profile, "daily_action_limit", self._policy.daily_action_limit) or self._policy.daily_action_limit),
                )
                if not active_policy.allow_action(int(account.warmup_actions_today or 0)):
                    await AccountRepository.set_warmup_pause(
                        session,
                        account.id,
                        until=datetime.utcnow(),
                        reason="daily_limit_reached",
                    )
                    await WarmupLogRepository.create(
                        session,
                        account_id=account.id,
                        action="pause_daily_limit",
                        status="skip",
                    )
                    continue

                action = choose_warmup_action()
                delay = active_policy.next_delay()
                targets = self._parse_targets(getattr(profile, "target_chats_text", "") or "")

                status = "ok"
                details = ""
                try:
                    worker = await self._ensure_worker_connected(account)
                    if not worker:
                        status = "skip"
                        details = "worker_unavailable"
                    else:
                        status, details = await self._do_action(worker, action, targets)
                except errors.FloodWaitError as e:
                    pause_until = datetime.utcnow() + timedelta(seconds=int(e.seconds or 60))
                    await AccountRepository.set_warmup_pause(
                        session,
                        account.id,
                        until=pause_until,
                        reason=f"floodwait:{int(e.seconds or 60)}",
                    )
                    await WarmupLogRepository.create(
                        session,
                        account_id=account.id,
                        action=f"pause_floodwait_{action}",
                        status="skip",
                        details=f"seconds={int(e.seconds or 60)}",
                    )
                    continue
                except Exception as e:
                    status = "error"
                    details = f"err={str(e)[:180]}"

                next_run = datetime.utcnow() + timedelta(seconds=delay)
                await AccountRepository.mark_warmup_action(
                    session,
                    account_id=account.id,
                    next_run_at=next_run,
                )
                await WarmupLogRepository.create(
                    session,
                    account_id=account.id,
                    action=action,
                    status=status,
                    details=f"delay={delay:.1f}s {details}",
                )
                if status in ("error", "skip"):
                    await telemetry_emitter.emit_event(
                        "warning" if status == "skip" else "error",
                        "warmup_action",
                        f"Warmup {status}: {action}",
                        payload={"account_id": account.id, "action": action, "details": details},
                    )
                else:
                    await telemetry_emitter.emit_metric(
                        "warmup_actions_ok",
                        1.0,
                        tags={"account_id": account.id, "action": action},
                    )


warmup_runner = WarmupRunner()
