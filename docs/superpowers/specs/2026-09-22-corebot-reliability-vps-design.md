# CoreBot Reliability and VPS Deployment Design

**Date:** 2026-09-22

## Goal

Stabilize CoreBot without replacing its current SQLite/systemd architecture: supervise fire-and-forget asyncio tasks, make parser task claiming safe across processes, expose meaningful health/readiness checks, preserve targeted database-conflict semantics, and provide a reusable personal skill for safe VPS installation.

## Constraints

- Preserve Python 3.11+, aiogram 3, Telethon, FastAPI, SQLAlchemy 2.x, and SQLite WAL.
- Do not replace the synchronous Control Plane repositories with async SQLAlchemy.
- Do not wrap every commit in generic `IntegrityError` handling.
- Preserve existing uncommitted user changes.
- Keep `/health` backward-compatible.
- Never expose the Control Plane beyond `127.0.0.1` by default.
- Never overwrite `.env`, SQLite databases, sessions, or logs during deployment/update.
- Embedded parser and standalone parser must not process the same task.

## Background Task Supervision

Create `utils/background_tasks.py` with a small `BackgroundTaskSupervisor`. It owns strong task references, records task names, logs terminal exceptions, supports snapshots for readiness, and cancels/awaits remaining tasks during shutdown. One global supervisor is used for one-shot jobs; components with an existing explicit lifecycle keep their own `_task` fields.

Replace untracked tasks for mailing starts, post-mailing worker restoration, and accept-transcript collection with the supervisor. Core application shutdown drains the supervisor before disconnecting databases.

## Parser Concurrency

Add an atomic claim operation in `workers/parser/task_runner.py`. A conditional SQL update changes one task from `pending` to `running`; the caller proceeds only when exactly one row is updated. Multiple parser processes may observe the same ID, but only one can claim it.

Parser entity writes remain short transactions. SQLite WAL and busy timeouts stay in place. Parser upsert operations use SQLite `ON CONFLICT` where an actual unique-key race exists instead of globally acquiring `BEGIN IMMEDIATE` locks.

## Database Errors

Expected unique conflicts are handled at user/API boundaries or converted into idempotent parser behavior. Unexpected `IntegrityError` and operational failures remain visible and roll back through the existing session lifecycle. Existing correct conflict handlers for groups/proxies remain unchanged.

## Health and Readiness

- `/health` remains a lightweight compatibility endpoint returning `ok`.
- `/health/live` confirms the API process is alive.
- `/health/ready` checks Control Plane DB, bot DB, parser task terminal failure, and supervised background-task failures.
- Readiness returns HTTP 503 when a required dependency is unavailable.
- Responses reveal component status but no paths, tokens, credentials, or raw exception tracebacks.

## Deployment Safety

The project documentation gets a compact launch/deployment map and an explicit warning against running both parser modes. VPS installation uses a dedicated `corebot` user, `/opt/corebot/app`, `/opt/corebot/venv`, two systemd services, loopback-only Control Plane, UFW with SSH only, generated secrets, and backups before updates.

A personal skill at `~/.codex/skills/corebot-vps-deploy` provides deterministic scripts and decision guidance. It supports Git or archive source, refuses destructive overwrite, preserves data, validates required environment values, installs systemd units, and verifies health/logs after startup.

## Testing

Tests cover task retention/error reporting/shutdown, atomic parser claiming, readiness success/failure, and idempotent parser writes. Existing 44 tests remain green. The deployment scripts are exercised in dry-run or temporary-directory modes; the skill is validated with the bundled skill validator.

