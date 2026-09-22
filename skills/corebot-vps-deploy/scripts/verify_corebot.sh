#!/usr/bin/env bash
set -euo pipefail
APP_DIR=${APP_DIR:-/opt/corebot/app}
ENV_FILE=${ENV_FILE:-$APP_DIR/.env}
READY_URL=${READY_URL:-http://127.0.0.1:8081/health/ready}
[[ -f "$ENV_FILE" ]] || { echo "Missing env file: $ENV_FILE" >&2; exit 2; }
get_env() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1 | tr -d '\r'; }
for key in API_ID API_HASH BOT_TOKEN OWNER_ID DATABASE_URL BOT_DATABASE_URL CP_DATABASE_URL CP_JWT_SECRET CP_BOOTSTRAP_ADMIN_USERNAME CP_BOOTSTRAP_ADMIN_PASSWORD; do
  [[ -n "$(get_env "$key")" ]] || { echo "Missing required env key: $key" >&2; exit 3; }
done
if grep -Eqi '(^|=)(change-me[^[:space:]]*|admin123)$' "$ENV_FILE"; then
  echo "Unsafe default secret/password in env file" >&2
  exit 3
fi
jwt_secret=$(get_env CP_JWT_SECRET)
admin_password=$(get_env CP_BOOTSTRAP_ADMIN_PASSWORD)
[[ ${#jwt_secret} -ge 32 ]] || { echo "CP_JWT_SECRET must be at least 32 characters" >&2; exit 3; }
[[ ${#admin_password} -ge 12 ]] || { echo "CP_BOOTSTRAP_ADMIN_PASSWORD must be at least 12 characters" >&2; exit 3; }
mode=$(get_env PARSER_EMBEDDED || true)
if [[ "${mode:-1}" == 1 ]] && { systemctl is-enabled --quiet corebot-parser.service 2>/dev/null || systemctl is-active --quiet corebot-parser.service 2>/dev/null; }; then
  echo "Embedded parser and standalone corebot-parser.service cannot be enabled together" >&2
  exit 4
fi
# Shared Python validator (task 04) as the authoritative gate. The Bash checks
# above stay: they parse the env file directly with no Python dependency.
# Nothing is removed here on purpose (defense in depth, pre-venv stage).
if [[ -f "$APP_DIR/tools/validate_config.py" ]]; then
  if [[ -x /opt/corebot/venv/bin/python ]]; then
    VALIDATOR_PY=/opt/corebot/venv/bin/python
  elif command -v python3 >/dev/null 2>&1; then
    VALIDATOR_PY=python3
  else
    VALIDATOR_PY=
  fi
  if [[ -n "${VALIDATOR_PY:-}" ]]; then
    (cd "$APP_DIR" && COREBOT_ENV=production "$VALIDATOR_PY" -m tools.validate_config --mode production --env-file "$ENV_FILE") || exit 3
  else
    echo "WARNING: no Python found; shared config validator skipped, Bash checks only" >&2
  fi
else
  echo "WARNING: shared config validator not found in $APP_DIR; Bash checks only" >&2
fi
systemctl is-active --quiet corebot-cp.service
systemctl is-active --quiet corebot.service
curl --fail --silent --show-error "$READY_URL"
if command -v ss >/dev/null 2>&1; then
  listen=$(ss -ltnH '( sport = :8081 )' 2>/dev/null || true)
  [[ -n "$listen" ]] || { echo "Control Plane is not listening on 8081" >&2; exit 5; }
  if grep -qE '(^|[[:space:]])0\.0\.0\.0:8081|\[::\]:8081' <<<"$listen"; then
    echo "Control Plane is exposed publicly on port 8081" >&2
    exit 5
  fi
fi
echo
echo "CoreBot verification passed"
