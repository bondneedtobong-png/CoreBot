"""Task 11: local load/acceptance scenario (all-mocks, hermetic).

Covers, without sending anything to real users and without touching
``data/*.db`` / ``.env`` (every database lives under ``tmp_path``):

(a) concurrent DB writes: outbound enqueue storm (N tasks x M rows),
    parser claim race (exactly one winner), counter UPSERT storm;
    p95 latency of a single-row write is measured per operation;
(b) SQLITE_BUSY retry metric over the run (retries/min, exhausted == 0);
(c) WAL size after the run;
(d) API load through a real TestClient lifespan (GET /health/ready p95,
    GET /version);
(e) queue backlog drain (enqueue K pending rows -> batch drain, timed);
(f) lifespan startup/shutdown wall time (SLO budgets);
(g) backup+restore wall time of a synthetic tmp instance (RTO budget).

Reproducible parameters are the ``LOAD_*`` constants below. The aggregated
metrics (counts/latencies only, no PII) are written as JSON to
``tmp_path`` (never committed) and a summary is printed. The test asserts
the ADR 0002 / SLO thresholds, so a regression fails loudly.

External boundaries are never constructed here (no messenger SDK clients,
no LLM provider calls, no real HTTP sessions); the guard test below fails
the suite if a real network client marker sneaks into this file.
"""

from __future__ import annotations

import asyncio
import json
import math
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path

# NOTE: markers are built by concatenation so the literal forbidden strings
# never appear contiguously in THIS file — otherwise the guard below would
# fail on its own definition (self-trigger).
FORBIDDEN_NETWORK_MARKERS = (
    "Telegram" + "Client",
    "tele" + "thon",
    "aio" + "gram",
    "open" + "router",
    "Aiohttp" + "Session",
    "Client" + "Session(",
    "url" + "open",
)


def test_load_file_uses_no_real_network_clients():
    src = Path(__file__).read_text(encoding="utf-8")
    lowered = src.lower()
    for marker in FORBIDDEN_NETWORK_MARKERS:
        assert marker.lower() not in lowered, f"network marker leaked: {marker}"


# --- reproducible parameters -------------------------------------------------

LOAD_DB_TASKS = 8  # concurrent enqueue workers
LOAD_OUTBOUND_PER_TASK = 10  # rows per worker -> 80 outbound rows total
LOAD_COUNTER_TASKS = 16  # concurrent UPSERT increments, same (client, key)
LOAD_CLAIM_TASKS = 8  # concurrent parser-claim racers, one pending task
LOAD_API_READY_CALLS = 20  # GET /health/ready samples
LOAD_API_VERSION_CALLS = 5  # GET /version samples
LOAD_QUEUE_ROWS = 100  # backlog rows to drain
LOAD_QUEUE_BATCH = 20  # drain batch size
LOAD_BACKUP_ROWS = 200  # synthetic rows per DB in the tmp instance

# Thresholds (ADR 0002 + SLO; mirrored in GO_LIVE_REPORT).
TH_P95_WRITE_MS = 500.0  # ADR 0002 #2
TH_BUSY_RETRIES_PER_MIN = 5.0  # ADR 0002 #3
TH_WAL_BYTES = 256 * 1024 * 1024  # ADR 0002 #1
TH_READY_P95_SEC = 2.0  # SLO: readiness 200 <= 2 s
TH_STARTUP_SEC = 180.0  # SLO: /health/ready 200 within <= 3 min
TH_SHUTDOWN_SEC = 300.0  # update stop budget <= 5 min (RELEASE_CONTRACT)
TH_RESTORE_TOTAL_SEC = 1800.0  # ADR 0002 #6: drill must stay << 30 min

ARTIFACT_NAME = "load-acceptance-metrics.json"
SYNTHETIC_ENV = (
    "API_ID=12345\n"
    "API_HASH=SYNTHETIC_HASH_FOR_TESTS_ONLY\n"
    "BOT_TOKEN=000000:SYNTHETIC_TEST_ONLY\n"
    "OWNER_ID=123456789\n"
)


def _p95(xs: list[float]) -> float:
    assert xs, "empty sample"
    ordered = sorted(xs)
    idx = max(0, min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1))
    return float(ordered[idx])


def test_load_acceptance_full(tmp_path, monkeypatch):
    """Single aggregated load run: phases (a)..(g), JSON artifact, prints."""
    import sqlite3

    from sqlalchemy import func, select, update
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from database.crm_repositories import ClientClassCounterRepository
    from database.models import (
        Account,
        Base,
        BotCommand,
        Client,
        ClientStatus,
        OutboundQueue,
        ParsingTask,
    )
    from database.repositories import OutboundQueueRepository
    from database.sqlite_pragmas import (
        SQLITE_BUSY_RETRY_STATS,
        register_async_sqlite_pragmas,
        reset_retry_stats,
        run_with_busy_retry,
    )
    from workers.parser.task_runner import claim_pending_task

    db_path = tmp_path / "load.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        connect_args={"timeout": 30, "check_same_thread": False},
    )
    register_async_sqlite_pragmas(engine)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def _scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with Session() as setup:
            setup.add(Account(phone="+10000000001", session_name="load_sess_1"))
            setup.add(Client(username="loadclient1", status=ClientStatus.NEW))
            setup.add(ParsingTask(kind="channels", status="pending"))
            setup.add(ParsingTask(kind="channels", status="pending"))
            await setup.commit()
            account_id = await setup.scalar(
                select(Account.id).where(Account.session_name == "load_sess_1")
            )
            client_row_id = await setup.scalar(
                select(Client.id).where(Client.username == "loadclient1")
            )
            claim_task_id = await setup.scalar(
                select(ParsingTask.id)
                .where(ParsingTask.status == "pending")
                .order_by(ParsingTask.id.asc())
                .limit(1)
            )
        return int(account_id), int(client_row_id), int(claim_task_id)

    account_id, client_row_id, claim_task_id = asyncio.run(_scenario())

    # --- (a) write storm + (b) busy metric -----------------------------------
    reset_retry_stats()
    wall_start = time.perf_counter()
    enqueue_lat: list[float] = []

    async def _enqueue_worker(worker_no: int):
        async with Session() as sess:
            for i in range(LOAD_OUTBOUND_PER_TASK):
                t0 = time.perf_counter()
                await OutboundQueueRepository.enqueue(
                    sess,
                    account_id=account_id,
                    peer_user_id=700000000 + worker_no * 1000 + i,
                    text=f"load-probe-w{worker_no}-{i}",
                    requested_by="load-test",
                )
                enqueue_lat.append(time.perf_counter() - t0)

    async def _counter_worker():
        async with Session() as sess:
            await ClientClassCounterRepository.increment(
                sess, client_row_id, "loadstorm", 1
            )

    async def _claim_worker():
        async def _op():
            async with Session() as sess:
                won = await claim_pending_task(sess, claim_task_id)
                await sess.commit()
                return won

        return await run_with_busy_retry(_op, op_name="parser-claim")

    async def _storm_with_claim_capture():
        nonlocal_claim = {}

        async def _wrapped_claim(idx: int):
            nonlocal_claim[idx] = await _claim_worker()

        await asyncio.gather(
            *(_enqueue_worker(w) for w in range(LOAD_DB_TASKS)),
            *(_counter_worker() for _ in range(LOAD_COUNTER_TASKS)),
            *(_wrapped_claim(i) for i in range(LOAD_CLAIM_TASKS)),
        )
        return [nonlocal_claim[i] for i in range(LOAD_CLAIM_TASKS)]

    claim_results = asyncio.run(_storm_with_claim_capture())
    db_wall_sec = time.perf_counter() - wall_start
    busy = dict(SQLITE_BUSY_RETRY_STATS)
    retries_per_min = (
        float(busy["retries"]) / (db_wall_sec / 60.0) if db_wall_sec > 0 else 0.0
    )

    async def _verify():
        async with Session() as verify:
            n_outbound = await verify.scalar(
                select(func.count(OutboundQueue.id)).where(
                    OutboundQueue.requested_by == "load-test"
                )
            )
            counts = await ClientClassCounterRepository.get_counts(
                verify, client_row_id
            )
            winners = sum(1 for won in claim_results if won)
        return int(n_outbound), counts, winners

    n_outbound, counter_counts, claim_winners = asyncio.run(_verify())
    assert n_outbound == LOAD_DB_TASKS * LOAD_OUTBOUND_PER_TASK
    assert counter_counts == {"loadstorm": LOAD_COUNTER_TASKS}
    assert claim_winners == 1, f"claim race must have 1 winner, got {claim_winners}"

    p95_write_ms = _p95(enqueue_lat) * 1000.0

    # --- (c) WAL size (while engine still open) -------------------------------
    wal_bytes = 0
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db_path) + suffix)
        if sidecar.exists():
            wal_bytes += sidecar.stat().st_size
    db_file_bytes = db_path.stat().st_size

    # --- (e) queue backlog drain (same tmp DB, sequential consumer loop) ------
    drain_start = time.perf_counter()

    async def _drain():
        async with Session() as sess:
            for i in range(LOAD_QUEUE_ROWS):
                sess.add(
                    BotCommand(
                        command="load.probe",
                        args_json=json.dumps({"seq": i}),
                        status="pending",
                        requested_by="load-test",
                    )
                )
            await sess.commit()
        drained = 0
        while True:
            async with Session() as sess:
                rows = (
                    (
                        await sess.execute(
                            select(BotCommand)
                            .where(
                                BotCommand.status == "pending",
                                BotCommand.requested_by == "load-test",
                            )
                            .order_by(BotCommand.id.asc())
                            .limit(LOAD_QUEUE_BATCH)
                        )
                    )
                    .scalars()
                    .all()
                )
                if not rows:
                    break
                ids = [int(r.id) for r in rows]
                await sess.execute(
                    update(BotCommand)
                    .where(BotCommand.id.in_(ids))
                    .values(status="done")
                )
                await sess.commit()
                drained += len(ids)
        async with Session() as sess:
            left = await sess.scalar(
                select(func.count(BotCommand.id)).where(
                    BotCommand.status == "pending",
                    BotCommand.requested_by == "load-test",
                )
            )
        return drained, int(left)

    drained, left_pending = asyncio.run(_drain())
    drain_sec = time.perf_counter() - drain_start
    assert drained == LOAD_QUEUE_ROWS
    assert left_pending == 0

    # --- (d)+(f) API load + lifespan timing on the REAL lifespan --------------
    sys.modules.pop("control_plane.main", None)
    import control_plane.main as main
    from control_plane import health as health_mod
    from unittest import mock as _mock

    monkeypatch.setenv("PARSER_EMBEDDED", "0")
    monkeypatch.setenv("COREBOT_ENV", "local")
    monkeypatch.setattr(main, "bootstrap_defaults", lambda: None)
    fake_db = _mock.Mock()
    fake_db.connect = _mock.AsyncMock()
    fake_db.disconnect = _mock.AsyncMock()
    monkeypatch.setattr(main, "bot_db", fake_db)
    monkeypatch.setattr(health_mod, "probe_control_plane_db", lambda: True)

    async def _bot_ok():
        return True

    monkeypatch.setattr(health_mod, "probe_bot_db", _bot_ok)
    monkeypatch.setattr(
        health_mod.background_tasks,
        "snapshot",
        lambda: {"active": 0, "active_names": [], "failed": 0, "failures": []},
    )
    from fastapi.testclient import TestClient

    client = TestClient(main.app)
    t_enter = time.perf_counter()
    client.__enter__()
    try:
        startup_sec = time.perf_counter() - t_enter
        ready_lat: list[float] = []
        for _ in range(LOAD_API_READY_CALLS):
            t0 = time.perf_counter()
            resp = client.get("/health/ready")
            ready_lat.append(time.perf_counter() - t0)
            assert resp.status_code == 200
            assert resp.json().get("ok") is True
        for _ in range(LOAD_API_VERSION_CALLS):
            resp = client.get("/version")
            assert resp.status_code == 200
            body = resp.json()
            assert set(body) <= {
                "version",
                "sha",
                "python_requires",
                "ubuntu",
                "released_at",
                "code_checksum",
            }
            assert "version" in body and "sha" in body
        t_exit = time.perf_counter()
    finally:
        client.__exit__(None, None, None)
    shutdown_sec = time.perf_counter() - t_exit
    ready_p95_sec = _p95(ready_lat)
    sys.modules.pop("control_plane.main", None)

    # --- engine dispose before backup phase -----------------------------------
    asyncio.run(engine.dispose())

    # --- (g) backup+restore of a synthetic tmp instance -----------------------
    repo_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo_root / "scripts"))
    from backup_lib import db_integrity_check, sqlite_backup  # noqa: E402

    stage = tmp_path / "stage"
    (stage / "data" / "sessions").mkdir(parents=True, exist_ok=True)
    (stage / "logs").mkdir(parents=True, exist_ok=True)
    (stage / ".env").write_text(SYNTHETIC_ENV, encoding="utf-8")
    (stage / "data" / "sessions" / "acc.session").write_bytes(b"fake-session-bytes")
    (stage / "logs" / "app.log").write_text("log\n", encoding="utf-8")
    for name in ("corebot.db", "control_plane.db"):
        conn = sqlite3.connect(str(stage / "data" / name))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS probe (id INTEGER PRIMARY KEY, v TEXT)"
            )
            for i in range(LOAD_BACKUP_ROWS):
                conn.execute("INSERT INTO probe (v) VALUES (?)", (f"v{i}",))
            conn.commit()
        finally:
            conn.close()

    backup_dir = tmp_path / "backup"
    backup_dir.mkdir(exist_ok=True)
    restore_dir = tmp_path / "restore"
    t_bak = time.perf_counter()
    for name in ("corebot.db", "control_plane.db"):
        sqlite_backup(stage / "data" / name, backup_dir / name)
    archive = tmp_path / "corebot-load-test.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(stage / ".env", arcname=".env")
        tar.add(backup_dir / "corebot.db", arcname="data/corebot.db")
        tar.add(backup_dir / "control_plane.db", arcname="data/control_plane.db")
        tar.add(
            stage / "data" / "sessions" / "acc.session",
            arcname="data/sessions/acc.session",
        )
    backup_sec = time.perf_counter() - t_bak
    archive_bytes = archive.stat().st_size

    t_res = time.perf_counter()
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(restore_dir, filter="data")
    assert db_integrity_check(restore_dir / "data" / "corebot.db") == "ok"
    assert db_integrity_check(restore_dir / "data" / "control_plane.db") == "ok"
    restore_sec = time.perf_counter() - t_res

    # --- thresholds ------------------------------------------------------------
    assert p95_write_ms <= TH_P95_WRITE_MS, f"p95 write {p95_write_ms:.1f}ms"
    assert retries_per_min <= TH_BUSY_RETRIES_PER_MIN, (
        f"busy retries/min {retries_per_min:.2f}"
    )
    assert busy["exhausted"] == 0, f"busy exhausted: {busy}"
    assert wal_bytes < TH_WAL_BYTES, f"WAL {wal_bytes} bytes"
    assert ready_p95_sec <= TH_READY_P95_SEC, f"ready p95 {ready_p95_sec:.3f}s"
    assert startup_sec <= TH_STARTUP_SEC, f"startup {startup_sec:.3f}s"
    assert shutdown_sec <= TH_SHUTDOWN_SEC, f"shutdown {shutdown_sec:.3f}s"
    assert (backup_sec + restore_sec) < TH_RESTORE_TOTAL_SEC

    metrics = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "params": {
            "db_tasks": LOAD_DB_TASKS,
            "outbound_per_task": LOAD_OUTBOUND_PER_TASK,
            "counter_tasks": LOAD_COUNTER_TASKS,
            "claim_tasks": LOAD_CLAIM_TASKS,
            "api_ready_calls": LOAD_API_READY_CALLS,
            "api_version_calls": LOAD_API_VERSION_CALLS,
            "queue_rows": LOAD_QUEUE_ROWS,
            "queue_batch": LOAD_QUEUE_BATCH,
            "backup_rows_per_db": LOAD_BACKUP_ROWS,
        },
        "db_write_storm": {
            "outbound_rows": n_outbound,
            "counter_final": counter_counts,
            "claim_winners": claim_winners,
            "p95_single_write_ms": round(p95_write_ms, 2),
            "storm_wall_sec": round(db_wall_sec, 2),
            "db_file_bytes": db_file_bytes,
        },
        "sqlite_busy": {
            **{k: int(v) for k, v in busy.items()},
            "retries_per_min": round(retries_per_min, 3),
        },
        "wal_bytes_after_storm": wal_bytes,
        "api": {
            "ready_p95_sec": round(ready_p95_sec, 3),
            "ready_calls": LOAD_API_READY_CALLS,
            "version_calls": LOAD_API_VERSION_CALLS,
        },
        "queue_drain": {
            "rows": LOAD_QUEUE_ROWS,
            "drained": drained,
            "left_pending": left_pending,
            "drain_sec": round(drain_sec, 2),
        },
        "lifecycle": {
            "startup_sec": round(startup_sec, 3),
            "shutdown_sec": round(shutdown_sec, 3),
        },
        "backup_restore": {
            "backup_sec": round(backup_sec, 2),
            "restore_sec": round(restore_sec, 2),
            "total_sec": round(backup_sec + restore_sec, 2),
            "archive_bytes": archive_bytes,
        },
        "thresholds": {
            "p95_write_ms": TH_P95_WRITE_MS,
            "busy_retries_per_min": TH_BUSY_RETRIES_PER_MIN,
            "wal_bytes": TH_WAL_BYTES,
            "ready_p95_sec": TH_READY_P95_SEC,
            "startup_sec": TH_STARTUP_SEC,
            "shutdown_sec": TH_SHUTDOWN_SEC,
            "restore_total_sec": TH_RESTORE_TOTAL_SEC,
        },
    }
    artifact = tmp_path / ARTIFACT_NAME
    artifact.write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("LOAD-ACCEPTANCE-SUMMARY " + json.dumps(metrics, ensure_ascii=False))
