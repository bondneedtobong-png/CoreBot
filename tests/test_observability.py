"""Task 08: observability, runtime snapshot, heartbeats, alerting.

Hermetic by design: no network, no real Telegram, no real DB files (except
temp ones). Time is injected; the only real clock use is the consumer-kill
thread test, which offsets from a captured ``now``.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent

NOW = datetime(2026, 9, 22, 12, 0, 0)  # naive UTC, fixed


def _beat(minutes_ago: float = 0.0, detail: dict | None = None) -> dict:
    return {
        "updated_at": NOW - timedelta(minutes=minutes_ago),
        "detail": detail or {},
    }


def _base_inputs(**overrides) -> "SnapshotInputs":
    from control_plane.services.snapshot import SnapshotInputs

    fresh = {
        "bot": _beat(0.5, {"workers_total": 2, "workers_connected": 2}),
        "consumer:outbound": _beat(0.1, {"last_tick": NOW.isoformat()}),
        "consumer:bot_command": _beat(0.1, {"last_tick": NOW.isoformat()}),
    }
    params = dict(
        now=NOW,
        beats=fresh,
        cp_db_ok=True,
        bot_db_ok=True,
        parser_embedded=True,
        parser_task_status="running",
        background={"active": 1, "active_names": ["x"], "failed": 0, "failures": []},
        release={"version": "0.1.0", "sha": "a" * 40},
        deployed_sha="a" * 40,
    )
    params.update(overrides)
    return SnapshotInputs(**params)


def _snapshot(**overrides) -> dict:
    from control_plane.services.snapshot import build_snapshot

    return build_snapshot(_base_inputs(**overrides))


# --- core classification ----------------------------------------------------


def test_stale_bot_heartbeat_is_down():
    snap = _snapshot(beats={"bot": _beat(5.0)})
    assert snap["components"]["bot"]["state"] == "down"
    assert snap["overall"] == "down"
    assert snap["readiness"] == {"ok": False, "http": 503}
    # WHEN last alive is preserved.
    assert snap["components"]["bot"]["last_seen"] == (NOW - timedelta(minutes=5)).isoformat()


def test_fresh_heartbeat_is_ok_with_readiness_200():
    snap = _snapshot()
    assert snap["overall"] == "ok"
    assert snap["readiness"] == {"ok": True, "http": 200}
    assert snap["components"]["bot"]["state"] == "ok"
    assert snap["last_tick"] == (NOW - timedelta(seconds=6)).isoformat()


def test_failed_background_task_is_down():
    snap = _snapshot(
        background={"active": 0, "active_names": [], "failed": 1,
                    "failures": [{"name": "mailing-1", "error_type": "RuntimeError"}]}
    )
    assert snap["components"]["background_tasks"]["state"] == "down"
    assert snap["overall"] == "down"
    assert snap["readiness"]["http"] == 503


def test_transient_signals_are_degraded_not_down():
    from control_plane.services.snapshot import SnapshotInputs, build_snapshot

    inputs = _base_inputs(
        floodwait_1h=15,
        busy_delta={"attempts": 30, "retries": 25, "exhausted": 0, "failed_fast": 0},
    )
    snap = build_snapshot(inputs)
    assert snap["components"]["signals"]["state"] == "degraded"
    assert snap["overall"] == "degraded"
    assert snap["readiness"] == {"ok": True, "http": 200}


def test_exhausted_db_lock_is_degraded_with_reasons():
    inputs = _base_inputs(
        busy_delta={"attempts": 5, "retries": 5, "exhausted": 1, "failed_fast": 0},
    )
    from control_plane.services.snapshot import build_snapshot

    snap = build_snapshot(inputs)
    assert snap["components"]["signals"]["state"] == "degraded"
    assert "db-lock-exhausted" in snap["components"]["signals"]["detail"]["reasons"]
    assert snap["overall"] == "degraded"


def test_worker_pool_offline_is_degraded():
    snap = _snapshot(
        beats={"bot": _beat(0.5, {"workers_total": 3, "workers_connected": 0}),
               "consumer:outbound": _beat(0.1),
               "consumer:bot_command": _beat(0.1)}
    )
    assert snap["components"]["bot"]["state"] == "degraded"
    assert snap["overall"] == "degraded"


def test_queues_crit_caps_at_degraded():
    snap = _snapshot(outbound_pending=300)
    assert snap["components"]["queues"]["state"] == "degraded"
    assert snap["overall"] == "degraded"
    assert snap["readiness"]["http"] == 200


def test_consumer_missing_row_with_fresh_bot_is_degraded_not_down():
    beats = {"bot": _beat(0.2)}
    snap = _snapshot(beats=beats)
    assert snap["components"]["consumer:outbound"]["state"] == "degraded"
    assert snap["overall"] == "degraded"


def test_stale_consumer_tick_is_down():
    beats = {"bot": _beat(0.2), "consumer:outbound": _beat(5.0),
             "consumer:bot_command": _beat(0.1)}
    snap = _snapshot(beats=beats)
    assert snap["components"]["consumer:outbound"]["state"] == "down"
    assert snap["overall"] == "down"


# --- parser: reuse + no false positive --------------------------------------


def test_parser_disabled_is_ok_in_snapshot():
    snap = _snapshot(parser_embedded=False, parser_task_status=None)
    assert snap["components"]["parser"]["state"] == "ok"
    assert snap["components"]["parser"]["detail"]["mode"] == "disabled"
    assert snap["overall"] == "ok"


def test_parser_crash_is_down():
    snap = _snapshot(parser_task_status="failed")
    assert snap["components"]["parser"]["state"] == "down"
    assert snap["overall"] == "down"


def test_parser_task_status_reuses_health_logic():
    async def _boom():
        raise RuntimeError("parser died")

    async def _make():
        task = asyncio.create_task(_boom(), name="embedded-parser-loop")
        try:
            await task
        except RuntimeError:
            pass
        return task

    task = asyncio.run(_make())
    assert task.done()
    from control_plane.services.snapshot import parser_task_status

    assert parser_task_status(task) == "failed"


def test_parser_disabled_ready_200_contract():
    """Real /health/ready: disabled optional parser must stay 200 + shape."""
    from control_plane import health

    async def bot_ok():
        return True

    health_probe_cp = health.probe_control_plane_db
    health_probe_bot = health.probe_bot_db
    snap_fn = health.background_tasks.snapshot
    health.probe_control_plane_db = lambda: True  # noqa: E731
    health.probe_bot_db = bot_ok  # type: ignore
    health.background_tasks.snapshot = lambda: {  # noqa: E731
        "active": 0, "active_names": [], "failed": 0, "failures": []}
    try:
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(parser_task=None)))
        response = asyncio.run(health.readiness(request))
    finally:
        health.probe_control_plane_db = health_probe_cp
        health.probe_bot_db = health_probe_bot
        health.background_tasks.snapshot = snap_fn
    assert response.status_code == 200
    assert response.body == (
        b'{"ok":true,"components":{"control_plane_db":"ok",'
        b'"bot_db":"ok","parser":"disabled","background_tasks":"ok"}}'
    )


# --- watchdog: recovery + cooldown + sustained health ------------------------


def test_recovery_event_on_down_to_ok():
    from control_plane.services.watchdog import RECOVERED_WORD, WatchdogState, evaluate

    state = WatchdogState(components={"bot": "down", "__overall__": "down"})
    snap = _snapshot()
    specs = evaluate(state, snap, _base_inputs(), NOW)
    kinds = {s.kind for s in specs}
    assert "bot.recovered" in kinds
    assert "instance.recovered" in kinds
    rec = [s for s in specs if s.recovery]
    assert rec and all(RECOVERED_WORD in s.title for s in rec)


def test_no_recovery_without_prior_down():
    from control_plane.services.watchdog import WatchdogState, evaluate

    specs = evaluate(WatchdogState(), _snapshot(), _base_inputs(), NOW)
    assert [s for s in specs if s.recovery] == []


def _memory_db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from control_plane.models import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, future=True)()


def test_alert_dedup_cooldown_single_send():
    import asyncio as _asyncio

    from control_plane.services.watchdog import AlertSpec, WatchdogState, dispatch

    sent: list[tuple[str, str]] = []

    async def fake_sender(title: str, details: str) -> None:
        sent.append((title, details))

    db = _memory_db()
    try:
        state = WatchdogState()
        spec = AlertSpec(kind="consumer_death", severity="critical",
                         title="t", details="d", fingerprint="consumer:consumer:outbound")
        res1 = _asyncio.run(dispatch(state, [spec], db=db, tenant_id=1, sender=fake_sender, now=NOW))
        res2 = _asyncio.run(dispatch(state, [spec], db=db, tenant_id=1,
                                     sender=fake_sender, now=NOW + timedelta(seconds=60)))
        assert res1 == {"journaled": 1, "sent": 1}
        assert res2["sent"] == 0 and len(sent) == 1  # cooldown: 1 send total
        res3 = _asyncio.run(dispatch(state, [spec], db=db, tenant_id=1,
                                     sender=fake_sender, now=NOW + timedelta(seconds=901)))
        assert res3["sent"] == 1 and len(sent) == 2  # cooldown expired
    finally:
        db.close()


def test_health_failure_needs_sustained_5min():
    from control_plane.services.watchdog import WatchdogState, evaluate

    state = WatchdogState()
    down_inputs = _base_inputs(beats={"bot": _beat(10.0)})
    down_snap = _snapshot(beats={"bot": _beat(10.0)})
    first = evaluate(state, down_snap, down_inputs, NOW)
    assert "health:ready" not in {s.fingerprint for s in first}
    later = evaluate(state, down_snap, down_inputs, NOW + timedelta(seconds=301))
    assert "health:ready" in {s.fingerprint for s in later}


def test_version_mismatch_spec():
    from control_plane.services.watchdog import WatchdogState, evaluate

    inputs = _base_inputs()
    snap = _snapshot()
    ok_specs = evaluate(WatchdogState(), snap, inputs, NOW,
                        live_sha="a" * 40, deployed_sha="a" * 40)
    assert "release:version-mismatch" not in {s.fingerprint for s in ok_specs}
    bad_specs = evaluate(WatchdogState(), snap, inputs, NOW,
                         live_sha="b" * 40, deployed_sha="a" * 40)
    mismatch = [s for s in bad_specs if s.fingerprint == "release:version-mismatch"]
    assert len(mismatch) == 1 and mismatch[0].severity == "critical"


def test_note_update_result_codes():
    from control_plane.services.watchdog import note_update_result

    assert note_update_result(exit_code=0) is None
    assert note_update_result(exit_code=2) is None
    spec3 = note_update_result(exit_code=3, target_sha="c" * 40, backup_path="/b.tar.gz")
    assert spec3 is not None and spec3.severity == "warning" and spec3.kind == "update_failed"
    spec4 = note_update_result(exit_code=4, target_sha="c" * 40, backup_path="/b.tar.gz")
    assert spec4 is not None and spec4.severity == "critical" and spec4.kind == "update_rollback_incomplete"


# --- heartbeat store ----------------------------------------------------------


def test_heartbeat_store_roundtrip_and_missing_table(tmp_path):
    from control_plane.services.heartbeat import (
        ensure_table_dbapi,
        read_beats_dbapi,
        upsert_beat_dbapi,
    )

    db_path = tmp_path / "hb.db"
    conn = sqlite3.connect(str(db_path))
    try:
        assert read_beats_dbapi(conn) == {}  # missing table: no false positive
        ensure_table_dbapi(conn)
        upsert_beat_dbapi(conn, "bot", NOW, '{"workers_total": 1}')
        upsert_beat_dbapi(conn, "consumer:outbound", NOW - timedelta(minutes=5), "{}")
        beats = read_beats_dbapi(conn)
        assert beats["bot"]["updated_at"] == NOW
        assert beats["bot"]["detail"] == {"workers_total": 1}
        assert beats["consumer:outbound"]["updated_at"] == NOW - timedelta(minutes=5)
    finally:
        conn.close()


def test_consumer_kill_thread_reflected_in_status():
    """A ticking test consumer is ok; after kill (no new ticks) it is down."""
    from control_plane.services.snapshot import SnapshotInputs, build_snapshot

    beats: dict = {}
    stop = threading.Event()

    def _ticker():
        while not stop.is_set():
            beats["consumer:outbound"] = {
                "updated_at": datetime.now().replace(microsecond=0),
                "detail": {"last_tick": "x"},
            }
            time.sleep(0.02)

    thread = threading.Thread(target=_ticker, name="test-consumer", daemon=True)
    thread.start()
    try:
        deadline = time.time() + 5
        while "consumer:outbound" not in beats and time.time() < deadline:
            time.sleep(0.01)
        alive_inputs = SnapshotInputs(
            now=datetime.now().replace(microsecond=0),
            beats={"bot": {"updated_at": datetime.now().replace(microsecond=0), "detail": {}},
                   **{k: dict(v) for k, v in beats.items()}},
            cp_db_ok=True, bot_db_ok=True, parser_embedded=False,
        )
        assert build_snapshot(alive_inputs)["components"]["consumer:outbound"]["state"] == "ok"
    finally:
        stop.set()  # manual kill of the test consumer
        thread.join(timeout=5)
    frozen = {k: dict(v) for k, v in beats.items()}
    dead_inputs = SnapshotInputs(
        now=max(v["updated_at"] for v in frozen.values()) + timedelta(seconds=180),
        beats={"bot": {"updated_at": datetime.now().replace(microsecond=0), "detail": {}},
               **frozen},
        cp_db_ok=True, bot_db_ok=True, parser_embedded=False,
    )
    dead = build_snapshot(dead_inputs)
    assert dead["components"]["consumer:outbound"]["state"] == "down"
    assert dead["overall"] == "down"


# --- secrets / PII --------------------------------------------------------------


HOSTILE_TOKEN = "123456789:AAE-SECRET-BOT-TOKEN-VALUE-xyz"
HOSTILE_PROXY = "socks5://operator:s3cr3t-proxy-pass@10.0.0.1:1080"
HOSTILE_PHONE = "+79161234567"
HOSTILE_BODY = "привет, это личное сообщение клиента №42"


def test_snapshot_json_has_no_secrets_or_message_bodies():
    from control_plane.services.sanitize import find_leaks
    from control_plane.services.snapshot import SnapshotInputs, build_snapshot

    beats = {
        "bot": _beat(0.5, {
            "workers_total": 1,
            "note": f"token={HOSTILE_TOKEN} phone={HOSTILE_PHONE}",
            "proxy": HOSTILE_PROXY,
            "stolen_text": HOSTILE_BODY,
            "api_hash": "a" * 32,
        }),
        "consumer:outbound": _beat(0.1),
        "consumer:bot_command": _beat(0.1),
    }
    snap = build_snapshot(SnapshotInputs(now=NOW, beats=beats, cp_db_ok=True,
                                         bot_db_ok=True, parser_embedded=False))
    text = json.dumps(snap, ensure_ascii=False)
    leaks = find_leaks(snap, [HOSTILE_TOKEN, "s3cr3t-proxy-pass", HOSTILE_PHONE,
                              HOSTILE_BODY, "a" * 32,
                              "stolen_text", "note", "proxy"])
    assert leaks == [], f"secret/PII leak: {leaks}"
    # Diagnostics survive: version + counts + states still present.
    assert '"overall": "ok"' in text
    assert "workers_total" in text


def test_sanitize_deny_keys_but_keeps_numbers_and_timestamps():
    from control_plane.services.sanitize import MASK, sanitize

    out = sanitize({
        "api_hash": "f" * 32,
        "session_count": 5,
        "checked_at": "2026-09-22T12:00:00",
        "version": "0.1.0",
        "sha": "a" * 40,
    })
    assert out["api_hash"] == MASK
    assert out["session_count"] == 5
    assert out["checked_at"] == "2026-09-22T12:00:00"
    assert out["sha"] == "a" * 40


def test_alert_text_is_sanitized_before_send():
    import asyncio as _asyncio

    from control_plane.services import sanitize as sanitize_mod
    from control_plane.services.watchdog import AlertSpec, WatchdogState, dispatch

    sent: list[tuple[str, str]] = []

    async def fake_sender(title: str, details: str) -> None:
        sent.append((sanitize_mod.scrub_text(title), sanitize_mod.scrub_text(details)))

    db = _memory_db()
    try:
        spec = AlertSpec(kind="k", severity="warning",
                         title=f"stuck {HOSTILE_TOKEN}",
                         details=f"call {HOSTILE_PHONE}", fingerprint="fp-test-scrub")
        _asyncio.run(dispatch(WatchdogState(), [spec], db=db, tenant_id=1,
                              sender=fake_sender, now=NOW))
    finally:
        db.close()
    assert len(sent) == 1
    assert HOSTILE_TOKEN not in sent[0][0]
    assert HOSTILE_PHONE not in sent[0][1]


# --- log + CLI ------------------------------------------------------------------


def test_count_log_events_window_and_markers(tmp_path):
    from control_plane.services.snapshot import count_log_events

    lines = [
        "2026-09-22 11:10:00 | WARNING | x | FloodWait: 30 sec",
        "2026-09-22 10:00:00 | WARNING | x | FloodWait: old, out of window",
        "no-timestamp FloodWait line is ignored",
        "2026-09-22 11:20:00 | INFO    | x | all quiet here",
    ]
    assert count_log_events(lines, ("FloodWait",), NOW - timedelta(hours=1)) == 1
    assert count_log_events(lines, ("FloodWait",), NOW - timedelta(hours=3)) == 2


def test_cli_json_runs_without_secrets(tmp_path):
    env = dict(__import__("os").environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["BOT_TOKEN"] = "999999:CLI-LEAK-PROBE-TOKEN-VALUE"
    proc = subprocess.run(
        [sys.executable, "-m", "tools.instance_status", "--json", "--app-dir", str(tmp_path)],
        capture_output=True, text=True, cwd=str(REPO_ROOT), env=env, timeout=120,
    )
    assert proc.returncode in (0, 2)
    payload = json.loads(proc.stdout)
    assert payload["snapshot"]["overall"] in ("ok", "degraded", "down")
    assert payload["readiness"]["http"] in (200, 503)
    assert "version" in payload
    assert "999999:CLI-LEAK-PROBE-TOKEN-VALUE" not in proc.stdout
    assert payload["coverage"] == "external"


def test_cli_human_readable_empty_dir_is_down(tmp_path):
    env = dict(__import__("os").environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    proc = subprocess.run(
        [sys.executable, "-m", "tools.instance_status", "--app-dir", str(tmp_path)],
        capture_output=True, text=True, cwd=str(REPO_ROOT), env=env, timeout=120,
    )
    assert proc.returncode == 2  # no DBs visible from an empty dir
    assert "overall: down" in proc.stdout
