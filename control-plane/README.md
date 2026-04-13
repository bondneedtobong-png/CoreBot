## Control Plane

Centralized service for distributed CoreBot agents.

### MVP capabilities
- Multi-tenant data isolation (`tenant_id` on every business row).
- Agent ingest API (events/logs/metrics batches).
- Web auth API (admin-created credentials, JWT).
- Dashboard API (tenant-scoped overview, logs, agent status).
- Alert engine with dedup/suppression and Telegram notifications.

### Run locally
1. Install deps from `requirements.txt`
2. Set env vars:
   - `CP_DATABASE_URL` (default `sqlite:///data/control_plane.db`)
   - `CP_JWT_SECRET`
   - `CP_BOOTSTRAP_ADMIN_USERNAME`
   - `CP_BOOTSTRAP_ADMIN_PASSWORD`
   - optional `CP_TELEGRAM_BOT_TOKEN`, `CP_TELEGRAM_ALERT_CHAT_ID`
3. Start:
   - `uvicorn control_plane.main:app --reload --port 8081`

### Security notes
- Do not commit `CP_JWT_SECRET` and agent tokens.
- Rotate agent tokens periodically.
- Use HTTPS in production.
