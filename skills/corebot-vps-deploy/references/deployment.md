# Deployment procedure

## Preflight

- Target: Ubuntu 22.04 or 24.04, systemd, root/sudo access.
- Confirm at least 2 GB free disk space and outbound access to Telegram and Python package indexes.
- Stage the repository under `/tmp/CoreBot`; do not clone over `/opt/corebot/app`.
- Prepare an env file outside the repository with mode `600`.

Required env keys: `API_ID`, `API_HASH`, `BOT_TOKEN`, `OWNER_ID`, `CP_JWT_SECRET`, `CP_BOOTSTRAP_ADMIN_USERNAME`, and `CP_BOOTSTRAP_ADMIN_PASSWORD`.

Recommended paths:

```env
DATABASE_URL=sqlite+aiosqlite:////opt/corebot/app/data/corebot.db
BOT_DATABASE_URL=sqlite:////opt/corebot/app/data/corebot.db
CP_DATABASE_URL=sqlite:////opt/corebot/app/data/control_plane.db
PARSER_EMBEDDED=1
```

Generate secrets with `openssl rand -hex 32` without placing the resulting value in shell history.

Validate with the shared gate before starting services (validates bot +
Control Plane per `docs/operations/CONFIG_CONTRACT.md`; secrets are masked;
exit 0 ok, 1 usage error, 2 config error):

```bash
COREBOT_ENV=production python3 /tmp/CoreBot/tools/validate_config.py --mode production --env-file /root/corebot.env
```

The installer writes both systemd units with a fail-fast hook (no unit files
live in the repo; VPS units are not edited by hand):

```ini
ExecStartPre=/opt/corebot/venv/bin/python -m tools.validate_config --mode production
```

The installer/verifier Bash env checks are intentionally kept alongside the
validator: they parse the env file directly with no Python dependency
(pre-venv stage), the validator is the authoritative runtime gate.

## Remote flow

1. Upload source and env file.
2. Run installer with `--dry-run`.
3. Run installer as root.
4. Run verifier.
5. Access the panel through `ssh -L 8081:127.0.0.1:8081 user@vps`.

## Update flow (versioned, task 06)

Release artifact: `<tag> (<sha12>)` plus `RELEASE.json` manifest with
`version`, `sha`, `python_requires (>=3.11)`, `ubuntu (22.04, 24.04)`,
`released_at` (UTC) and `code_checksum` (sha256 of the code tree).
Live identity is served at `GET /version` (no secrets); `/health*`
contracts are unchanged. The deployed SHA record lives in
`/opt/corebot/app/.deployed_sha`; `scripts/release_status.sh` prints
manifest plus live `/version` (allowlisted fields only).

Run from the app checkout on the VPS (`scripts/update_corebot.sh`):

```bash
cd /opt/corebot/app
sudo bash scripts/update_corebot.sh --dry-run --sha <sha>   # plan only, touches nothing
sudo bash scripts/update_corebot.sh --sha <sha>             # or --tag <tag>, or --branch <name>
bash scripts/release_status.sh
```

Strict order inside the script (SLO: service stop <= 5 min):

1. Preflight: production validator, git state, Python 3.11+, free disk (>= 2 GB default).
2. Backup via `scripts/backup_corebot.sh`; the archive must exist and be non-empty.
3. Stage: `git archive <sha>` into a temp dir (never `git pull` / `git reset --hard` on the live checkout) plus a fresh `RELEASE.json`.
4. Dependencies from the staged tree.
5. Stop `corebot-cp.service`, then `corebot.service`; rsync the stage over the code with `--checksum`, excluding `.git`, `.env`, `data/`, `logs/`.
6. Start `corebot-cp.service`, then `corebot.service`.
7. Readiness gate: `/health/live` + `/health/ready` HTTP 200 (default 18 x 10 s), then live `/version` SHA must equal the target and both units must be active.
8. Success writes `.deployed_sha`; repeat of the same SHA is a logged no-op.

Rollback is automatic when step 7 fails: previous staged code is swapped
back, `.env`/`data/` are restored from the pre-update backup (logs are kept
for forensics), services restart in the same order, health is re-checked.
Exit codes: `0` ok/no-op/dry-run, `2` pre-swap failure (nothing changed),
`3` rolled back, `4` rollback incomplete (manual recovery, RUNBOOK 17).
