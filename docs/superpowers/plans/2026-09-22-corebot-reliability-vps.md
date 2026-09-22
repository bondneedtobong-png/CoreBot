# CoreBot Reliability and VPS Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Stabilize CoreBot background work, parser concurrency, health reporting, deployment documentation, and VPS installation automation.

**Architecture:** Add one shared supervisor for one-shot asyncio jobs, atomically claim parser tasks at the database boundary, keep conflict handling targeted, and expose liveness/readiness as explicit FastAPI routes. Package the production deployment contract as a validated personal Codex skill with non-destructive shell helpers.

**Tech Stack:** Python 3.11, asyncio, FastAPI, SQLAlchemy 2.x async/sync, SQLite WAL, pytest, Bash, systemd.

**Spec:** `docs/superpowers/specs/2026-09-22-corebot-reliability-vps-design.md`

## Global Constraints

- Preserve existing uncommitted user changes.
- Keep SQLite and existing service boundaries.
- Keep `/health` backward-compatible.
- Do not expose port 8081 publicly by default.
- Do not overwrite `.env`, databases, sessions, or logs.
- Use test-first RED/GREEN cycles for production behavior.

---

### Task 1: Background Task Supervisor

**Files:**
- Create: `utils/background_tasks.py`
- Create: `tests/test_background_tasks.py`
- Modify: `main.py`
- Modify: `bot/main.py`
- Modify: `bot/handlers/mailing.py`
- Modify: `workers/bot_command_consumer.py`
- Modify: `workers/manager.py`
- Modify: `services/database/accept_transcript.py`

**Interfaces:**
- Produces: `background_tasks.create(coro, *, name) -> asyncio.Task`, `snapshot() -> dict`, `shutdown() -> None`.

- [x] Write tests proving tasks are strongly retained, terminal failures are reported, and shutdown cancels/awaits remaining jobs.
- [x] Run `python -m pytest tests/test_background_tasks.py -q` and confirm failures because the supervisor does not exist.
- [x] Implement the minimal supervisor.
- [x] Replace one-shot `asyncio.create_task` calls with named supervised calls.
- [x] Drain the supervisor during `main.py` shutdown.
- [x] Run the focused tests and the existing suite.

### Task 2: Atomic Parser Claim and Idempotent Writes

**Files:**
- Modify: `workers/parser/task_runner.py`
- Modify: `workers/parser/storage.py`
- Create: `tests/test_parser_concurrency.py`

**Interfaces:**
- Produces: `claim_pending_task(session, task_id: int) -> bool`.
- Preserves: `upsert_channel`, `upsert_group`, `upsert_user`, `add_user_source_edge` call contracts.

- [x] Write a test in which two sessions try to claim the same pending task and exactly one succeeds.
- [x] Write tests showing duplicate parser entity/source writes are idempotent.
- [x] Run the focused tests and confirm expected failures.
- [x] Implement conditional task claiming and SQLite conflict-aware writes.
- [x] Run focused and full tests.

### Task 3: Liveness and Readiness

**Files:**
- Create: `control_plane/health.py`
- Modify: `control_plane/main.py`
- Create: `tests/test_health_readiness.py`

**Interfaces:**
- Produces: `/health`, `/health/live`, `/health/ready`.
- Consumes: `background_tasks.snapshot()`, `SessionLocal`, `bot_db`, `app.state.parser_task`.

- [x] Write tests for successful readiness and HTTP 503 on a failed dependency.
- [x] Run tests and confirm `/health/ready` is missing.
- [x] Implement dependency probes with sanitized component status.
- [x] Preserve the existing `/health` response.
- [x] Run focused and full tests.

### Task 4: Launch and Deployment Documentation

**Files:**
- Modify carefully: `QUICKSTART.md`
- Modify carefully: `RUNBOOK.md`
- Modify carefully: `.env.example`

- [x] Add a one-page local launch checklist for Windows and Linux.
- [x] State that `PARSER_EMBEDDED=1` and standalone parser are mutually exclusive.
- [x] Add generated-secret and unsafe-default checks.
- [x] Add post-deploy readiness, backup, and log-verification commands.
- [x] Review the diff to ensure existing user documentation is preserved.

### Task 5: Personal VPS Deployment Skill

**Files:**
- Create: `C:/Users/bond/.codex/skills/corebot-vps-deploy/SKILL.md`
- Create: `C:/Users/bond/.codex/skills/corebot-vps-deploy/agents/openai.yaml`
- Create: `C:/Users/bond/.codex/skills/corebot-vps-deploy/scripts/install_corebot.sh`
- Create: `C:/Users/bond/.codex/skills/corebot-vps-deploy/scripts/verify_corebot.sh`
- Create: `C:/Users/bond/.codex/skills/corebot-vps-deploy/scripts/backup_corebot.sh`
- Create: `C:/Users/bond/.codex/skills/corebot-vps-deploy/references/deployment.md`
- Create: `C:/Users/bond/.codex/skills/corebot-vps-deploy/references/troubleshooting.md`

- [x] Record baseline failure scenarios: overwriting an existing `.env`, exposing port 8081, running both parser modes, and updating without backup.
- [x] Initialize the skill with the bundled initializer.
- [x] Write the minimal skill and scripts that prevent those failures.
- [x] Exercise scripts in safe dry-run/temporary-directory modes.
- [x] Run `quick_validate.py` on the skill.

### Task 6: Final Verification

**Files:** all files changed by Tasks 1-5.

- [x] Run `python -m pytest -q`.
- [x] Run `python -m compileall -q bot control_plane database services workers utils main.py`.
- [x] Run focused shell syntax checks for skill scripts.
- [x] Inspect `git diff --check` and `git status --short`.
- [x] Report pre-existing warnings and distinguish them from regressions.

