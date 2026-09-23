"""Unified runtime snapshot of one CoreBot instance (task 08).

One snapshot answers two questions per component: **WHAT is broken** (state
``ok`` / ``degraded`` / ``down``) and **WHEN it was last alive**
(``last_seen`` naive-UTC ISO timestamp).

Component model
---------------

*Serving* components drive the overall state directly:

* ``bot`` — bot process heartbeat row in ``corebot.db`` (stale ``> 120 s``
  ⇒ ``down``; SLO alerts #1/#2 use the same 2-minute threshold);
* ``bot_db`` / ``control_plane_db`` — plain ``SELECT 1`` probes (mirror the
  ``/health/ready`` hard dependencies);
* ``background_tasks`` — supervised-task failures (mirrors ``/health/ready``;
  any failure ⇒ ``down``);
* ``consumer:outbound`` / ``consumer:bot_command`` — last successful loop
  tick from the shared heartbeat table (stale ``> 120 s`` ⇒ ``down``);
* ``parser`` — embedded parser task status **reused** from
  ``control_plane.health._parser_status`` (never duplicated here). A
  disabled optional parser (``PARSER_EMBEDDED=0``) is ``ok``/``disabled`` —
  never a false positive.

*Resource/signal* components (``queues``, ``storage``, ``signals``) cap at
``degraded`` for the overall state: they raise alerts but a full queue or a
growing database does not by itself mean the instance stopped serving.
Informational sections (``mailing``, ``worker_pool``, ``version``) never
affect the overall state.

``degraded`` means: the process is alive (fresh heartbeat, ``/health/ready``
would still return 200) but a transient external problem is visible —
FloodWait spike, transient SQLite-busy retries, backlog above the warning
level, worker pool offline. ``down`` means the ``/health/ready`` semantics:
a hard dependency is unavailable.

Overall ⇒ readiness mapping (prediction only — the ``/health*`` contracts
and JSON are **not** changed by this module):

* ``down`` ⇒ ``503``, ``ok``/``degraded`` ⇒ ``200``.

The builder is pure (all inputs are passed in, ``now`` is injected), so the
same code serves the in-CP watchdog (full coverage: live task handles,
busy-retry stats) and the fleet CLI (external coverage: DB/file probes
only). Payloads are sanitized before return and contain counts/ids/statuses
only — never message bodies or credentials.
"""

from __future__ import annotations

import os as _os
import re as _re
import shutil as _shutil
from dataclasses import dataclass as _dataclass
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path as _Path
from typing import Any, Optional

from control_plane.services import sanitize as _sanitize
from control_plane.services.heartbeat import (
    BOT_STALE_SEC,
    CONSUMER_STALE_SEC,
)

OK = "ok"
DEGRADED = "degraded"
DOWN = "down"
UNKNOWN = "unknown"

STATES = (OK, DEGRADED, DOWN)

# Queue backlog thresholds (pending rows). SLO is silent here; numbers are
# chosen so a normally-draining consumer (batch 10–20 per tick, tick every
# ~2 s) trips ``warn`` only after minutes of stall and ``crit`` after a
# sustained outage. Component state caps at ``degraded`` (see module docs).
OUTBOUND_BACKLOG_WARN = 50
OUTBOUND_BACKLOG_CRIT = 200
BOTCMD_BACKLOG_WARN = 50

# FloodWait / auth-error spike: SLO #7 — ``>= 10`` per 1 hour in logs.
FLOODWAIT_SPIKE_PER_HOUR = 10

# Ingest 401 spike: SLO #8 — ``>= 3`` per 10 minutes.
INGEST_401_SPIKE_COUNT = 3
INGEST_401_WINDOW_SEC = 600

# Storage: SLO #4 disk ``> 80 %`` warn / ``> 90 %`` critical;
# SLO #5 DB ``> 2 GB`` warn / ``> 5 GB`` critical;
# SLO #6 backup age ``> 26 h``.
DISK_WARN_PCT = 80.0
DISK_CRIT_PCT = 90.0
DB_SIZE_WARN_BYTES = 2 * 1024 * 1024 * 1024
DB_SIZE_CRIT_BYTES = 5 * 1024 * 1024 * 1024
BACKUP_MAX_AGE_HOURS = 26.0


@dataclass
class SnapshotInputs:
    """Everything :func:`build_snapshot` needs. IO lives in collectors."""

    now: datetime
    # Heartbeats: {component: {"updated_at": datetime|None, "detail": dict}}.
    beats: dict[str, dict[str, Any]] = field(default_factory=dict)
    # DB probes (None = could not probe).
    cp_db_ok: Optional[bool] = None
    bot_db_ok: Optional[bool] = None
    # Embedded parser: flag + in-process task status ("running"/"disabled"/
    # "failed"/"stopped") when a live handle is visible, else None (CLI).
    parser_embedded: bool = True
    parser_task_status: Optional[str] = None
    # parsing_tasks table counts.
    parsing_pending: int = 0
    parsing_running: int = 0
    parsing_failed_24h: int = 0
    # background_tasks.snapshot() when running inside CP, else None (CLI).
    background: Optional[dict[str, Any]] = None
    # Queue backlog (pending rows; counts only).
    outbound_pending: int = 0
    botcmd_pending: int = 0
    # Active mailing {id,status,sent,total} or None. Informational only.
    mailing: Optional[dict[str, Any]] = None
    # SQLITE_BUSY_RETRY_STATS absolute values (this process) + deltas since
    # the last watchdog evaluation (None outside CP).
    busy: dict[str, int] = field(default_factory=dict)
    busy_delta: Optional[dict[str, int]] = None
    # Log-derived signals (None = logs unavailable).
    floodwait_1h: Optional[int] = None
    ingest401_10m: Optional[int] = None
    # Storage signals (None = unavailable).
    disk_used_pct: Optional[float] = None
    db_bytes: Optional[int] = None
    backup_age_h: Optional[float] = None
    # Release identity + deploy record.
    release: dict[str, Any] = field(default_factory=dict)
    deployed_sha: str = ""


def _age_sec(now: datetime, ts: Optional[datetime]) -> Optional[float]:
    if ts is None:
        return None
    try:
        return (now - ts.replace(tzinfo=None)).total_seconds()
    except Exception:
        return None


def _fresh(now: datetime, ts: Optional[datetime], stale_sec: float) -> bool:
    age = _age_sec(now, ts)
    return age is not None and age <= stale_sec


def _iso(ts: Optional[datetime]) -> Optional[str]:
    if ts is None:
        return None
    try:
        return ts.replace(tzinfo=None).isoformat()
    except Exception:
        return None


def _component(
    state: str, last_seen: Optional[datetime], detail: Any = None
) -> dict[str, Any]:
    return {"state": state, "last_seen": _iso(last_seen), "detail": detail or {}}


#: Allowlisted heartbeat-detail keys. The heartbeat row is written by another
#: process, so at this trust boundary only known diagnostic keys flow into
#: the snapshot — arbitrary free text (potentially a message body or a
#: credential pasted into ``detail``) is dropped entirely, not just masked.
BOT_DETAIL_ALLOWLIST = frozenset(
    {
        "pid",
        "kind",
        "version",
        "sha",
        "workers_total",
        "workers_connected",
        "mailing_running",
    }
)

CONSUMER_DETAIL_ALLOWLIST = frozenset({"kind", "last_tick"})


def _clean_detail(detail: Any, allowed: frozenset) -> dict[str, Any]:
    if not isinstance(detail, dict):
        return {}
    return {k: v for k, v in detail.items() if k in allowed}


def classify_bot(inputs: SnapshotInputs) -> dict[str, Any]:
    """Bot process: fresh heartbeat ⇒ alive; stale/missing ⇒ down."""
    row = inputs.beats.get("bot", {})
    seen = row.get("updated_at")
    if not _fresh(inputs.now, seen, BOT_STALE_SEC):
        return _component(
            DOWN, seen, {"reason": "heartbeat-stale", "stale_sec": BOT_STALE_SEC}
        )
    detail = _clean_detail(row.get("detail"), BOT_DETAIL_ALLOWLIST)
    total = detail.get("workers_total")
    connected = detail.get("workers_connected")
    if (
        isinstance(total, int)
        and isinstance(connected, int)
        and total > 0
        and connected == 0
    ):
        return _component(DEGRADED, seen, {"reason": "worker-pool-offline", **detail})
    return _component(OK, seen, detail)


def classify_consumer(inputs: SnapshotInputs, component: str) -> dict[str, Any]:
    """Consumer loop: last successful tick fresh ⇒ alive, else down.

    A missing row while the bot itself is fresh is ``degraded`` (startup
    race: the bot beat exists but the consumer has not persisted its first
    tick yet) rather than a false ``down``; a missing row with a stale bot
    is ``down`` together with everything else.
    """
    row = inputs.beats.get(component)
    seen = (row or {}).get("updated_at")
    if row is None or seen is None:
        bot_seen = inputs.beats.get("bot", {}).get("updated_at")
        if _fresh(inputs.now, bot_seen, BOT_STALE_SEC):
            return _component(
                DEGRADED,
                None,
                {"reason": "no-tick-yet", "stale_sec": CONSUMER_STALE_SEC},
            )
        return _component(
            DOWN, None, {"reason": "no-tick", "stale_sec": CONSUMER_STALE_SEC}
        )
    if not _fresh(inputs.now, seen, CONSUMER_STALE_SEC):
        return _component(
            DOWN, seen, {"reason": "tick-stale", "stale_sec": CONSUMER_STALE_SEC}
        )
    return _component(
        OK, seen, _clean_detail((row or {}).get("detail"), CONSUMER_DETAIL_ALLOWLIST)
    )


def classify_parser(inputs: SnapshotInputs) -> dict[str, Any]:
    """Embedded parser status; a disabled optional parser is ``ok``.

    The in-process task verdict is reused from
    ``control_plane.health._parser_status`` (imported lazily so this module
    stays importable without FastAPI side effects at collection time).
    """
    if not inputs.parser_embedded:
        return _component(OK, None, {"mode": "disabled"})
    status = inputs.parser_task_status
    if status in ("failed", "stopped"):
        return _component(
            DOWN,
            inputs.now,
            {"reason": "parser-task-crash", "task_status": status},
        )
    if status == "running":
        return _component(OK, inputs.now, {"task_status": status})
    # External view (CLI): fall back to parsing_tasks table signals.
    if inputs.parsing_failed_24h > 0:
        return _component(
            DEGRADED,
            None,
            {"reason": "recent-failures-24h", "failed_24h": inputs.parsing_failed_24h},
        )
    return _component(
        OK,
        None,
        {
            "pending": inputs.parsing_pending,
            "running": inputs.parsing_running,
            "failed_24h": inputs.parsing_failed_24h,
        },
    )


def classify_background(inputs: SnapshotInputs) -> dict[str, Any]:
    """Supervised background tasks (mirrors /health/ready hard dep)."""
    snap = inputs.background
    if snap is None:
        return _component(UNKNOWN, None, {"reason": "external-view"})
    if int(snap.get("failed", 0)) > 0:
        return _component(
            DOWN,
            inputs.now,
            {"reason": "task-failed", "failures": snap.get("failures", [])},
        )
    return _component(
        OK,
        inputs.now,
        {"active": snap.get("active", 0), "active_names": snap.get("active_names", [])},
    )


def classify_queues(inputs: SnapshotInputs) -> dict[str, Any]:
    """Queue backlog. Caps at ``degraded`` (never flips overall to down)."""
    detail = {
        "outbound_pending": int(inputs.outbound_pending),
        "bot_commands_pending": int(inputs.botcmd_pending),
        "parsing_pending": int(inputs.parsing_pending),
        "warn_at": OUTBOUND_BACKLOG_WARN,
        "crit_at": OUTBOUND_BACKLOG_CRIT,
    }
    worst = max(int(inputs.outbound_pending), int(inputs.botcmd_pending))
    if (
        worst >= OUTBOUND_BACKLOG_CRIT
        or int(inputs.botcmd_pending) >= BOTCMD_BACKLOG_WARN * 4
    ):
        return _component(
            DEGRADED, inputs.now, {**detail, "reason": "backlog-critical"}
        )
    if (
        worst >= OUTBOUND_BACKLOG_WARN
        or int(inputs.botcmd_pending) >= BOTCMD_BACKLOG_WARN
    ):
        return _component(DEGRADED, inputs.now, {**detail, "reason": "backlog-warning"})
    return _component(OK, inputs.now, detail)


def classify_storage(inputs: SnapshotInputs) -> dict[str, Any]:
    """Disk / DB size / backup age. Caps at ``degraded`` (alerts carry severity)."""
    detail: dict[str, Any] = {
        "disk_used_pct": inputs.disk_used_pct,
        "db_bytes": inputs.db_bytes,
        "backup_age_h": inputs.backup_age_h,
    }
    reasons: list[str] = []
    if inputs.disk_used_pct is not None and inputs.disk_used_pct > DISK_CRIT_PCT:
        reasons.append("disk-critical")
    elif inputs.disk_used_pct is not None and inputs.disk_used_pct > DISK_WARN_PCT:
        reasons.append("disk-warning")
    if inputs.db_bytes is not None and inputs.db_bytes > DB_SIZE_CRIT_BYTES:
        reasons.append("db-size-critical")
    elif inputs.db_bytes is not None and inputs.db_bytes > DB_SIZE_WARN_BYTES:
        reasons.append("db-size-warning")
    if inputs.backup_age_h is not None and inputs.backup_age_h > BACKUP_MAX_AGE_HOURS:
        reasons.append("backup-stale")
    if reasons:
        return _component(DEGRADED, inputs.now, {**detail, "reasons": reasons})
    return _component(OK, inputs.now, detail)


def classify_signals(inputs: SnapshotInputs) -> dict[str, Any]:
    """Transient external-failure signals → ``degraded`` (process alive)."""
    detail: dict[str, Any] = {
        "floodwait_1h": inputs.floodwait_1h,
        "ingest401_10m": inputs.ingest401_10m,
        "busy": dict(inputs.busy or {}),
        "busy_delta": dict(inputs.busy_delta or {}),
    }
    reasons: list[str] = []
    if (
        inputs.floodwait_1h is not None
        and inputs.floodwait_1h >= FLOODWAIT_SPIKE_PER_HOUR
    ):
        reasons.append("floodwait-spike")
    if (
        inputs.ingest401_10m is not None
        and inputs.ingest401_10m >= INGEST_401_SPIKE_COUNT
    ):
        reasons.append("ingest-401-spike")
    delta = inputs.busy_delta or {}
    if int(delta.get("exhausted", 0)) > 0:
        reasons.append("db-lock-exhausted")
    elif int(delta.get("retries", 0)) >= 20:
        reasons.append("db-busy-transient")
    if reasons:
        return _component(DEGRADED, inputs.now, {**detail, "reasons": reasons})
    return _component(OK, inputs.now, detail)


# Components whose ``down`` flips the overall instance state (the serving
# path). Resource/signal sections alert but never report instance ``down``.
SERVING_COMPONENTS = (
    "bot",
    "bot_db",
    "control_plane_db",
    "background_tasks",
    "consumer:outbound",
    "consumer:bot_command",
    "parser",
)


def _worst_overall(components: dict[str, dict[str, Any]]) -> str:
    overall = OK
    for name in SERVING_COMPONENTS:
        state = (components.get(name) or {}).get("state")
        if state == DOWN:
            return DOWN
        if state == DEGRADED:
            overall = DEGRADED
    for name, comp in components.items():
        if name in SERVING_COMPONENTS:
            continue
        if name in ("mailing", "worker_pool", "version"):
            continue
        if (comp or {}).get("state") == DEGRADED:
            overall = DEGRADED
    return overall


def build_snapshot(inputs: SnapshotInputs) -> dict[str, Any]:
    """Build, sanitize and return the JSON-serializable instance snapshot."""
    components: dict[str, Any] = {
        "bot": classify_bot(inputs),
        "bot_db": _component(
            OK if inputs.bot_db_ok else DOWN,
            inputs.now if inputs.bot_db_ok else None,
            {} if inputs.bot_db_ok is not None else {"reason": "probe-unavailable"},
        )
        if inputs.bot_db_ok is not None
        else _component(UNKNOWN, None, {"reason": "probe-unavailable"}),
        "control_plane_db": _component(
            OK if inputs.cp_db_ok else DOWN,
            inputs.now if inputs.cp_db_ok else None,
            {} if inputs.cp_db_ok is not None else {"reason": "probe-unavailable"},
        )
        if inputs.cp_db_ok is not None
        else _component(UNKNOWN, None, {"reason": "probe-unavailable"}),
        "parser": classify_parser(inputs),
        "background_tasks": classify_background(inputs),
        "consumer:outbound": classify_consumer(inputs, "consumer:outbound"),
        "consumer:bot_command": classify_consumer(inputs, "consumer:bot_command"),
        "queues": classify_queues(inputs),
        "storage": classify_storage(inputs),
        "signals": classify_signals(inputs),
        "mailing": _component(
            OK,
            inputs.now if inputs.mailing else None,
            inputs.mailing or {"active": False},
        ),
        "worker_pool": _component(
            OK if inputs.beats.get("bot", {}).get("updated_at") else UNKNOWN,
            inputs.beats.get("bot", {}).get("updated_at"),
            {
                "total": (inputs.beats.get("bot", {}).get("detail") or {}).get(
                    "workers_total"
                ),
                "connected": (inputs.beats.get("bot", {}).get("detail") or {}).get(
                    "workers_connected"
                ),
            },
        ),
    }
    release = dict(inputs.release or {})
    live_sha = str(release.get("sha", "unknown") or "unknown")
    deployed = (inputs.deployed_sha or "").strip()
    match: Optional[bool] = None
    if deployed and live_sha and live_sha != "unknown":
        match = live_sha[:12].lower() == deployed[:12].lower()
    components["version"] = _component(
        OK,
        inputs.now,
        {**release, "deployed_sha": deployed or "unknown", "match": match},
    )

    overall = _worst_overall(components)
    beats_seen = [
        v.get("updated_at") for v in inputs.beats.values() if v.get("updated_at")
    ]
    last_tick = max(beats_seen) if beats_seen else None
    snapshot = {
        "checked_at": inputs.now.replace(tzinfo=None).isoformat(),
        "overall": overall,
        "readiness": {"ok": overall != DOWN, "http": 503 if overall == DOWN else 200},
        "components": components,
        "last_tick": _iso(last_tick),
    }
    return _sanitize.sanitize(snapshot)


def parser_task_status(task: Any) -> Optional[str]:
    """Reuse ``control_plane.health._parser_status`` (no logic duplication)."""
    from control_plane.health import _parser_status

    status, _ok = _parser_status(task)
    return status


# ---------------------------------------------------------------------------
# Live collectors (IO). Everything is best-effort: a failed probe yields
# None/zero, never an exception. Used by the fleet CLI (external coverage)
# and by the in-CP watchdog (full coverage).
# ---------------------------------------------------------------------------

_LOG_TS_RE = _re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")

FLOODWAIT_MARKERS = (
    "FloodWait",
    "FLOOD_WAIT",
    "PeerFlood",
    "PEER_FLOOD",
    "FloodWaitError",
    "SESSION_REVOKED",
    "AuthKeyUnregistered",
    "Unauthorized",
)

INGEST_401_MARKERS = ("/ingest", " 401")

LOG_TAIL_LINES = 5000


def _parse_log_ts(line: str) -> Optional[datetime]:
    match = _LOG_TS_RE.match(line.strip())
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def count_log_events(
    lines: list[str], markers: tuple[str, ...], since: datetime
) -> int:
    """Count recent lines containing any marker (all markers for 401-tuples).

    Only lines with a parseable timestamp ``>= since`` count; unparseable
    lines are ignored so ancient log garbage cannot inflate the signal.
    For the ingest-401 tuple both markers must be present in the same line
    (uvicorn access line: ``"POST /ingest/batch ..." 401``).
    """
    total = 0
    for line in lines:
        ts = _parse_log_ts(line)
        if ts is None or ts < since:
            continue
        if all(m in line for m in markers):
            total += 1
    return total


def read_log_tail(path: _Path, max_lines: int = LOG_TAIL_LINES) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            data = fh.read().splitlines()
    except OSError:
        return []
    return data[-max_lines:] if len(data) > max_lines else data


def is_parser_embedded_enabled() -> bool:
    """Same semantics as ``control_plane.main.is_parser_embedded_enabled``.

    Read here (instead of importing ``control_plane.main``) so the fleet CLI
    does not construct the FastAPI app: ``PARSER_EMBEDDED=0/false/off/no``
    disables the embedded loop.
    """
    return _os.getenv("PARSER_EMBEDDED", "1").strip().lower() not in {
        "0",
        "false",
        "off",
        "no",
    }


@_dataclass
class CollectOptions:
    app_dir: _Path
    in_process: bool = False  # True inside CP: live handles + busy stats
    parser_task: Any = None  # live asyncio handle (CP only)
    backup_dirs: Optional[list[_Path]] = None
    log_files: Optional[list[_Path]] = None
    deployed_sha_file: Optional[_Path] = None


def _newest_backup_age_h(dirs: list[_Path], now: datetime) -> Optional[float]:
    newest: Optional[float] = None
    for directory in dirs:
        try:
            if not directory.is_dir():
                continue
            for path in directory.iterdir():
                if not path.is_file():
                    continue
                name = path.name.lower()
                if not (
                    name.endswith(".tar.gz")
                    or name.endswith(".db")
                    or name.endswith(".tgz")
                ):
                    continue
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                newest = mtime if newest is None else max(newest, mtime)
        except OSError:
            continue
    if newest is None:
        return None
    try:
        return max(0.0, (now.timestamp() - newest) / 3600.0)
    except Exception:
        return None


def _resolve_corebot_db_path(app_dir: _Path) -> _Path:
    url = (_os.getenv("BOT_DATABASE_URL", "") or "").strip()
    if url.startswith("sqlite:////"):
        return _Path(url[len("sqlite:///") :])
    if url.startswith("sqlite:///"):
        return app_dir / url[len("sqlite:///") :]
    return app_dir / "data" / "corebot.db"


def collect_live_inputs(opts: CollectOptions) -> SnapshotInputs:
    """Probe DBs, files and (in-CP) live handles into :class:`SnapshotInputs`."""
    from utils.time import utcnow_naive

    now = utcnow_naive()
    app_dir = _Path(opts.app_dir)

    release: dict[str, Any] = {}
    try:
        from control_plane.version import get_release_info

        release = get_release_info()
    except Exception:
        pass
    deployed_sha = ""
    sha_file = opts.deployed_sha_file or (app_dir / ".deployed_sha")
    try:
        if sha_file.is_file():
            deployed_sha = sha_file.read_text(encoding="utf-8").strip().split()[0]
    except (OSError, IndexError):
        deployed_sha = ""

    beats: dict[str, dict[str, Any]] = {}
    cp_db_ok: Optional[bool] = None
    bot_db_ok: Optional[bool] = None
    outbound_pending = 0
    botcmd_pending = 0
    parsing_pending = 0
    parsing_running = 0
    parsing_failed_24h = 0
    mailing: Optional[dict[str, Any]] = None

    try:
        from sqlalchemy import text as _text

        from control_plane.business.db import BotSession
        from control_plane.services.heartbeat import read_beats_session

        session = BotSession()
        try:
            try:
                session.execute(_text("SELECT 1"))
                bot_db_ok = True
            except Exception:
                bot_db_ok = False
            beats = read_beats_session(session)
            try:
                outbound_pending = int(
                    session.execute(
                        _text(
                            "SELECT COUNT(*) FROM outbound_queue WHERE status='pending'"
                        )
                    ).scalar()
                    or 0
                )
            except Exception:
                pass
            try:
                botcmd_pending = int(
                    session.execute(
                        _text(
                            "SELECT COUNT(*) FROM bot_commands WHERE status='pending'"
                        )
                    ).scalar()
                    or 0
                )
            except Exception:
                pass
            try:
                rows = session.execute(
                    _text("SELECT status, COUNT(*) FROM parsing_tasks GROUP BY status")
                ).all()
                for status, count in rows:
                    if status == "pending":
                        parsing_pending = int(count or 0)
                    elif status == "running":
                        parsing_running = int(count or 0)
            except Exception:
                pass
            try:
                cutoff = now - timedelta(hours=24)
                rows = session.execute(
                    _text(
                        "SELECT finished_at FROM parsing_tasks WHERE status='failed' "
                        "ORDER BY id DESC LIMIT 50"
                    )
                ).all()
                failed = 0
                for (finished_at,) in rows:
                    ts = finished_at
                    if isinstance(ts, str):
                        try:
                            ts = datetime.fromisoformat(ts)
                        except ValueError:
                            continue
                    if isinstance(ts, datetime) and ts.replace(tzinfo=None) >= cutoff:
                        failed += 1
                parsing_failed_24h = failed
            except Exception:
                pass
            try:
                # Counts/ids only — message_text is NEVER selected (PII rule).
                row = session.execute(
                    _text(
                        "SELECT id, status, messages_sent, total_messages FROM mailings "
                        "WHERE status='running' ORDER BY id DESC LIMIT 1"
                    )
                ).first()
                if row is not None:
                    mailing = {
                        "id": int(row[0]),
                        "status": str(row[1]),
                        "sent": int(row[2] or 0),
                        "total": int(row[3] or 0),
                    }
            except Exception:
                pass
        finally:
            session.close()
    except Exception:
        pass

    try:
        from sqlalchemy import text as _text

        from control_plane.database import SessionLocal

        session = SessionLocal()
        try:
            session.execute(_text("SELECT 1"))
            cp_db_ok = True
        except Exception:
            cp_db_ok = False
        finally:
            session.close()
    except Exception:
        pass

    background: Optional[dict[str, Any]] = None
    busy: dict[str, int] = {}
    if opts.in_process:
        try:
            from utils.background_tasks import background_tasks

            background = background_tasks.snapshot()
        except Exception:
            background = None
        try:
            from database.sqlite_pragmas import SQLITE_BUSY_RETRY_STATS

            busy = {k: int(v) for k, v in SQLITE_BUSY_RETRY_STATS.items()}
        except Exception:
            busy = {}

    parser_task_status_value: Optional[str] = None
    if opts.in_process and opts.parser_task is not None:
        try:
            parser_task_status_value = parser_task_status(opts.parser_task)
        except Exception:
            parser_task_status_value = None

    floodwait_1h: Optional[int] = None
    ingest401_10m: Optional[int] = None
    log_files = opts.log_files
    if log_files is None:
        log_files = [app_dir / "logs" / "error.log", app_dir / "logs" / "corebot.log"]
    try:
        lines: list[str] = []
        for path in log_files:
            lines.extend(read_log_tail(_Path(path)))
        if lines:
            floodwait_1h = count_log_events(
                lines, FLOODWAIT_MARKERS, now - timedelta(hours=1)
            )
            ingest401_10m = count_log_events(
                lines, INGEST_401_MARKERS, now - timedelta(seconds=600)
            )
    except Exception:
        pass

    disk_used_pct: Optional[float] = None
    db_bytes: Optional[int] = None
    db_path = _resolve_corebot_db_path(app_dir)
    try:
        if db_path.is_file():
            db_bytes = int(db_path.stat().st_size)
    except OSError:
        pass
    try:
        anchor = db_path.parent if db_path.parent.exists() else app_dir
        usage = _shutil.disk_usage(str(anchor))
        if usage.total:
            disk_used_pct = round(100.0 * usage.used / usage.total, 1)
    except OSError:
        pass

    backup_dirs = opts.backup_dirs
    if backup_dirs is None:
        backup_dirs = [
            _Path("/opt/corebot/backups"),
            app_dir / "data" / "control_plane_backups",
        ]
    backup_age_h = _newest_backup_age_h(backup_dirs, now)

    return SnapshotInputs(
        now=now,
        beats=beats,
        cp_db_ok=cp_db_ok,
        bot_db_ok=bot_db_ok,
        parser_embedded=is_parser_embedded_enabled(),
        parser_task_status=parser_task_status_value,
        parsing_pending=parsing_pending,
        parsing_running=parsing_running,
        parsing_failed_24h=parsing_failed_24h,
        background=background,
        outbound_pending=outbound_pending,
        botcmd_pending=botcmd_pending,
        mailing=mailing,
        busy=busy,
        busy_delta=None,
        floodwait_1h=floodwait_1h,
        ingest401_10m=ingest401_10m,
        disk_used_pct=disk_used_pct,
        db_bytes=db_bytes,
        backup_age_h=backup_age_h,
        release=release,
        deployed_sha=deployed_sha,
    )


__all__ = [
    "OK",
    "DEGRADED",
    "DOWN",
    "UNKNOWN",
    "STATES",
    "SERVING_COMPONENTS",
    "OUTBOUND_BACKLOG_WARN",
    "OUTBOUND_BACKLOG_CRIT",
    "BOTCMD_BACKLOG_WARN",
    "FLOODWAIT_SPIKE_PER_HOUR",
    "INGEST_401_SPIKE_COUNT",
    "INGEST_401_WINDOW_SEC",
    "DISK_WARN_PCT",
    "DISK_CRIT_PCT",
    "DB_SIZE_WARN_BYTES",
    "DB_SIZE_CRIT_BYTES",
    "BACKUP_MAX_AGE_HOURS",
    "SnapshotInputs",
    "build_snapshot",
    "classify_bot",
    "classify_consumer",
    "classify_parser",
    "classify_background",
    "classify_queues",
    "classify_storage",
    "classify_signals",
    "parser_task_status",
    "CollectOptions",
    "collect_live_inputs",
    "count_log_events",
    "read_log_tail",
    "is_parser_embedded_enabled",
    "FLOODWAIT_MARKERS",
    "INGEST_401_MARKERS",
    "LOG_TAIL_LINES",
]
