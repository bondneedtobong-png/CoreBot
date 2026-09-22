#!/usr/bin/env bash
set -euo pipefail
APP_DIR=/opt/corebot/app
VENV_DIR=/opt/corebot/venv
COREBOT_USER=corebot
SOURCE_DIR=
ENV_FILE=
PYTHON_BIN=
DRY_RUN=0
usage() {
  cat <<'EOF'
Usage: install_corebot.sh --source-dir PATH --env-file PATH [options]
  --app-dir PATH       default: /opt/corebot/app
  --venv-dir PATH      default: /opt/corebot/venv
  --user NAME          default: corebot
  --python PATH        explicit Python 3.11+ binary (default: auto-detect)
  --dry-run            validate and print the installation plan only
EOF
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --source-dir) SOURCE_DIR=${2:?}; shift 2 ;;
    --env-file) ENV_FILE=${2:?}; shift 2 ;;
    --app-dir) APP_DIR=${2:?}; shift 2 ;;
    --venv-dir) VENV_DIR=${2:?}; shift 2 ;;
    --user) COREBOT_USER=${2:?}; shift 2 ;;
    --python) PYTHON_BIN=${2:?}; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done
[[ -n "$SOURCE_DIR" && -f "$SOURCE_DIR/requirements.txt" ]] || { echo "Invalid --source-dir" >&2; exit 2; }
[[ -n "$ENV_FILE" && -f "$ENV_FILE" ]] || { echo "Invalid --env-file" >&2; exit 2; }
get_env() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1 | tr -d '\r'; }
for key in API_ID API_HASH BOT_TOKEN OWNER_ID DATABASE_URL BOT_DATABASE_URL CP_DATABASE_URL CP_JWT_SECRET CP_BOOTSTRAP_ADMIN_USERNAME CP_BOOTSTRAP_ADMIN_PASSWORD; do
  [[ -n "$(get_env "$key")" ]] || { echo "Missing required env key: $key" >&2; exit 3; }
done
if grep -Eqi '(^|=)(change-me[^[:space:]]*|admin123)$' "$ENV_FILE"; then
  echo "Unsafe default secret/password in env file" >&2
  exit 3
fi
[[ "$(get_env API_ID)" =~ ^[0-9]+$ ]] || { echo "API_ID must be numeric" >&2; exit 3; }
[[ "$(get_env OWNER_ID)" =~ ^[0-9]+$ ]] || { echo "OWNER_ID must be numeric" >&2; exit 3; }
jwt_secret=$(get_env CP_JWT_SECRET)
admin_password=$(get_env CP_BOOTSTRAP_ADMIN_PASSWORD)
[[ ${#jwt_secret} -ge 32 ]] || { echo "CP_JWT_SECRET must be at least 32 characters" >&2; exit 3; }
[[ ${#admin_password} -ge 12 ]] || { echo "CP_BOOTSTRAP_ADMIN_PASSWORD must be at least 12 characters" >&2; exit 3; }
[[ "$(get_env DATABASE_URL)" == sqlite+aiosqlite:////* ]] || { echo "DATABASE_URL must use an absolute SQLite path" >&2; exit 3; }
[[ "$(get_env BOT_DATABASE_URL)" == sqlite:////* ]] || { echo "BOT_DATABASE_URL must use an absolute SQLite path" >&2; exit 3; }
[[ "$(get_env CP_DATABASE_URL)" == sqlite:////* ]] || { echo "CP_DATABASE_URL must use an absolute SQLite path" >&2; exit 3; }
parser_mode=$(get_env PARSER_EMBEDDED || true)
[[ -z "$parser_mode" || "$parser_mode" == 0 || "$parser_mode" == 1 ]] || { echo "PARSER_EMBEDDED must be 0 or 1" >&2; exit 3; }
# Shared Python validator (task 04): single source of truth for bot + Control
# Plane rules (stdlib-only, runs on system python3 before the venv exists).
# The Bash checks above stay as the pre-venv early gate: they need no Python
# and parse the env file directly. Nothing is removed here on purpose.
if [[ -f "$SOURCE_DIR/tools/validate_config.py" ]]; then
  COREBOT_ENV=production python3 "$SOURCE_DIR/tools/validate_config.py" --mode production --env-file "$ENV_FILE" || {
    echo "Shared config validator rejected the env file (see errors above)" >&2
    exit 3
  }
else
  echo "WARNING: shared config validator not found in source dir; Bash checks only" >&2
fi
if [[ -d "$APP_DIR" && -n "$(find "$APP_DIR" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
  echo "Refusing to overwrite non-empty APP_DIR: $APP_DIR" >&2
  echo "Use the backup/update workflow for an existing installation." >&2
  exit 4
fi
if [[ $DRY_RUN -eq 1 ]]; then
  cat <<EOF
DRY RUN OK
source=$SOURCE_DIR
env_file=$ENV_FILE
app_dir=$APP_DIR
venv_dir=$VENV_DIR
user=$COREBOT_USER
python=${PYTHON_BIN:-auto}
control_plane=127.0.0.1:8081
parser_embedded=${parser_mode:-1}
persistent_paths=.env,data,logs,data/sessions
EOF
  exit 0
fi
[[ ${EUID:-$(id -u)} -eq 0 ]] || { echo "Run as root" >&2; exit 5; }
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y git rsync curl sqlite3 build-essential python3 python3-pip python3-dev python3-venv libgl1 libxkbcommon-x11-0 libxcb-cursor0
if [[ -z "$PYTHON_BIN" ]]; then
  if command -v python3.11 >/dev/null 2>&1; then
    PYTHON_BIN=python3.11
  elif python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
    PYTHON_BIN=python3
  else
    . /etc/os-release
    if [[ "${VERSION_ID:-}" == "22.04" ]]; then
      DEBIAN_FRONTEND=noninteractive apt-get install -y software-properties-common
      add-apt-repository -y ppa:deadsnakes/ppa
      apt-get update
      DEBIAN_FRONTEND=noninteractive apt-get install -y python3.11 python3.11-dev python3.11-venv
      PYTHON_BIN=python3.11
    else
      echo "Python 3.11+ is required; pass --python with a compatible binary" >&2
      exit 6
    fi
  fi
fi
"$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3, 11), sys.version'
if ! id "$COREBOT_USER" >/dev/null 2>&1; then
  useradd -r -m -s /bin/bash -d "$(dirname "$APP_DIR")" "$COREBOT_USER"
fi
mkdir -p "$APP_DIR" "$VENV_DIR" "$APP_DIR/data/sessions" "$APP_DIR/logs"
rsync -a --exclude .git --exclude .env --exclude data --exclude logs "$SOURCE_DIR/" "$APP_DIR/"
install -o "$COREBOT_USER" -g "$COREBOT_USER" -m 600 "$ENV_FILE" "$APP_DIR/.env"
chown -R "$COREBOT_USER:$COREBOT_USER" "$(dirname "$APP_DIR")"
runuser -u "$COREBOT_USER" -- "$PYTHON_BIN" -m venv "$VENV_DIR"
runuser -u "$COREBOT_USER" -- "$VENV_DIR/bin/python" -m pip install --upgrade pip
runuser -u "$COREBOT_USER" -- "$VENV_DIR/bin/python" -m pip install -r "$APP_DIR/requirements.txt"
cat > /etc/systemd/system/corebot-cp.service <<EOF
[Unit]
Description=CoreBot Control Plane
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=$COREBOT_USER
Group=$COREBOT_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStartPre=$VENV_DIR/bin/python -m tools.validate_config --mode production
ExecStart=$VENV_DIR/bin/uvicorn control_plane.main:app --host 127.0.0.1 --port 8081
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=$APP_DIR/data $APP_DIR/logs
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/corebot.service <<EOF
[Unit]
Description=CoreBot Telegram Service
After=network-online.target corebot-cp.service
Wants=network-online.target
[Service]
Type=simple
User=$COREBOT_USER
Group=$COREBOT_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStartPre=$VENV_DIR/bin/python -m tools.validate_config --mode production
ExecStart=$VENV_DIR/bin/python main.py
Restart=on-failure
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=$APP_DIR/data $APP_DIR/logs
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now corebot-cp.service corebot.service
echo "Installation finished. Run verify_corebot.sh; readiness must return HTTP 200."
