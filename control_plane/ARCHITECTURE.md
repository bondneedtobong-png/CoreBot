## Control Plane Architecture Decisions

### Ingest
- Agents push batched events/metrics to `/ingest/batch`.
- Authentication via per-agent token in `X-Agent-Token`.
- Schema is versioned (`schema_version`) for forward compatibility.

### Auth and RBAC
- Web auth is admin-created credentials (username/password).
- JWT access/refresh tokens.
- Roles: `super_admin`, `tenant_admin`, `tenant_viewer`.

### Tenant Isolation
- Every row in business tables has `tenant_id`.
- API handlers scope data by `tenant_id` unless `super_admin`.
- Cross-tenant writes are blocked in admin endpoints.

### Storage
- Separate central DB (`CP_DATABASE_URL`) from local bot DB.
- Tables: tenants, users, agents, tokens, events, metrics, alerts, audit logs.

### Alerting
- Alert upsert by fingerprint with suppression window.
- Telegram notifications for critical/error classes in MVP.

### Operations
- Agent token rotation endpoint.
- Audit log for admin actions.
- Retention policy controlled by `CP_RETENTION_DAYS` (cleanup job can be attached later).
