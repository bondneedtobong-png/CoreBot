"""Rate-limited Telegram alerting with recovery events (task 08).

Built **on top of** ``control_plane.services.alerts`` (called, never
modified): every evaluation is journaled with ``upsert_alert`` (fingerprint
dedup inside ``CP_INGEST_SUPPRESSION_SEC``, default 300 s), while Telegram
delivery is additionally gated by a per-kind cooldown.

Cooldown numbers
----------------

* :data:`ALERT_COOLDOWN_SEC` = 900 s (15 minutes) between two Telegram
  deliveries for the same fingerprint — strictly above the ingest
  suppression window, so a flapping signal cannot spam the owner faster
  than once per 15 minutes;
* recovery events (``«восстановлено»`` on a ``down → ok`` transition) bypass
  the cooldown — a state change is rare and the owner must see it at once;
* one-shot release events (``update exit 3/4`` via
  :func:`note_update_result`) are also sent immediately.

Kind coverage: SLO signals (bot/consumer death, parser crash, outbound
backlog, health failure sustained ``> 5 min``, FloodWait spike, repeated
DB-lock from ``SQLITE_BUSY_RETRY_STATS``, ingest-401 spike, disk / DB size /
backup age) plus task-06 release signals (``update exit 3`` rolled back,
``update exit 4`` rollback incomplete, version ``MISMATCH`` live vs
deployed). State (last delivery per fingerprint, previous component states,
``down_since`` markers, busy baseline) lives in :class:`WatchdogState`, so
tests inject a fresh instance while production uses the module singleton.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional

from utils.time import utcnow_naive

from control_plane.services.snapshot import (
    BOTCMD_BACKLOG_WARN,
    DEGRADED,
    DOWN,
    FLOODWAIT_SPIKE_PER_HOUR,
    INGEST_401_SPIKE_COUNT,
    OK,
    OUTBOUND_BACKLOG_CRIT,
    OUTBOUND_BACKLOG_WARN,
    SERVING_COMPONENTS,
    SnapshotInputs,
)

ALERT_COOLDOWN_SEC = 900

#: Sustained-down window before the ``health_failure`` alert fires
#: (SLO #3: ``/health/ready`` non-200 ``> 5 minutes``).
HEALTH_DOWN_SUSTAIN_SEC = 300

RECOVERED_WORD = "восстановлено"


@dataclass
class AlertSpec:
    kind: str
    severity: str  # info | warning | critical
    title: str
    details: str
    fingerprint: str
    recovery: bool = False


@dataclass
class WatchdogState:
    last_sent: dict[str, datetime] = field(default_factory=dict)
    components: dict[str, str] = field(default_factory=dict)
    down_since: dict[str, datetime] = field(default_factory=dict)
    busy_baseline: Optional[dict[str, int]] = None


STATE = WatchdogState()

Sender = Callable[[str, str], Awaitable[None]]


def cooldown_for(kind: str) -> int:
    """Per-kind cooldown seconds (single number today: 15 minutes)."""
    return ALERT_COOLDOWN_SEC


def should_notify(state: WatchdogState, fingerprint: str, now: datetime) -> bool:
    last = state.last_sent.get(fingerprint)
    if last is None:
        return True
    try:
        return (now.replace(tzinfo=None) - last.replace(tzinfo=None)) >= timedelta(
            seconds=ALERT_COOLDOWN_SEC
        )
    except Exception:
        return True


def _busy_delta(state: WatchdogState, current: dict[str, int]) -> dict[str, int]:
    base = state.busy_baseline or {}
    delta = {}
    for key in ("attempts", "retries", "exhausted", "failed_fast"):
        try:
            delta[key] = max(0, int(current.get(key, 0)) - int(base.get(key, 0)))
        except Exception:
            delta[key] = 0
    return delta


def evaluate(
    state: WatchdogState,
    snapshot: dict[str, Any],
    inputs: SnapshotInputs,
    now: datetime,
    *,
    live_sha: str = "",
    deployed_sha: str = "",
) -> list[AlertSpec]:
    """Turn a snapshot (+ release identity) into alert/recovery specs.

    Pure except for mutating ``state`` (transition bookkeeping + busy
    baseline refresh). Delivery gating happens in :func:`dispatch`.
    """
    specs: list[AlertSpec] = []
    components: dict[str, Any] = snapshot.get("components", {}) or {}

    def comp(name: str) -> dict[str, Any]:
        return components.get(name) or {}

    def last_seen(name: str) -> str:
        return comp(name).get("last_seen") or "unknown"

    # --- serving components: down --------------------------------------
    bot = comp("bot")
    if bot.get("state") == DOWN:
        specs.append(
            AlertSpec(
                kind="bot_heartbeat_stale",
                severity="critical",
                title="CoreBot: процесс бота не отвечает",
                details=(
                    f"bot heartbeat stale > 120 c (SLO #1: > 2 мин). "
                    f"Последний живой tick: {last_seen('bot')}."
                ),
                fingerprint="heartbeat:bot",
            )
        )
    for consumer in ("consumer:outbound", "consumer:bot_command"):
        if comp(consumer).get("state") == DOWN:
            specs.append(
                AlertSpec(
                    kind="consumer_death",
                    severity="critical",
                    title=f"CoreBot: consumer умер ({consumer})",
                    details=(
                        f"Последний успешный tick: {last_seen(consumer)} "
                        f"(порог 120 c, опрос 60 c ⇒ обнаружение <= 180 c)."
                    ),
                    fingerprint=f"consumer:{consumer}",
                )
            )
    parser = comp("parser")
    if parser.get("state") == DOWN:
        specs.append(
            AlertSpec(
                kind="parser_crash",
                severity="critical",
                title="CoreBot: parser crash",
                details=f"embedded parser task: {(parser.get('detail') or {}).get('task_status')}.",
                fingerprint="parser:crash",
            )
        )
    bg = comp("background_tasks")
    if bg.get("state") == DOWN:
        failures = (bg.get("detail") or {}).get("failures", [])
        specs.append(
            AlertSpec(
                kind="supervisor_task_failed",
                severity="critical",
                title="CoreBot: упавшая фоновая задача",
                details=f"background_tasks failed: {failures} (readiness 503).",
                fingerprint="supervisor:failed",
            )
        )
    for db_name in ("bot_db", "control_plane_db"):
        if comp(db_name).get("state") == DOWN:
            specs.append(
                AlertSpec(
                    kind="health_failure",
                    severity="critical",
                    title=f"CoreBot: БД недоступна ({db_name})",
                    details=f"{db_name} probe failed (readiness 503).",
                    fingerprint=f"health:{db_name}",
                )
            )

    # --- health sustained-down (SLO #3: readiness non-200 > 5 min) ------
    overall = snapshot.get("overall")
    marker = state.down_since.get("overall")
    if overall == DOWN:
        if marker is None:
            state.down_since["overall"] = now
        elif (now - marker) >= timedelta(seconds=HEALTH_DOWN_SUSTAIN_SEC):
            if state.down_since.get("overall_alerted") is None:
                specs.append(
                    AlertSpec(
                        kind="health_failure",
                        severity="critical",
                        title="CoreBot: /health/ready не готов > 5 минут",
                        details=(
                            f"overall=down с {marker.isoformat()} "
                            f"(SLO #3). Компоненты: {down_names(components)}."
                        ),
                        fingerprint="health:ready",
                    )
                )
                state.down_since["overall_alerted"] = now
    else:
        state.down_since.pop("overall", None)
        state.down_since.pop("overall_alerted", None)

    # --- queues ---------------------------------------------------------
    queues = comp("queues")
    if queues.get("state") == DEGRADED:
        detail = queues.get("detail") or {}
        crit = (detail.get("reason") == "backlog-critical")
        specs.append(
            AlertSpec(
                kind="outbound_backlog",
                severity="critical" if crit else "warning",
                title="CoreBot: очередь outbound растёт",
                details=(
                    f"outbound_pending={detail.get('outbound_pending')}, "
                    f"bot_commands_pending={detail.get('bot_commands_pending')} "
                    f"(warn {OUTBOUND_BACKLOG_WARN} / crit {OUTBOUND_BACKLOG_CRIT})."
                ),
                fingerprint="queue:outbound",
            )
        )

    # --- signals (inputs are the source of truth) -------------------------
    if (inputs.floodwait_1h or 0) >= FLOODWAIT_SPIKE_PER_HOUR:        specs.append(
            AlertSpec(
                kind="floodwait_spike",
                severity="warning",
                title="CoreBot: всплеск FloodWait/авторизационных ошибок",
                details=(
                    f"FloodWait/auth ошибок за час: {inputs.floodwait_1h} "
                    f"(SLO #7: >= 10/час). Процесс жив, readiness 200."
                ),
                fingerprint="signal:floodwait",
            )
        )
    delta = _busy_delta(state, dict(inputs.busy or {}))
    if int(delta.get("exhausted", 0)) > 0:
        specs.append(
            AlertSpec(
                kind="db_lock_repeated",
                severity="critical",
                title="CoreBot: повторяющиеся DB lock (retry исчерпан)",
                details=(
                    f"SQLITE_BUSY exhausted +{delta.get('exhausted')} "
                    f"с прошлой проверки (retries +{delta.get('retries')})."
                ),
                fingerprint="signal:db-lock",
            )
        )
    elif int(delta.get("retries", 0)) >= 20:
        specs.append(
            AlertSpec(
                kind="db_lock_repeated",
                severity="warning",
                title="CoreBot: transient DB busy (процесс жив)",
                details=(
                    f"SQLITE_BUSY retries +{delta.get('retries')} без exhausted — "
                    f"degraded, readiness 200."
                ),
                fingerprint="signal:db-lock",
            )
        )
    if (inputs.ingest401_10m or 0) >= INGEST_401_SPIKE_COUNT:
        specs.append(
            AlertSpec(
                kind="ingest_401_spike",
                severity="warning",
                title="CoreBot: неверный токен ingest (401)",
                details=(
                    f"HTTP 401 на /ingest за 10 мин: {inputs.ingest401_10m} "
                    f"(SLO #8: >= 3/10 мин)."
                ),
                fingerprint="signal:ingest-401",
            )
        )

    # --- storage (inputs are the source of truth; SLO #4/#5/#6) ---------
    storage_pressures: list[tuple[str, str]] = []
    if inputs.disk_used_pct is not None:
        if inputs.disk_used_pct > 90.0:
            storage_pressures.append(("disk-critical", "critical"))
        elif inputs.disk_used_pct > 80.0:
            storage_pressures.append(("disk-warning", "warning"))
    if inputs.db_bytes is not None:
        if inputs.db_bytes > 5 * 1024 * 1024 * 1024:
            storage_pressures.append(("db-size-critical", "critical"))
        elif inputs.db_bytes > 2 * 1024 * 1024 * 1024:
            storage_pressures.append(("db-size-warning", "warning"))
    if inputs.backup_age_h is not None and inputs.backup_age_h > 26.0:
        storage_pressures.append(("backup-stale", "warning"))
    for reason, severity in storage_pressures:
        specs.append(
            AlertSpec(
                kind="storage_pressure",
                severity=severity,
                title=f"CoreBot: давление хранилища ({reason})",
                details=(
                    f"disk={inputs.disk_used_pct}% "
                    f"(warn 80/crit 90), db_bytes={inputs.db_bytes} "
                    f"(warn 2G/crit 5G), backup_age_h={inputs.backup_age_h} "
                    f"(max 26h)."
                ),
                fingerprint=f"storage:{reason}",
            )
        )

    # --- release identity (task 06) -------------------------------------
    live = (live_sha or "").strip()
    deployed = (deployed_sha or "").strip()
    if live and deployed and live.lower()[:12] != deployed.lower()[:12]:
        specs.append(
            AlertSpec(
                kind="version_mismatch",
                severity="critical",
                title="CoreBot: version-MISMATCH (live vs deployed)",
                details=(
                    f"live /version sha={live[:12]} != deployed_sha={deployed[:12]}: "
                    f"возможно новое ядро + старый CP."
                ),
                fingerprint="release:version-mismatch",
            )
        )

    # --- recovery: down → ok transitions --------------------------------
    for name in SERVING_COMPONENTS:
        prev = state.components.get(name)
        cur = (components.get(name) or {}).get("state")
        if prev == DOWN and cur == OK:
            specs.append(
                AlertSpec(
                    kind=f"{name}.recovered",
                    severity="info",
                    title=f"CoreBot: {name} {RECOVERED_WORD}",
                    details=f"{name}: down → ok. Последний живой tick: {last_seen(name)}.",
                    fingerprint=f"{name}:recovered",
                    recovery=True,
                )
            )
    if state.components.get("__overall__") == DOWN and overall == OK:
        specs.append(
            AlertSpec(
                kind="instance.recovered",
                severity="info",
                title=f"CoreBot: инстанс {RECOVERED_WORD}",
                details="overall: down → ok.",
                fingerprint="instance:recovered",
                recovery=True,
            )
        )

    # --- bookkeeping -----------------------------------------------------
    for name in SERVING_COMPONENTS:
        cur = (components.get(name) or {}).get("state")
        if cur in (OK, DEGRADED, DOWN):
            state.components[name] = cur
    if overall in (OK, DEGRADED, DOWN):
        state.components["__overall__"] = overall
    state.busy_baseline = {k: int(inputs.busy.get(k, 0)) for k in ("attempts", "retries", "exhausted", "failed_fast")}

    return specs


def down_names(components: dict[str, Any]) -> str:
    down = sorted(n for n, c in components.items() if (c or {}).get("state") == DOWN)
    return ",".join(down) if down else "-"


async def dispatch(
    state: WatchdogState,
    specs: list[AlertSpec],
    *,
    db: Any,
    tenant_id: int,
    sender: Optional[Sender] = None,
    now: Optional[datetime] = None,
) -> dict[str, int]:
    """Journal every spec via ``upsert_alert``; send via Telegram on cooldown.

    Returns ``{"journaled": n, "sent": m}``. ``sender`` defaults to
    ``send_telegram_alert`` (real Telegram); tests inject a fake.
    """
    from control_plane.services.alerts import send_telegram_alert, upsert_alert

    moment = now or utcnow_naive()
    send = sender or send_telegram_alert
    journaled = 0
    sent = 0
    for spec in specs:
        try:
            upsert_alert(
                db,
                tenant_id=tenant_id,
                agent_id=None,
                kind=spec.kind,
                severity=spec.severity,
                title=spec.title,
                details=spec.details,
                fingerprint=spec.fingerprint,
            )
            journaled += 1
        except Exception:
            continue
        try:
            if spec.recovery or should_notify(state, spec.fingerprint, moment):
                await send(spec.title, spec.details)
                state.last_sent[spec.fingerprint] = moment
                sent += 1
        except Exception:
            continue
    return {"journaled": journaled, "sent": sent}


def note_update_result(
    *,
    exit_code: int,
    target_sha: str = "",
    backup_path: str = "",
) -> Optional[AlertSpec]:
    """Map ``scripts/update_corebot.sh`` exit codes to alert specs (task 06).

    * ``0`` — success / no-op / dry-run ⇒ no alert (``None``);
    * ``3`` — update failed but rolled back ⇒ warning, sent immediately;
    * ``4`` — rollback incomplete, manual recovery required ⇒ critical;
    * anything else (``2`` = pre-swap failure, nothing changed) ⇒ no alert.

    Delivery: the fleet wrapper (or the operator) journals + sends the
    returned spec right away, e.g. via
    ``python -m tools.instance_status --report-update-exit 3`` — the update
    script itself (frozen task-06 code) is never modified.
    """
    if exit_code == 0:
        return None
    if exit_code == 3:
        return AlertSpec(
            kind="update_failed",
            severity="warning",
            title="CoreBot: обновление не удалось, выполнен rollback (exit 3)",
            details=(
                f"target={target_sha[:12] if target_sha else 'unknown'} "
                f"backup={backup_path or 'unknown'}: предыдущий код и данные "
                f"восстановлены, health перепроверен."
            ),
            fingerprint=f"update:{target_sha[:12] if target_sha else 'unknown'}:exit3",
        )
    if exit_code == 4:
        return AlertSpec(
            kind="update_rollback_incomplete",
            severity="critical",
            title="CoreBot: rollback НЕ завершён (exit 4) — нужно ручное восстановление",
            details=(
                f"target={target_sha[:12] if target_sha else 'unknown'} "
                f"backup={backup_path or 'unknown'}. RUNBOOK раздел 17."
            ),
            fingerprint=f"update:{target_sha[:12] if target_sha else 'unknown'}:exit4",
        )
    return None


def default_tenant_id(db: Any) -> int:
    """Resolve the singleton ``default`` tenant id (best-effort, fallback 1)."""
    try:
        from control_plane.models import Tenant

        row = db.query(Tenant).filter(Tenant.name == "default").first()
        if row is not None:
            return int(row.id)
        first = db.query(Tenant).order_by(Tenant.id.asc()).first()
        if first is not None:
            return int(first.id)
    except Exception:
        pass
    return 1


__all__ = [
    "ALERT_COOLDOWN_SEC",
    "HEALTH_DOWN_SUSTAIN_SEC",
    "RECOVERED_WORD",
    "AlertSpec",
    "WatchdogState",
    "STATE",
    "cooldown_for",
    "should_notify",
    "evaluate",
    "down_names",
    "dispatch",
    "note_update_result",
    "default_tenant_id",
]
