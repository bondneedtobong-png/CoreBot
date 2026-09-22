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

## Remote flow

1. Upload source and env file.
2. Run installer with `--dry-run`.
3. Run installer as root.
4. Run verifier.
5. Access the panel through `ssh -L 8081:127.0.0.1:8081 user@vps`.

## Update flow

Back up first, stage new source separately, preserve `.env`/`data`/`logs`, install changed requirements, restart both services, and require readiness HTTP 200.
