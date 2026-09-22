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

## Backups and restore (task 09)

- Backup via `scripts/backup_corebot.sh` (online: SQLite through the
  `.backup` API or `backup_lib.py`, never a blind live `.db`/`-wal` copy).
  Archive `corebot-<UTC>.tar.gz` (mode `600`) in `/opt/corebot/backups/`
  (mode `700`) holds `backup_manifest.json` (instance id/name, release
  version/SHA, UTC timestamp, DB `user_version` + `tables_hash`, per-file
  sha256/size) plus a sibling `<archive>.sha256` sidecar and a
  `.last_backup_ok` cron marker (watchdog/`instance_status` derive
  `backup_age_h` from archive mtime; alert when `> 26h`).
- Retention: keep 7 / 30 days; the newest successful backup is never
  deleted. Preflight refuses the run when free space is below
  `data x2 + 256MB`. Concurrent runs serialize via `flock` (duplicate
  exits 0 with no path — callers treat empty output as "no fresh backup").
- At-rest default is the protected `700`/`600` layout; `--encrypt`
  additionally writes `<archive>.enc` (openssl aes-256-cbc/pbkdf2,
  passphrase from a `600` file) for off-host copies.
- Daily schedule: `systemd/corebot-backup.service` + `corebot-backup.timer`
  (`03:17`, `RandomizedDelaySec=15min`, `Persistent=true`,
  `OnFailure=corebot-backup-alert.service`); install by copying the units
  to `/etc/systemd/system` and `systemctl enable --now` (idempotent).
- Restore only into an EMPTY directory with
  `scripts/restore_corebot.sh --archive <path> --target <empty-dir>`
  (sidecar + manifest verified first; live-instance overwrite is always
  refused, `--allow-nonempty` covers stray non-live files only); after
  restore both DBs must pass `integrity_check`.
- Full drill (timed, within RTO): `docs/operations/BACKUP_RESTORE_DRILL.md`
  (repeat at least every 90 days); automated coverage in
  `tests/test_backup_restore.py`.

## Commands

```bash
sudo bash scripts/install_corebot.sh --source-dir /tmp/CoreBot --env-file /root/corebot.env
sudo bash scripts/verify_corebot.sh
sudo bash scripts/backup_corebot.sh
sudo bash scripts/backup_corebot.sh --encrypt  # needs BACKUP_PASSPHRASE_FILE (0600, off-repo)
sudo bash scripts/restore_corebot.sh --archive /opt/corebot/backups/<corebot-UTC.tar.gz> --target /tmp/restore-drill
python3 /opt/corebot/app/scripts/backup_lib.py integrity-check --db /tmp/restore-drill/data/corebot.db
cd /opt/corebot/app && sudo bash scripts/update_corebot.sh --dry-run --sha <sha>
cd /opt/corebot/app && sudo bash scripts/update_corebot.sh --sha <sha>
bash scripts/release_status.sh
curl --fail http://127.0.0.1:8081/version
```

For failures, read `references/troubleshooting.md` before changing configuration.
