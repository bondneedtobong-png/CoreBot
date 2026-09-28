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
from utils.time import utcnow_naive
from typing import Optional

from sqlalchemy import or_, select, update
from telethon.errors import AuthKeyDuplicatedError, AuthKeyUnregisteredError, SessionRevokedError

from database.models import (
    Account, AccountHealthCheck, AccountSafetyEvent, AccountSafetyState, AccountStatus, BotCommand,
    EngagementDraft, Mailing, MailingRun, MailingStatus, Proxy, ProxyType,
)
from database.session import session_scope
from database.repositories import MailingRepository
from database.sqlite_pragmas import commit_with_busy_retry, execute_with_busy_retry
from services.account_safety import check_account_gate
from services.engagement_guard import (
    EngagementTargetError, discover_managed_messages, inspect_managed_message,
    parse_group_ref,
)
from utils.background_tasks import background_tasks
from utils.logger import log


async def _check_assigned_proxy(config: tuple) -> bool:
    """Check the assigned SOCKS5 route freshly; fail closed if checker is missing."""
    try:
        from utils.proxy_checker import check_proxy
    except ImportError:
        return False
    ok, _exit_ip = await check_proxy(
        "socks5", config[1], config[2], config[3], config[4], timeout=15,
    )
    return bool(ok)


class BotCommandConsumer:
    """Бэкграунд-задача внутри процесса бота."""

    def __init__(self, *, idle_sleep_sec: float = 2.0):
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._idle_sleep_sec = float(idle_sleep_sec)
        self._did_recover_engagement = False
        self._did_recover_mailings = False
        self._did_recover_safety = False
        self._did_recover_health = False
        self._did_recover_community_batches = False
        self._did_recover_owned_chat = False

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._did_recover_engagement = False
        self._did_recover_mailings = False
        self._did_recover_safety = False
        self._did_recover_health = False
        self._did_recover_community_batches = False
        self._did_recover_owned_chat = False
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
            # Task 08: heartbeat для CP (shared corebot.db, без нового порта).
            from control_plane.services.heartbeat import (
                BOTCMD_COMPONENT,
                record_tick_best_effort,
            )

            await record_tick_best_effort(BOTCMD_COMPONENT)
            if processed == 0:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self._idle_sleep_sec
                    )
                except asyncio.TimeoutError:
                    pass

    async def _tick(self) -> int:
        if not self._did_recover_owned_chat:
            from services.owned_chat_campaign import recover_campaign_sends

            await recover_campaign_sends(scope_factory=session_scope)
            self._did_recover_owned_chat = True
        if not self._did_recover_community_batches:
            from services.community_link_batch_jobs import recover_owned_community_batches

            await recover_owned_community_batches(scope_factory=session_scope)
            self._did_recover_community_batches = True
        if not self._did_recover_safety:
            await self._recover_resume_after_restart()
            self._did_recover_safety = True
        if not self._did_recover_health:
            await self._recover_health_checks_after_restart()
            self._did_recover_health = True
        if not self._did_recover_mailings:
            from workers.manager import worker_manager

            if not worker_manager.is_mailing_busy():
                await self._recover_mailing_runs_after_restart()
                self._did_recover_mailings = True
        if not self._did_recover_engagement:
            await self._recover_engagement_after_restart()
            self._did_recover_engagement = True
        async with session_scope() as session:
            res = await session.execute(
                select(BotCommand)
                .where(
                    BotCommand.status == "pending",
                    or_(BotCommand.not_before.is_(None), BotCommand.not_before <= utcnow_naive()),
                )
                .order_by(BotCommand.id.asc())
                .limit(20)
            )
            rows = list(res.scalars().all())
            if not rows:
                return 0
            claimed = []
            for row in rows:
                result = await execute_with_busy_retry(
                    session,
                    update(BotCommand)
                    .where(BotCommand.id == int(row.id), BotCommand.status == "pending")
                    .values(status="processing"),
                    op_name="botcmd-claim",
                )
                if result.rowcount == 1:
                    claimed.append(row)
            await commit_with_busy_retry(session, op_name="botcmd-claim")

        for row in claimed:
            await self._process_one(row)
        return len(claimed)

    async def _recover_resume_after_restart(self) -> None:
        """A crashed freshness check must leave the safety gate closed."""
        async with session_scope() as session:
            rows = list((await session.execute(
                select(BotCommand).where(
                    BotCommand.command == "account_safety.resume",
                    BotCommand.status == "processing",
                )
            )).scalars().all())
        for row in rows:
            try:
                args = json.loads(row.args_json or "{}")
                account_id = int(args["account_id"])
                expected_at = datetime.fromisoformat(args["state_updated_at"])
            except (KeyError, TypeError, ValueError):
                await self._mark_failed(row, "verification_interrupted")
                continue
            await self._fail_account_resume(
                row, account_id, expected_at, "verification_interrupted",
            )

    async def _recover_health_checks_after_restart(self) -> None:
        """A claimed check has an unknown outcome after restart; do not replay it."""
        async with session_scope() as session:
            ids = list((await session.execute(
                select(BotCommand.id).where(
                    BotCommand.command == "account_safety.health_check",
                    BotCommand.status == "processing",
                )
            )).scalars())
            if not ids:
                return
            now = utcnow_naive()
            await execute_with_busy_retry(
                session,
                update(AccountHealthCheck)
                .where(AccountHealthCheck.command_id.in_(ids),
                       AccountHealthCheck.status.in_(["pending", "processing"]))
                .values(status="interrupted", reason_code="check_interrupted", finished_at=now),
                op_name="health-check-recover-history",
            )
            await execute_with_busy_retry(
                session,
                update(BotCommand).where(BotCommand.id.in_(ids))
                .values(status="failed", error="check_interrupted", processed_at=now),
                op_name="health-check-recover-command",
            )
            await commit_with_busy_retry(session, op_name="health-check-recover")

    async def _recover_mailing_runs_after_restart(self) -> None:
        """Mark unknown outcomes after a process restart; never replay sends."""
        async with session_scope() as session:
            running = list((await session.execute(
                select(MailingRun).where(MailingRun.status == "running")
            )).scalars().all())
            queued = list((await session.execute(
                select(MailingRun).where(MailingRun.status == "queued")
            )).scalars().all())
            if not running and not queued:
                return
            now = utcnow_naive()
            if running:
                run_ids = [run.id for run in running]
                mailing_ids = [run.mailing_id for run in running]
                await execute_with_busy_retry(
                    session,
                    update(MailingRun)
                    .where(MailingRun.id.in_(run_ids))
                    .values(status="interrupted", finished_at=now),
                    op_name="mailing-recover-runs",
                )
                await execute_with_busy_retry(
                    session,
                    update(Mailing)
                    .where(Mailing.id.in_(mailing_ids), Mailing.status == MailingStatus.RUNNING)
                    .values(status=MailingStatus.ERROR, updated_at=now),
                    op_name="mailing-recover-status",
                )
            if queued:
                commands = list((await session.execute(select(BotCommand).where(
                    BotCommand.command == "mailing.start",
                    BotCommand.status.in_(["pending", "processing", "done"]),
                ))).scalars().all())
                linked: dict[int, BotCommand] = {}
                for command in commands:
                    try:
                        linked[int(json.loads(command.args_json or "{}")["run_id"])] = command
                    except (KeyError, TypeError, ValueError):
                        continue
                for run in queued:
                    command = linked.get(int(run.id))
                    if command is not None and command.status == "pending":
                        continue  # The frozen audience is safe to run after restart.
                    run.status = "interrupted"
                    run.finished_at = now
                    if command is not None:
                        command.status = "failed"
                        command.error = "interrupted_before_start"
                        command.processed_at = now
            await commit_with_busy_retry(session, op_name="mailing-recover")

    async def _recover_engagement_after_restart(self) -> None:
        """Never replay a community reply whose Telegram outcome is unknown."""
        async with session_scope() as session:
            ids = list((await session.execute(
                select(BotCommand.id).where(
                    BotCommand.command == "engagement.send", BotCommand.status == "processing"
                )
            )).scalars())
            now = utcnow_naive()
            if ids:
                await execute_with_busy_retry(
                    session,
                    update(EngagementDraft)
                    .where(EngagementDraft.command_id.in_(ids), EngagementDraft.status.in_(["queued", "sending"]))
                    .values(status="uncertain", error_code="uncertain_after_restart", updated_at=now),
                    op_name="engagement-recover-drafts",
                )
                await execute_with_busy_retry(
                    session,
                    update(BotCommand)
                    .where(BotCommand.id.in_(ids))
                    .values(status="failed", error="uncertain_after_restart", processed_at=now),
                    op_name="engagement-recover-commands",
                )
            await execute_with_busy_retry(
                session,
                update(BotCommand).where(
                    BotCommand.command == "engagement.preview", BotCommand.status == "processing"
                ).values(status="failed", error="preview_interrupted", processed_at=now),
                op_name="engagement-recover-preview",
            )
            await execute_with_busy_retry(
                session,
                update(BotCommand).where(
                    BotCommand.command == "engagement.discover", BotCommand.status == "processing"
                ).values(status="failed", error="discovery_interrupted", processed_at=now),
                op_name="engagement-recover-discovery",
            )
            await commit_with_busy_retry(session, op_name="engagement-recover")

    async def _process_one(self, row: BotCommand) -> None:
        from workers.manager import worker_manager  # локальный импорт

        cmd = (row.command or "").strip().lower()
        try:
            args = json.loads(row.args_json or "{}")
        except Exception:
            args = {}

        try:
            if cmd == "owned_chat_campaign.send":
                from services.owned_chat_campaign import process_campaign_send

                await process_campaign_send(int(row.id))
                return

            if cmd == "community_link_check.batch":
                from services.community_link_batch_jobs import process_owned_community_batch_step

                await process_owned_community_batch_step(int(row.id), scope_factory=session_scope)
                return

            if cmd == "account_safety.health_check":
                await self._perform_account_health_check(row, args, worker_manager)
                return

            if cmd == "account_safety.resume":
                await self._verify_account_resume(row, args, worker_manager)
                return

            if cmd == "engagement.send":
                await self._send_engagement(row, int(args.get("draft_id") or 0), worker_manager)
                return

            if cmd == "engagement.preview":
                await self._preview_engagement(row, args, worker_manager)
                return

            if cmd == "engagement.discover":
                await self._discover_engagement(row, args, worker_manager)
                return

            if cmd == "mailing.start":
                mailing_id = int(args.get("mailing_id") or 0)
                queued_run_id = int(args.get("run_id") or 0) or None
                if mailing_id <= 0:
                    raise ValueError("mailing_id required")
                if queued_run_id is not None:
                    run_error = None
                    async with session_scope() as session:
                        run = await session.get(MailingRun, queued_run_id)
                        mailing = await session.get(Mailing, mailing_id)
                        if run is None or run.mailing_id != mailing_id or run.status != "queued":
                            run_error = "queued_run_unavailable"
                        elif mailing is None:
                            run_error = "mailing_unavailable"
                        elif run.config_json != MailingRepository.run_config_json(mailing):
                            run_error = "config_changed_after_queue"
                        if (run_error and run is not None and run.mailing_id == mailing_id
                                and run.status == "queued"):
                            await MailingRepository.finish_run(session, queued_run_id, "rejected")
                    if run_error:
                        await self._mark_failed(row, run_error)
                        return
                # Не врём панели «ok: scheduled», если пул занят:
                # start_mailing в этом случае молча выйдет по _mailing_busy.
                is_busy = (
                    worker_manager.is_mailing_busy()
                    if hasattr(worker_manager, "is_mailing_busy")
                    else bool(getattr(worker_manager, "_mailing_busy", False))
                )
                if is_busy:
                    running = getattr(worker_manager, "current_mailing_id", None)
                    if queued_run_id is not None:
                        async with session_scope() as session:
                            run = await session.get(MailingRun, queued_run_id)
                            if run and run.mailing_id == mailing_id and run.status == "queued":
                                await MailingRepository.finish_run(session, queued_run_id, "rejected")
                    await self._mark_failed(
                        row,
                        f"mailing already running (current={running}, requested={mailing_id})",
                    )
                    return
                # запускаем рассылку как отдельную фоновую таску, чтобы консьюмер
                # не блокировался на длинной кампании
                mailing_task = (
                    worker_manager.start_mailing(mailing_id, queued_run_id=queued_run_id)
                    if queued_run_id is not None
                    else worker_manager.start_mailing(mailing_id)
                )
                background_tasks.create(mailing_task, name=f"mailing-{mailing_id}")
                await self._mark_done(row, "ok: scheduled")
                return

            if cmd in ("mailing.pause", "mailing.stop"):
                mailing_id = int(args.get("mailing_id") or 0)
                if mailing_id <= 0:
                    raise ValueError("mailing_id required")
                active_id = getattr(worker_manager, "current_mailing_id", None)
                if active_id != mailing_id:
                    await self._mark_failed(
                        row,
                        f"mailing_not_active (current={active_id}, requested={mailing_id})",
                    )
                    return
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
                            await commit_with_busy_retry(session, op_name="botcmd-mailing-status")
                await self._mark_done(row, "ok: stop signal sent")
                return

            raise ValueError(f"unknown command: {cmd}")
        except Exception as e:
            if cmd == "owned_chat_campaign.send":
                log.error(f"Owned chat send {row.id} interrupted: {type(e).__name__}")
                await self._mark_failed(row, "outcome_uncertain")
            elif cmd == "community_link_check.batch":
                log.error(f"Community batch {row.id} failed: {type(e).__name__}")
                await self._mark_failed(row, "batch_error")
            elif cmd == "account_safety.health_check":
                log.error(f"Account health check {row.id} failed: {type(e).__name__}")
                await self._finish_account_health_check(
                    row, status="failed", reason="check_error",
                )
            elif cmd == "account_safety.resume":
                try:
                    account_id = int(args["account_id"])
                    expected_at = datetime.fromisoformat(args["state_updated_at"])
                    await self._fail_account_resume(
                        row, account_id, expected_at, "verification_error",
                    )
                except Exception as recovery_error:
                    log.error(f"Account verification cleanup failed: {recovery_error}")
                    await self._mark_failed(row, "verification_error")
            else:
                await self._mark_failed(row, str(e))

    async def _finish_account_health_check(
        self, row: BotCommand, *, status: str, reason: str,
        proxy_state: str = "unknown", auth_state: str = "unknown",
        proxy_id: int | None = None, proxy_config: tuple | None = None,
    ) -> None:
        """Persist diagnostic facts and command outcome in one transaction."""
        async with session_scope() as session:
            check = (await session.execute(
                select(AccountHealthCheck).where(AccountHealthCheck.command_id == row.id)
            )).scalar_one_or_none()
            now = utcnow_naive()
            if check is not None and check.status in ("pending", "processing"):
                account = await session.get(Account, check.account_id)
                state = await session.get(AccountSafetyState, check.account_id)
                if proxy_config is not None and account is not None:
                    current = await session.get(Proxy, account.proxy_id) if account.proxy_id else None
                    current_config = (
                        current.proxy_type, current.host, current.port,
                        current.username, current.password,
                    ) if current else None
                    if account.proxy_id != proxy_id or current_config != proxy_config:
                        proxy_state, auth_state, reason = "changed", "unknown", "proxy_assignment_changed"
                if account is not None and (account.is_spam_blocked or account.status in (
                    AccountStatus.BANNED, AccountStatus.SPAM_BLOCKED,
                )):
                    check.safety_state = "platform_restricted"
                    check.safety_reason_code = "platform_restricted"
                elif account is not None and account.flood_wait_until and account.flood_wait_until > now:
                    check.safety_state = "cooling_down"
                    check.safety_reason_code = "flood_wait"
                elif state is not None:
                    check.safety_state = state.state
                    check.safety_reason_code = state.reason_code
                else:
                    check.safety_state = "ready" if account is not None else None
                check.status = status
                check.reason_code = reason
                check.proxy_id = proxy_id
                check.proxy_state = proxy_state
                check.auth_state = auth_state
                check.finished_at = now
            await execute_with_busy_retry(
                session,
                update(BotCommand).where(BotCommand.id == row.id, BotCommand.status == "processing")
                .values(status="done" if status == "completed" else "failed",
                        error=None if status == "completed" else reason, processed_at=now),
                op_name="health-check-finish-command",
            )
            await commit_with_busy_retry(session, op_name="health-check-finish")

    async def _perform_account_health_check(self, row: BotCommand, args: dict, worker_manager) -> None:
        """Check only assigned SOCKS5 and own auth via the bot's leased Worker."""
        try:
            account_id = int(args["account_id"])
        except (KeyError, TypeError, ValueError):
            await self._finish_account_health_check(row, status="failed", reason="invalid_check_request")
            return
        async with session_scope() as session:
            check = (await session.execute(
                select(AccountHealthCheck).where(AccountHealthCheck.command_id == row.id)
            )).scalar_one_or_none()
            valid = check is not None and check.account_id == account_id and check.status == "pending"
            if valid:
                check.status = "processing"
                check.started_at = utcnow_naive()
                await commit_with_busy_retry(session, op_name="health-check-start")
                account = await session.get(Account, account_id)
                proxy_id = account.proxy_id if account is not None else None
                proxy = await session.get(Proxy, proxy_id) if proxy_id is not None else None
                proxy_config = (
                    proxy.proxy_type, proxy.host, proxy.port, proxy.username, proxy.password,
                ) if proxy is not None else None
        if not valid:
            await self._finish_account_health_check(row, status="failed", reason="invalid_check_request")
            return
        if account is None:
            await self._finish_account_health_check(row, status="failed", reason="account_unavailable")
            return
        if proxy_config is None or proxy_config[0] != ProxyType.SOCKS5:
            await self._finish_account_health_check(
                row, status="completed", reason="socks5_proxy_required",
                proxy_state="missing", proxy_id=proxy_id,
            )
            return
        try:
            proxy_ok = await _check_assigned_proxy(proxy_config)
        except Exception:
            proxy_ok = False
        if not proxy_ok:
            await self._finish_account_health_check(
                row, status="completed", reason="proxy_unavailable",
                proxy_state="unavailable", proxy_id=proxy_id, proxy_config=proxy_config,
            )
            return

        worker = worker_manager.workers.get(account_id)
        if worker is None:
            worker = await worker_manager.ensure_worker(account_id)
        if worker is None:
            await self._finish_account_health_check(
                row, status="completed", reason="session_unavailable",
                proxy_state="ok", auth_state="unavailable",
                proxy_id=proxy_id, proxy_config=proxy_config,
            )
            return
        worker_proxy = getattr(worker, "proxy", None)
        worker_config = (
            worker_proxy.proxy_type, worker_proxy.host, worker_proxy.port,
            worker_proxy.username, worker_proxy.password,
        ) if worker_proxy is not None else None
        if getattr(worker_proxy, "id", None) != proxy_id or worker_config != proxy_config:
            await self._finish_account_health_check(
                row, status="completed", reason="worker_proxy_mismatch",
                proxy_state="changed", proxy_id=proxy_id, proxy_config=proxy_config,
            )
            return

        try:
            if not getattr(worker, "is_connected", False) or worker.client is None:
                if not await worker.connect(quiet=True):
                    connect_error = getattr(worker, "last_connect_error", None)
                    invalid = connect_error == "unauthorized" or isinstance(connect_error, (
                        AuthKeyDuplicatedError, AuthKeyUnregisteredError, SessionRevokedError,
                    ))
                    await self._finish_account_health_check(
                        row, status="completed",
                        reason="auth_invalid" if invalid else "session_unavailable",
                        proxy_state="ok", auth_state="invalid" if invalid else "unavailable",
                        proxy_id=proxy_id, proxy_config=proxy_config,
                    )
                    return
            authorized = await worker.client.is_user_authorized()
            me = await worker.client.get_me() if authorized else None
            auth_state = "ok" if authorized and me is not None else "invalid"
            reason = "auth_proxy_verified" if auth_state == "ok" else "auth_invalid"
        except (AuthKeyDuplicatedError, AuthKeyUnregisteredError, SessionRevokedError):
            auth_state, reason = "invalid", "auth_invalid"
        except Exception:
            auth_state, reason = "unavailable", "session_check_error"
        await self._finish_account_health_check(
            row, status="completed", reason=reason,
            proxy_state="ok", auth_state=auth_state,
            proxy_id=proxy_id, proxy_config=proxy_config,
        )

    async def _fail_account_resume(
        self, row: BotCommand, account_id: int, expected_at: datetime,
        reason: str, *, state_name: str = "review_required",
    ) -> None:
        """Leave the account stopped and record a structured verification failure."""
        async with session_scope() as session:
            now = utcnow_naive()
            result = await execute_with_busy_retry(
                session,
                update(AccountSafetyState)
                .where(AccountSafetyState.account_id == account_id,
                       AccountSafetyState.state == "verifying",
                       AccountSafetyState.updated_at == expected_at)
                .values(state=state_name, reason_code=reason,
                        source="resume_check", updated_at=now),
                op_name="safety-resume-failed",
            )
            if result.rowcount == 1:
                session.add(AccountSafetyEvent(
                    account_id=account_id, event_type="verification_failed",
                    reason_code=reason, source="resume_check",
                    actor=row.requested_by or "operator", created_at=now,
                ))
                await commit_with_busy_retry(session, op_name="safety-resume-failed")
            else:
                await session.rollback()
        await self._mark_failed(row, reason)

    async def _verify_account_resume(self, row: BotCommand, args: dict, worker_manager) -> None:
        account_id = int(args.get("account_id") or 0)
        try:
            expected_at = datetime.fromisoformat(args["state_updated_at"])
        except (KeyError, TypeError, ValueError):
            await self._mark_failed(row, "invalid_verification_request")
            return
        if account_id <= 0:
            await self._mark_failed(row, "account_id_required")
            return
        async with session_scope() as session:
            account = await session.get(Account, account_id)
            state = await session.get(AccountSafetyState, account_id)
            stale = (account is None or state is None or state.state != "verifying"
                     or state.updated_at != expected_at)
            restricted = bool(account and (
                account.is_spam_blocked or account.status in (
                    AccountStatus.BANNED, AccountStatus.SPAM_BLOCKED,
                )
            ))
            cooling = bool(account and account.flood_wait_until
                           and account.flood_wait_until > utcnow_naive())
            proxy_id = account.proxy_id if account else None
            proxy = await session.get(Proxy, proxy_id) if proxy_id is not None else None
            proxy_config = (
                proxy.proxy_type, proxy.host, proxy.port, proxy.username, proxy.password,
            ) if proxy is not None else None
        if stale:
            await self._mark_failed(row, "stale_verification_request")
            return
        if restricted:
            await self._fail_account_resume(row, account_id, expected_at, "platform_restricted")
            return
        if cooling:
            await self._fail_account_resume(row, account_id, expected_at, "cooldown_active")
            return
        if proxy_id is None or proxy_config is None or proxy_config[0] != ProxyType.SOCKS5:
            await self._fail_account_resume(row, account_id, expected_at, "proxy_missing")
            return
        worker = worker_manager.workers.get(account_id)
        if worker is None:
            worker = await worker_manager.ensure_worker(account_id)
        if worker is None or getattr(getattr(worker, "proxy", None), "id", None) != proxy_id:
            await self._fail_account_resume(row, account_id, expected_at, "proxy_or_session_unavailable")
            return
        worker_proxy = worker.proxy
        if (worker_proxy.proxy_type, worker_proxy.host, worker_proxy.port,
                worker_proxy.username, worker_proxy.password) != proxy_config:
            await self._fail_account_resume(row, account_id, expected_at, "proxy_assignment_changed")
            return
        try:
            proxy_ok = await _check_assigned_proxy(proxy_config)
            if not proxy_ok:
                await self._fail_account_resume(
                    row, account_id, expected_at, "proxy_unavailable",
                )
                return
            if not worker.is_connected and not await worker.connect(quiet=True):
                auth_error = worker.last_connect_error
                reason = "auth_invalid" if (
                    auth_error == "unauthorized" or isinstance(auth_error, (
                        AuthKeyDuplicatedError, AuthKeyUnregisteredError, SessionRevokedError,
                    ))
                ) else "proxy_or_session_unavailable"
                await self._fail_account_resume(
                    row, account_id, expected_at, reason,
                    state_name="needs_reauth" if reason == "auth_invalid" else "review_required",
                )
                return
            if worker.client is None or not await worker.client.is_user_authorized():
                await self._fail_account_resume(
                    row, account_id, expected_at, "auth_invalid", state_name="needs_reauth",
                )
                return
            if await worker.client.get_me() is None:
                await self._fail_account_resume(
                    row, account_id, expected_at, "auth_invalid", state_name="needs_reauth",
                )
                return
        except (AuthKeyDuplicatedError, AuthKeyUnregisteredError, SessionRevokedError):
            await self._fail_account_resume(
                row, account_id, expected_at, "auth_invalid", state_name="needs_reauth",
            )
            return
        except Exception:
            await self._fail_account_resume(
                row, account_id, expected_at, "proxy_or_session_unavailable",
            )
            return

        async with session_scope() as session:
            account = await session.get(Account, account_id)
            state = await session.get(AccountSafetyState, account_id)
            if (account is None or state is None or state.state != "verifying"
                    or state.updated_at != expected_at
                    or account.proxy_id != proxy_id
                    or account.is_spam_blocked
                    or account.status in (AccountStatus.BANNED, AccountStatus.SPAM_BLOCKED)
                    or (state.resume_at and state.resume_at > utcnow_naive())
                    or (account.flood_wait_until and account.flood_wait_until > utcnow_naive())):
                await self._mark_failed(row, "stale_verification_request")
                return
            now = utcnow_naive()
            result = await execute_with_busy_retry(
                session,
                update(AccountSafetyState)
                .where(AccountSafetyState.account_id == account_id,
                       AccountSafetyState.state == "verifying",
                       AccountSafetyState.updated_at == expected_at)
                .values(state="ready", reason_code=None, source="resume_check",
                        updated_at=now, reviewed_at=now,
                        reviewed_by=row.requested_by or "operator", resume_at=None),
                op_name="safety-resume-verified",
            )
            if result.rowcount != 1:
                await session.rollback()
                await self._mark_failed(row, "stale_verification_request")
                return
            account.status = AccountStatus.ACTIVE
            account.flood_wait_until = None
            session.add(AccountSafetyEvent(
                account_id=account_id, event_type="resumed", reason_code="fresh_auth_proxy_verified",
                source="resume_check", actor=row.requested_by or "operator", created_at=now,
            ))
            await commit_with_busy_retry(session, op_name="safety-resume-verified")
        await self._mark_done(row, "auth_proxy_verified")

    async def _preview_engagement(self, command: BotCommand, args: dict, worker_manager) -> None:
        try:
            account_id = int(args["account_id"])
            link = str(args["message_link"])
            if account_id <= 0:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            await self._mark_failed(command, "invalid_preview_request")
            return
        async with session_scope() as session:
            allowed, reason = await check_account_gate(session, account_id)
        if not allowed:
            await self._mark_failed(command, f"safety_{reason}")
            return
        worker = worker_manager.workers.get(account_id)
        if worker is None:
            try:
                worker = await worker_manager.ensure_worker(account_id)
            except Exception:
                worker = None
        if worker is None:
            await self._mark_failed(command, "worker_unavailable")
            return
        try:
            if not worker.is_connected and not await worker.connect(quiet=True):
                raise EngagementTargetError("worker_unavailable")
            if worker.client is None:
                raise EngagementTargetError("worker_unavailable")
            result = await inspect_managed_message(worker.client, link)
        except EngagementTargetError as exc:
            await self._mark_failed(command, str(exc))
            return
        except Exception:
            await self._mark_failed(command, "preview_unavailable")
            return
        async with session_scope() as session:
            await execute_with_busy_retry(
                session,
                update(BotCommand).where(BotCommand.id == command.id,
                                         BotCommand.status == "processing")
                .values(status="done", error=None, processed_at=utcnow_naive(),
                        args_json=json.dumps({"account_id": account_id,
                                              "message_link": link, "result": result},
                                             ensure_ascii=False)),
                op_name="engagement-preview-done",
            )
            await commit_with_busy_retry(session, op_name="engagement-preview-done")

    async def _discover_engagement(self, command: BotCommand, args: dict, worker_manager) -> None:
        try:
            account_id = int(args["account_id"])
            group_ref = str(args["group_ref"])
            if account_id <= 0:
                raise ValueError
            parse_group_ref(group_ref)
        except (KeyError, TypeError, ValueError):
            await self._mark_failed(command, "invalid_discovery_request")
            return
        async with session_scope() as session:
            allowed, reason = await check_account_gate(session, account_id)
            account = await session.get(Account, account_id)
            proxy_id = account.proxy_id if account else None
            proxy = await session.get(Proxy, proxy_id) if proxy_id else None
            proxy_config = (
                proxy.proxy_type, proxy.host, proxy.port, proxy.username, proxy.password,
            ) if proxy else None
        if not allowed:
            await self._mark_failed(command, f"safety_{reason}")
            return
        if proxy_config is None or proxy_config[0] != ProxyType.SOCKS5:
            await self._mark_failed(command, "proxy_missing")
            return
        worker = worker_manager.workers.get(account_id)
        if worker is None:
            try:
                worker = await worker_manager.ensure_worker(account_id)
            except Exception:
                worker = None
        if worker is None:
            await self._mark_failed(command, "worker_unavailable")
            return
        worker_proxy = getattr(worker, "proxy", None)
        worker_config = (
            worker_proxy.proxy_type, worker_proxy.host, worker_proxy.port,
            worker_proxy.username, worker_proxy.password,
        ) if worker_proxy else None
        if getattr(worker_proxy, "id", None) != proxy_id or worker_config != proxy_config:
            await self._mark_failed(command, "proxy_assignment_changed")
            return
        try:
            if not await _check_assigned_proxy(proxy_config):
                raise EngagementTargetError("proxy_unavailable")
            if not worker.is_connected and not await worker.connect(quiet=True):
                raise EngagementTargetError("worker_unavailable")
            if worker.client is None or not await worker.client.is_user_authorized():
                raise EngagementTargetError("account_unavailable")
            messages = await discover_managed_messages(worker.client, group_ref)
        except EngagementTargetError as exc:
            await self._mark_failed(command, str(exc))
            return
        except Exception:
            await self._mark_failed(command, "discovery_unavailable")
            return
        async with session_scope() as session:
            await execute_with_busy_retry(
                session,
                update(BotCommand).where(BotCommand.id == command.id,
                                         BotCommand.status == "processing")
                .values(status="done", error=None, processed_at=utcnow_naive(),
                        args_json=json.dumps({"account_id": account_id,
                                              "group_ref": group_ref,
                                              "messages": messages}, ensure_ascii=False)),
                op_name="engagement-discovery-done",
            )
            await commit_with_busy_retry(session, op_name="engagement-discovery-done")

    async def _send_engagement(self, command: BotCommand, draft_id: int, worker_manager) -> None:
        if draft_id <= 0:
            await self._mark_failed(command, "draft_id required")
            return
        async with session_scope() as session:
            draft = await session.get(EngagementDraft, draft_id)
            if draft is None or draft.command_id != command.id or draft.status != "queued":
                await self._mark_failed(command, "draft not queued")
                return
            account_id = int(draft.account_id)
            peer_ref = draft.peer_ref
            reply_to = int(draft.reply_to_message_id)
            body = draft.draft_text
            mode = draft.mode
            source_text = draft.source_text
            allowed, gate_reason = await check_account_gate(session, account_id, peer_ref=peer_ref)
        if not allowed:
            await self._reject_engagement(command, draft_id, f"safety_{gate_reason}")
            return

        worker = worker_manager.workers.get(account_id)
        if worker is None:
            try:
                worker = await worker_manager.ensure_worker(account_id)
            except Exception:
                log.warning("Engagement worker setup failed")
        if worker is not None and not getattr(worker, "is_connected", False):
            try:
                if not await worker.connect(quiet=True):
                    worker = None
            except Exception:
                worker = None
        if worker is None:
            async with session_scope() as session:
                draft = await session.get(EngagementDraft, draft_id)
                draft.status = "failed"
                draft.error_code = "worker_unavailable"
                draft.updated_at = utcnow_naive()
                await commit_with_busy_retry(session, op_name="engagement-no-worker")
            await self._mark_failed(command, "worker_unavailable")
            return

        link = f"https://t.me/c/{peer_ref[4:]}/{reply_to}"
        try:
            if worker.client is None:
                raise EngagementTargetError("worker_unavailable")
            await inspect_managed_message(
                worker.client, link, expected_peer_id=int(peer_ref[4:]),
                expected_text=source_text,
            )
        except EngagementTargetError as exc:
            await self._reject_engagement(command, draft_id, str(exc))
            return
        except Exception:
            await self._reject_engagement(command, draft_id, "target_unavailable")
            return

        # Commit the sending state before Telegram RPC. A crash leaves it for
        # manual review; this command is never replayed automatically.
        async with session_scope() as session:
            draft = await session.get(EngagementDraft, draft_id)
            draft.status = "sending"
            draft.updated_at = utcnow_naive()
            await commit_with_busy_retry(session, op_name="engagement-sending")

        try:
            send_peer = int(peer_ref) if peer_ref.startswith("-100") and peer_ref[4:].isdigit() else peer_ref
            async def still_managed() -> bool:
                try:
                    await inspect_managed_message(
                        worker.client, link, expected_peer_id=int(peer_ref[4:]),
                        expected_text=source_text,
                    )
                    return True
                except Exception:
                    return False
            success, message_id, error, _peer_uid = await worker.send_message_with_typing(
                send_peer, body, typing_delay=0.0, use_typing=False, parse_mode=None,
                source="engagement_comment" if mode == "comment" else "engagement_chat",
                reply_to_message_id=reply_to,
                before_send=still_managed,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            success, message_id, error = False, None, "send_uncertain"

        if success:
            async with session_scope() as session:
                draft = await session.get(EngagementDraft, draft_id)
                draft.status = "sent"
                draft.telegram_message_id = message_id
                draft.error_code = None
                draft.updated_at = utcnow_naive()
                await commit_with_busy_retry(session, op_name="engagement-sent")
            await self._mark_done(command, "sent")
            return

        denied = bool(error and error.startswith("SAFETY_STOP:"))
        code = (
            "safety_stop" if denied else
            "target_changed" if error == "NEURO_WINDOW_CLOSED" else
            "peer_flood" if error and "PEER_FLOOD" in error else
            "flood_wait" if error and "FloodWait" in error else
            "send_uncertain"
        )
        async with session_scope() as session:
            draft = await session.get(EngagementDraft, draft_id)
            draft.status = "failed" if denied or code == "target_changed" else "uncertain"
            draft.error_code = code
            draft.updated_at = utcnow_naive()
            await commit_with_busy_retry(session, op_name="engagement-send-error")
        await self._mark_failed(command, code)

    async def _reject_engagement(self, command: BotCommand, draft_id: int, code: str) -> None:
        async with session_scope() as session:
            draft = await session.get(EngagementDraft, draft_id)
            if draft is not None and draft.status == "queued":
                draft.status = "failed"
                draft.error_code = code[:64]
                draft.updated_at = utcnow_naive()
                await commit_with_busy_retry(session, op_name="engagement-rejected")
        await self._mark_failed(command, code[:64])

    async def _mark_done(self, row: BotCommand, detail: str = "") -> None:
        async with session_scope() as session:
            await execute_with_busy_retry(
                session,
                update(BotCommand)
                .where(BotCommand.id == int(row.id))
                .values(
                    status="done",
                    error=detail or None,
                    processed_at=utcnow_naive(),
                ),
                op_name="botcmd-done",
            )
            await commit_with_busy_retry(session, op_name="botcmd-done")

    async def _mark_failed(self, row: BotCommand, err: str) -> None:
        log.warning(f"BotCommand {row.id} failed: {err}")
        async with session_scope() as session:
            await execute_with_busy_retry(
                session,
                update(BotCommand)
                .where(BotCommand.id == int(row.id))
                .values(
                    status="failed",
                    error=err,
                    processed_at=utcnow_naive(),
                ),
                op_name="botcmd-failed",
            )
            await commit_with_busy_retry(session, op_name="botcmd-failed")


bot_command_consumer = BotCommandConsumer()
