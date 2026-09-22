"""Bot → Control Plane heartbeat over the shared ``corebot.db`` (task 08).

Why ``corebot.db`` (and not a new port, HTTP endpoint or the CP telemetry
models):

* both processes already open this file — the bot via its async engine
  (``database/repository.py``) and the Control Plane via the sync
  ``BotSession`` (``control_plane/business/db.py``) — so no new listener is
  required (``INSTANCE_CONTRACT`` forbids binding anything except
  ``127.0.0.1:8081``) and no new network path is introduced;
* the CP telemetry models (``control_plane.db`` + HTTP ingest) require
  ``CP_AGENT_ENABLED=1`` and a token; the heartbeat must work even when the
  agent is disabled, and the bot must not write into the CP auth database
  directly;
* a single ``COUNT(*)``-friendly table with ``CREATE TABLE IF NOT EXISTS``
  needs no migration of existing tables and therefore does not touch the
  contracts of tasks 02/04–07 (their code is only *called* from here).

Timing numbers (all documented, all derived from ``docs/operations/SLO.md``):

* :data:`HEARTBEAT_INTERVAL_SEC` = 30 s — the bot process rewrites its row
  at most every 30 s, i.e. well within the required heartbeat interval
  of ``<= 60 s``;
* :data:`BOT_STALE_SEC` = :data:`CONSUMER_STALE_SEC` = 120 s — a heartbeat
  older than 2 minutes means *down*, matching SLO alerts #1/#2
  (``service not active > 2 minutes``);
* consumer-death detection budget: watchdog poll every 60 s + 120 s stale
  threshold ⇒ a dead consumer is flagged within ``<= 180 s``, inside the
  SLO #3 readiness window (``> 5 minutes``);
* :data:`WRITE_THROTTLE_SEC` = 30 s — consumers prove aliveness on every
  loop tick (idle sleep is 1.5–2 s) but persist at most twice a minute, so
  the extra write load on the shared SQLite WAL is negligible.

Timestamps are naive UTC ISO strings (task 02 rule: naive UTC in the DB).
All writers are best-effort and never raise into the caller loop.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Optional

from utils.time import utcnow_naive

HEARTBEAT_TABLE = "runtime_heartbeat"

BOT_COMPONENT = "bot"
OUTBOUND_COMPONENT = "consumer:outbound"
BOTCMD_COMPONENT = "consumer:bot_command"

HEARTBEAT_INTERVAL_SEC = 30
BOT_STALE_SEC = 120
CONSUMER_STALE_SEC = 120
WRITE_THROTTLE_SEC = 30

DDL = (
    f"CREATE TABLE IF NOT EXISTS {HEARTBEAT_TABLE} ("
    "component TEXT PRIMARY KEY, "
    "updated_at TEXT NOT NULL, "
    "detail TEXT NOT NULL DEFAULT '{}'"
    ")"
)

UPSERT_SQL = (
    f"INSERT INTO {HEARTBEAT_TABLE} (component, updated_at, detail) "
    "VALUES (:component, :updated_at, :detail) "
    "ON CONFLICT(component) DO UPDATE SET "
    "updated_at=excluded.updated_at, detail=excluded.detail"
)

SELECT_SQL = f"SELECT component, updated_at, detail FROM {HEARTBEAT_TABLE}"

_last_write: dict[str, datetime] = {}


def parse_ts(raw: Any) -> Optional[datetime]:
    """Parse a naive-UTC ISO timestamp from the heartbeat table."""
    if isinstance(raw, datetime):
        return raw.replace(tzinfo=None)
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return datetime.fromisoformat(raw.strip()).replace(tzinfo=None)
    except ValueError:
        return None


def ensure_table_dbapi(conn: Any) -> None:
    """Create the heartbeat table on a DBAPI connection (sqlite3)."""
    conn.execute(DDL)
    try:
        conn.commit()
    except Exception:
        pass


def upsert_beat_dbapi(
    conn: Any,
    component: str,
    updated_at: datetime,
    detail: str = "{}",
) -> None:
    """Write one heartbeat row on a DBAPI connection (sqlite3)."""
    ensure_table_dbapi(conn)
    conn.execute(
        f"INSERT INTO {HEARTBEAT_TABLE} (component, updated_at, detail) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT(component) DO UPDATE SET "
        "updated_at=excluded.updated_at, detail=excluded.detail",
        (component, updated_at.isoformat(), detail),
    )
    try:
        conn.commit()
    except Exception:
        pass


def read_beats_dbapi(conn: Any) -> dict[str, dict[str, Any]]:
    """Read all heartbeat rows from a DBAPI connection.

    Returns ``{component: {"updated_at": datetime|None, "detail": dict}}``.
    Missing table ⇒ empty dict (fresh install, no false positive).
    """
    import json

    try:
        cur = conn.execute(SELECT_SQL)
        rows = cur.fetchall()
    except Exception:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for component, updated_at, detail in rows:
        try:
            parsed_detail = json.loads(detail) if detail else {}
        except (ValueError, TypeError):
            parsed_detail = {}
        out[str(component)] = {
            "updated_at": parse_ts(updated_at),
            "detail": parsed_detail if isinstance(parsed_detail, dict) else {},
        }
    return out


def read_beats_session(session: Any) -> dict[str, dict[str, Any]]:
    """Read all heartbeat rows through a sync SQLAlchemy session (CP side)."""
    import json

    from sqlalchemy import text

    try:
        rows = session.execute(text(SELECT_SQL)).all()
    except Exception:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for component, updated_at, detail in rows:
        try:
            parsed_detail = json.loads(detail) if detail else {}
        except (ValueError, TypeError):
            parsed_detail = {}
        out[str(component)] = {
            "updated_at": parse_ts(updated_at),
            "detail": parsed_detail if isinstance(parsed_detail, dict) else {},
        }
    return out


def _throttled(component: str, now: datetime) -> bool:
    last = _last_write.get(component)
    if last is not None and (now - last) < timedelta(seconds=WRITE_THROTTLE_SEC):
        return True
    _last_write[component] = now
    return False


def reset_throttle() -> None:
    """Forget per-component write throttle (tests only)."""
    _last_write.clear()


async def beat(
    component: str,
    detail: Optional[dict[str, Any]] = None,
    *,
    force: bool = False,
    now: Optional[datetime] = None,
) -> bool:
    """Persist one heartbeat row from the bot process. Never raises.

    Returns True when the row was written, False when throttled or when the
    database was unavailable (best-effort by design).
    """
    import json

    from sqlalchemy import text

    moment = now or utcnow_naive()
    if not force and _throttled(component, moment):
        return False
    try:
        from database.session import session_scope
        from database.sqlite_pragmas import commit_with_busy_retry

        from sqlalchemy import text

        payload = json.dumps(detail or {}, ensure_ascii=False, default=str)
        async with session_scope() as session:
            try:
                await session.execute(text(DDL))
                await session.commit()
            except Exception:
                await session.rollback()
            await session.execute(
                text(UPSERT_SQL),
                {
                    "component": component,
                    "updated_at": moment.isoformat(),
                    "detail": payload,
                },
            )
            await commit_with_busy_retry(session, op_name=f"heartbeat-{component}")
    except Exception:
        return False
    return True


async def record_consumer_tick(component: str) -> bool:
    """Prove a consumer loop is alive (called after every consumer tick)."""
    return await beat(
        component,
        {"last_tick": utcnow_naive().isoformat(), "kind": "consumer-tick"},
    )


def default_bot_detail() -> dict[str, Any]:
    """Build the bot heartbeat detail (pool + version + pid, no secrets).

    Worker-pool connectivity lives only in the bot process memory
    (``worker_manager``), so the bot reports it inside its own heartbeat row;
    the Control Plane and the CLI only *read* it from there.
    """
    detail: dict[str, Any] = {
        "pid": _current_pid(),
        "kind": "bot-heartbeat",
    }
    try:
        from workers.manager import worker_manager

        workers = getattr(worker_manager, "workers", {}) or {}
        detail["workers_total"] = len(workers)
        detail["workers_connected"] = sum(
            1 for w in workers.values() if getattr(w, "is_connected", False)
        )
        detail["mailing_running"] = bool(getattr(worker_manager, "is_running", False))
    except Exception:
        pass
    try:
        from control_plane.version import get_release_info

        info = get_release_info()
        detail["version"] = str(info.get("version", "unknown"))
        detail["sha"] = str(info.get("sha", "unknown"))
    except Exception:
        detail.setdefault("version", "unknown")
        detail.setdefault("sha", "unknown")
    return detail


def _current_pid() -> int:
    import os

    try:
        return os.getpid()
    except Exception:
        return -1


class BotHeartbeat:
    """Asyncio background task writing the ``bot`` heartbeat row.

    Started once from the bot entry point (``main.py``)::

        bot_heartbeat = BotHeartbeat()
        bot_heartbeat.start()   # after db.connect() + consumers
        ...
        await bot_heartbeat.stop()

    Best-effort: a failed write is logged and retried on the next interval,
    never propagated.
    """

    def __init__(
        self,
        *,
        interval_sec: float = HEARTBEAT_INTERVAL_SEC,
        detail_fn: Any = None,
    ) -> None:
        self._interval = max(1.0, float(interval_sec))
        self._detail_fn = detail_fn or default_bot_detail
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="bot-heartbeat")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                detail = self._detail_fn()
            except Exception:
                detail = {"kind": "bot-heartbeat"}
            await beat(BOT_COMPONENT, detail if isinstance(detail, dict) else {})
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass
