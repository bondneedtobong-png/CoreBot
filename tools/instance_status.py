"""Fleet status CLI (task 08): snapshot + readiness + version, no secrets.

Usage::

    python -m tools.instance_status --json      # machine-readable (fleet automation, task 07)
    python -m tools.instance_status             # human-readable summary
    python -m tools.instance_status --report-update-exit 3 --target-sha <sha>
                                                # journal + announce an update result (task 06)

External coverage only: the CLI reads the shared DBs/files from the side
(no new ports; nothing is served). In-memory CP state (supervised tasks,
live parser handle, this-process busy stats) is visible only to the in-CP
watchdog — the CLI marks such sections ``external-view``/``unknown`` instead
of false-reporting them.

Exit codes: ``0`` = instance serving (overall ``ok`` or ``degraded``),
``2`` = instance ``down`` or a fatal CLI error. No secrets are ever printed:
the payload goes through the task-08 sanitizer and the process aborts with
exit ``2`` if a configured secret value is detected in the output.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tools.instance_status",
        description="CoreBot instance status: snapshot + readiness + version (no secrets).",
    )
    parser.add_argument(
        "--json", action="store_true", help="machine-readable JSON output"
    )
    parser.add_argument(
        "--app-dir",
        default=str(APP_ROOT),
        help="application directory (default: repository root)",
    )
    parser.add_argument(
        "--report-update-exit",
        type=int,
        default=None,
        help="journal + announce scripts/update_corebot.sh exit code (0/3/4)",
    )
    parser.add_argument(
        "--target-sha", default="", help="update target SHA for --report-update-exit"
    )
    parser.add_argument(
        "--backup-path", default="", help="backup archive path for --report-update-exit"
    )
    return parser


def build_payload(app_dir: Path) -> dict:
    from control_plane.services.snapshot import (
        CollectOptions,
        build_snapshot,
        collect_live_inputs,
    )

    inputs = collect_live_inputs(
        CollectOptions(app_dir=Path(app_dir), in_process=False)
    )
    snapshot = build_snapshot(inputs)
    return {
        "snapshot": snapshot,
        "readiness": snapshot.get("readiness", {}),
        "version": (snapshot.get("components", {}) or {})
        .get("version", {})
        .get("detail", {}),
        "coverage": "external",
    }


def _leak_check(text: str) -> str | None:
    """Return the offending variable name if a configured secret leaks."""
    for name in (
        "BOT_TOKEN",
        "API_HASH",
        "CP_JWT_SECRET",
        "CP_AGENT_TOKEN",
        "CP_TELEGRAM_BOT_TOKEN",
        "OPENROUTER_API_KEY",
        "CONTROL_BOT_PROXY_PASSWORD",
    ):
        value = (os.getenv(name, "") or "").strip()
        if len(value) >= 8 and value in text:
            return name
    return None


def render_human(payload: dict) -> str:
    snapshot = payload.get("snapshot", {}) or {}
    readiness = payload.get("readiness", {}) or {}
    version = payload.get("version", {}) or {}
    components = snapshot.get("components", {}) or {}
    lines = [
        f"overall: {snapshot.get('overall')}  "
        f"readiness: {readiness.get('http')}  checked_at: {snapshot.get('checked_at')}",
        f"version: {version.get('version')} sha: {version.get('sha')} "
        f"deployed: {version.get('deployed_sha')} match: {version.get('match')}",
        f"last_tick: {snapshot.get('last_tick')}",
    ]
    for name in (
        "bot",
        "control_plane_db",
        "bot_db",
        "parser",
        "background_tasks",
        "consumer:outbound",
        "consumer:bot_command",
        "queues",
        "storage",
        "signals",
        "mailing",
        "worker_pool",
    ):
        comp = components.get(name) or {}
        lines.append(
            f"[{comp.get('state', '?'):^8}] {name:20} last_seen={comp.get('last_seen')}"
        )
    return "\n".join(lines)


def report_update_exit(exit_code: int, target_sha: str, backup_path: str) -> int:
    from control_plane.database import SessionLocal
    from control_plane.services.alerts import send_telegram_alert, upsert_alert
    from control_plane.services.watchdog import default_tenant_id, note_update_result

    spec = note_update_result(
        exit_code=exit_code, target_sha=target_sha, backup_path=backup_path
    )
    if spec is None:
        print(f"update exit {exit_code}: no alert (success/no-op or pre-swap failure)")
        return 0
    db = SessionLocal()
    try:
        tenant_id = default_tenant_id(db)
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
    finally:
        db.close()
    try:
        asyncio.run(send_telegram_alert(spec.title, spec.details))
    except Exception as exc:
        print(f"telegram send failed (journaled anyway): {exc}", file=sys.stderr)
    print(f"reported: [{spec.severity}] {spec.title}")
    return 0 if exit_code == 3 else 2


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.report_update_exit is not None:
        return report_update_exit(
            args.report_update_exit, args.target_sha, args.backup_path
        )
    try:
        payload = build_payload(Path(args.app_dir))
    except Exception as exc:
        print(f"tools.instance_status: error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    else:
        text = render_human(payload)
    offender = _leak_check(text)
    if offender:
        print(
            f"tools.instance_status: error: secret leak detected ({offender})",
            file=sys.stderr,
        )
        return 2
    print(text)
    overall = (payload.get("snapshot", {}) or {}).get("overall")
    return 0 if overall in ("ok", "degraded") else 2


if __name__ == "__main__":
    sys.exit(main())
