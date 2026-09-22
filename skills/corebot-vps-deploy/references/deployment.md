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

## Update flow

Back up first, stage new source separately, preserve `.env`/`data`/`logs`, install changed requirements, restart both services, and require readiness HTTP 200.
