---
name: corebot-vps-deploy
description: Use when installing, updating, verifying, backing up, or troubleshooting this CoreBot project on an Ubuntu VPS with systemd, SQLite, Telethon sessions, and a loopback-only FastAPI Control Plane.
---

# CoreBot VPS Deploy

Deploy CoreBot without losing `.env`, SQLite databases, logs, or Telegram sessions.

## Required inputs

Obtain the SSH host/user, source method (Git checkout or uploaded directory), and a completed environment file. Never place secrets directly in shell history; upload an env file with mode `600`.

## Workflow

1. Read `references/deployment.md` and inspect the VPS before changing it.
2. If `/opt/corebot/app` exists, run `scripts/backup_corebot.sh` first. Do not treat an existing installation as a first install.
3. Confirm exactly one parser mode: `PARSER_EMBEDDED=1` means no standalone parser service; `PARSER_EMBEDDED=0` permits one.
4. Run `scripts/install_corebot.sh --dry-run` with the intended paths.
5. Request explicit authorization immediately before SSH uploads, package installation, systemd writes, service restarts, firewall changes, or deployment.
6. Run the installer on the VPS as root, then run `scripts/verify_corebot.sh`.
7. For updates, use the versioned flow from the app checkout (`scripts/update_corebot.sh --sha <sha>`, dry-run first): it backs up, stages the SHA outside the persistent checkout, restarts `corebot-cp` then `corebot`, gates on readiness, and rolls back automatically on failure. Never `git pull` / `git reset --hard` the live checkout.
8. After any install or update, report deployed SHA/version via `scripts/release_status.sh` and live `GET /version`. Never report success if `/health/ready` is not HTTP 200.

## Safety contract

- The installer refuses a non-empty target directory.
- Never overwrite an existing `.env`, `data/`, `logs/`, or `data/sessions/`.
- Reject empty Telegram credentials and unsafe values such as `change-me` or `admin123`.
- Validate with the shared gate `python -m tools.validate_config --mode production` (plus `ExecStartPre` in both units); keep the scripts' Bash env checks as the pre-venv early gate.
- Bind the Control Plane to `127.0.0.1:8081`; do not open port 8081 in UFW.
- Use `/opt/corebot/app` and `/opt/corebot/venv` unless the user explicitly chooses other paths.
- For updates, back up first and update code without replacing persistent paths.
- Release artifact is `<tag> (<sha12>)` plus `RELEASE.json` manifest (version, sha, python_requires, ubuntu, released_at, code_checksum), served live at `GET /version` (no secrets). Update pins an explicit SHA; "latest master" updates are forbidden.
- Restart order is always `corebot-cp.service` then `corebot.service`; the "new bot + old Control Plane" state is forbidden (checked via `/version` before success).
- Rollback is automatic on readiness/version failure: previous code plus `.env`/`data/` from the pre-update backup, services restarted in the same order, health re-checked.

## Commands

```bash
sudo bash scripts/install_corebot.sh --source-dir /tmp/CoreBot --env-file /root/corebot.env
sudo bash scripts/verify_corebot.sh
sudo bash scripts/backup_corebot.sh
cd /opt/corebot/app && sudo bash scripts/update_corebot.sh --dry-run --sha <sha>
cd /opt/corebot/app && sudo bash scripts/update_corebot.sh --sha <sha>
bash scripts/release_status.sh
curl --fail http://127.0.0.1:8081/version
```

For failures, read `references/troubleshooting.md` before changing configuration.
