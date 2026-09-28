"""
Фаза 2: безопасный scheduler прогрева аккаунтов.
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from utils.time import utcnow_naive
from typing import Optional

from telethon import errors
from telethon.errors import AuthKeyDuplicatedError, AuthKeyUnregisteredError, SessionRevokedError
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
from database.models import AccountSafetyState
from services.account_safety import check_account_gate, pause_account, reserve_send
from utils.logger import log
from utils.telemetry import telemetry_emitter
from services.warmup_schedule import (
    DEFAULT_TIME_ZONE, DEFAULT_WORK_END, DEFAULT_WORK_START,
    MIN_INTERVAL_SECONDS, is_off_hours, next_action_at, next_off_hours, parse_read_targets,
)


@dataclass
class WarmupPolicy:
    enabled: bool = WARMUP_ENABLED
    base_delay_sec: float = WARMUP_BASE_DELAY_SEC
    jitter_sec: float = WARMUP_JITTER_SEC
    daily_action_limit: int = WARMUP_DAILY_ACTION_LIMIT

    def next_delay(self) -> float:
        """Случайная пауза между действиями с jitter."""
        jitter = random.uniform(-self.jitter_sec, self.jitter_sec) if self.jitter_sec > 0 else 0.0
        return max(float(MIN_INTERVAL_SECONDS), self.base_delay_sec + jitter)

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
    _ = now or utcnow_naive()
    actions = (
        "read_dialogs",
        "read_channels",
    )
    weights = (0.65, 0.35)
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
        return parse_read_targets(raw)

    async def _ensure_worker_connected(self, account) -> Optional["Worker"]:
        from workers.manager import worker_manager

        worker = worker_manager.workers.get(account.id)
        if worker is None:
            worker = await worker_manager.ensure_worker(account.id)
            if worker is None:
                return None
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
        *,
        target_override: Optional[str] = None,
    ) -> tuple[str, str]:
        client = worker.client
        if client is None:
            return "skip", "no_client"

        if action == "read_dialogs":
            await client.get_dialogs(limit=8)
            return "ok", "dialogs_refreshed"

        if action == "read_channels":
            if not targets:
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
            target = target_override or random.choice(targets)
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

        return "skip", "unknown_action"

    async def _perform_account_action(
        self, worker: "Worker", account_id: int, action: str,
        targets: list[str], session,
    ) -> tuple[str, str]:
        """Share the worker's action lock and persisted safety gate with text sends."""
        if action == "set_reaction":
            if not targets:
                return "skip", "no_targets"
            target = random.choice(targets)
            async with worker._send_lock:
                allowed, reason = await reserve_send(
                    session, account_id, source="warmup_reaction", peer_ref=target,
                )
                if not allowed:
                    return "skip", reason
                return await self._do_action(
                    worker, action, targets, target_override=target,
                )
        allowed, reason = await check_account_gate(session, account_id)
        if not allowed:
            return "skip", reason
        return await self._do_action(worker, action, targets)

    async def _tick(self) -> None:
        async with session_scope() as session:
            candidates = await AccountRepository.list_warmup_candidates(session, limit=20)
            if not candidates:
                return

            for account in candidates:
                safety_state = await session.get(AccountSafetyState, account.id)
                if (
                    account.is_spam_blocked
                    or (account.flood_wait_until and account.flood_wait_until > utcnow_naive())
                    or (safety_state and safety_state.state != "ready")
                ):
                    continue
                now = utcnow_naive()
                # This counter belongs to warmup, not the account's shared last_reset.
                # The last warmup attempt supplies its UTC day across process restarts.
                if not account.warmup_last_action_at or account.warmup_last_action_at.date() != now.date():
                    account.warmup_actions_today = 0
                    if account.warmup_pause_reason == "daily_limit_reached":
                        account.warmup_pause_reason = None
                        account.warmup_paused_until = None
                    await session.commit()
                profile = await WarmupProfileRepository.get_effective_for_account(session, account)
                if profile and not profile.enabled:
                    await WarmupLogRepository.create(
                        session,
                        account_id=account.id,
                        action="skip_profile_disabled",
                        status="skip",
                    )
                    continue
                time_zone = getattr(profile, "time_zone", None) or DEFAULT_TIME_ZONE
                work_start = int(getattr(profile, "work_start_hour", DEFAULT_WORK_START))
                work_end = int(getattr(profile, "work_end_hour", DEFAULT_WORK_END))
                now = utcnow_naive()
                if not is_off_hours(now, time_zone, work_start, work_end):
                    # Persist the first quiet instant so a 5-second tick does not reconnect.
                    account.warmup_next_run_at = next_off_hours(now, time_zone, work_start, work_end)
                    await session.commit()
                    continue
                last_at = account.warmup_last_action_at
                if last_at and now < last_at + timedelta(seconds=MIN_INTERVAL_SECONDS):
                    account.warmup_next_run_at = last_at + timedelta(seconds=MIN_INTERVAL_SECONDS)
                    await session.commit()
                    continue
                active_policy = WarmupPolicy(
                    enabled=self._policy.enabled,
                    base_delay_sec=float(getattr(profile, "base_delay_sec", self._policy.base_delay_sec) or self._policy.base_delay_sec),
                    jitter_sec=float(getattr(profile, "jitter_sec", self._policy.jitter_sec) or self._policy.jitter_sec),
                    daily_action_limit=min(12, int(getattr(profile, "daily_action_limit", self._policy.daily_action_limit) or self._policy.daily_action_limit)),
                )
                if not active_policy.allow_action(int(account.warmup_actions_today or 0)):
                    next_day = (utcnow_naive() + timedelta(days=1)).replace(
                        hour=0, minute=0, second=0, microsecond=0,
                    )
                    await AccountRepository.set_warmup_pause(
                        session,
                        account.id,
                        until=next_day,
                        reason="daily_limit_reached",
                    )
                    await WarmupLogRepository.create(
                        session,
                        account_id=account.id,
                        action="pause_daily_limit",
                        status="skip",
                    )
                    continue

                allowed = set((getattr(profile, "allowed_actions", None) or "read_dialogs,read_channels").split(","))
                allowed &= {"read_dialogs", "read_channels", "set_reaction"}
                if not allowed:
                    await WarmupLogRepository.create(
                        session, account_id=account.id, action="skip_no_allowed_actions", status="skip",
                    )
                    continue
                action = choose_warmup_action()
                if action not in allowed:
                    action = random.choice(sorted(allowed))
                delay = active_policy.next_delay()
                targets = self._parse_targets(getattr(profile, "target_chats_text", "") or "")
                if action in ("read_channels", "set_reaction") and not targets:
                    action = "read_dialogs" if "read_dialogs" in allowed else action

                status = "ok"
                details = ""
                try:
                    worker = await self._ensure_worker_connected(account)
                    if not worker:
                        status = "skip"
                        details = "worker_unavailable"
                    else:
                        status, details = await self._perform_account_action(
                            worker, account.id, action, targets, session,
                        )
                        if action == "set_reaction" and status == "skip":
                            await WarmupLogRepository.create(
                                session, account_id=account.id, action="skip_reaction",
                                status="skip", details=details,
                            )
                            continue
                except errors.FloodWaitError as e:
                    pause_until = utcnow_naive() + timedelta(seconds=int(e.seconds or 60))
                    await pause_account(
                        session, account.id, reason_code="flood_wait", source="warmup",
                        state="cooling_down", resume_at=pause_until,
                    )
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
                except (AuthKeyDuplicatedError, AuthKeyUnregisteredError, SessionRevokedError) as e:
                    await pause_account(
                        session, account.id, reason_code="auth_invalid", source="warmup",
                        state="needs_reauth",
                    )
                    await WarmupLogRepository.create(
                        session, account_id=account.id, action=f"pause_auth_{action}",
                        status="skip", details=type(e).__name__,
                    )
                    continue
                except Exception as e:
                    status = "error"
                    details = f"err={str(e)[:180]}"

                next_run = next_action_at(utcnow_naive(), last_at, delay, 0)
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
