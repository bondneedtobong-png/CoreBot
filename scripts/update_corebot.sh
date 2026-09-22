#!/usr/bin/env bash
# CoreBot versioned update with health gate and automatic rollback (task 06).
#
# Release artifact: <tag> (<sha12>) + RELEASE.json manifest + GET /version.
# Update order (SLO: service stop <= 5 min, ready 200 within 3 min):
#   STEP 1/8 preflight -> STEP 2/8 backup -> STEP 3/8 stage -> STEP 4/8 dependencies
#   -> STEP 5/8 stop-swap (corebot-cp, then corebot) -> STEP 6/8 start (same order)
#   -> STEP 7/8 readiness-gate -> STEP 8/8 success | auto-rollback.
#
# Safety: the persistent checkout is NEVER touched with `git reset --hard`,
# `git pull` or `git checkout`. The target SHA is exported with
# `git archive` into a temp staging dir and rsynced over the app code while
# `.env`, `data/`, `logs/` (and `.git`) are excluded. New-bot + old-CP state
# is forbidden: success requires live /version SHA == target SHA.
#
# Exit codes: 0 = success / no-op / dry-run; 2 = usage or pre-swap failure
# (nothing was changed); 3 = update failed but rolled back to previous SHA;
# 4 = rollback incomplete, manual recovery required (backup path printed).
#
# Env knobs (defaults = production paths; override for dry-run/tests):
#   APP_DIR, VENV_PY, PYTHON_BIN, BACKUP_SCRIPT, BACKUP_DIR, SYSTEMCTL, CURL,
#   HEALTH_BASE, READY_ATTEMPTS (18), READY_INTERVAL (10), CURL_TIMEOUT (10),
#   DEPLOYED_SHA_FILE ($APP_DIR/.deployed_sha), SKIP_VALIDATOR (0),
#   SKIP_PIP (0), MIN_FREE_MB (2048), BRANCH (main).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-/opt/corebot/app}"
VENV_PY="${VENV_PY:-/opt/corebot/venv/bin/python}"
BACKUP_SCRIPT="${BACKUP_SCRIPT:-$SCRIPT_DIR/../skills/corebot-vps-deploy/scripts/backup_corebot.sh}"
BACKUP_DIR="${BACKUP_DIR:-/opt/corebot/backups}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
CURL="${CURL:-curl}"
HEALTH_BASE="${HEALTH_BASE:-http://127.0.0.1:8081}"
READY_ATTEMPTS="${READY_ATTEMPTS:-18}"
READY_INTERVAL="${READY_INTERVAL:-10}"
CURL_TIMEOUT="${CURL_TIMEOUT:-10}"
DEPLOYED_SHA_FILE="${DEPLOYED_SHA_FILE:-$APP_DIR/.deployed_sha}"
SKIP_VALIDATOR="${SKIP_VALIDATOR:-0}"
SKIP_PIP="${SKIP_PIP:-0}"
MIN_FREE_MB="${MIN_FREE_MB:-2048}"
BRANCH="${BRANCH:-main}"

PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  if [[ -x "$VENV_PY" ]]; then PYTHON_BIN="$VENV_PY"; else PYTHON_BIN="python3"; fi
fi

TARGET_SHA=""
TARGET_TAG=""
DRY_RUN=0
STAGE_DIR=""
PREV_STAGE_DIR=""
BACKUP_ARCHIVE=""

usage() {
  cat <<'EOF'
Usage: update_corebot.sh [--sha SHA | --tag TAG | --branch NAME] [--dry-run]
  Pins an explicit git SHA (never "latest master"), stages it outside the
  persistent checkout, swaps code, gates on readiness and rolls back on failure.
EOF
}

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

cleanup_staging() {
  [[ -n "$STAGE_DIR" && -d "$STAGE_DIR" ]] && rm -rf "$STAGE_DIR"
  [[ -n "$PREV_STAGE_DIR" && -d "$PREV_STAGE_DIR" ]] && rm -rf "$PREV_STAGE_DIR"
  STAGE_DIR=""; PREV_STAGE_DIR=""
}
trap cleanup_staging EXIT

while [[ $# -gt 0 ]]; do
  case "$1" in
    --sha) TARGET_SHA="${2:?--sha needs a value}"; shift 2 ;;
    --tag) TARGET_TAG="${2:?--tag needs a value}"; shift 2 ;;
    --branch) BRANCH="${2:?--branch needs a value}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -n "$TARGET_SHA" && -n "$TARGET_TAG" ]]; then
  echo "ERROR: pass only one of --sha / --tag / --branch" >&2; exit 2
fi

deployed_sha() { [[ -f "$DEPLOYED_SHA_FILE" ]] && cat "$DEPLOYED_SHA_FILE" || true; }

# STEP 1/8 preflight: validator (production) + git + disk, then resolve target.
step_preflight() {
  echo "STEP 1/8 preflight"
  [[ -d "$APP_DIR/.git" ]] || { echo "ERROR: git repo not found: $APP_DIR" >&2; exit 2; }
  command -v git >/dev/null || { echo "ERROR: git not found" >&2; exit 2; }
  git config --global --add safe.directory "$APP_DIR" || true
  if [[ "$SKIP_VALIDATOR" != "1" ]]; then
    (cd "$APP_DIR" && COREBOT_ENV=production "$PYTHON_BIN" -m tools.validate_config --mode production) \
      || { echo "ERROR: production config validator failed" >&2; exit 2; }
  else
    log "preflight: validator skipped (SKIP_VALIDATOR=1)"
  fi
  "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
    || { echo "ERROR: Python 3.11+ required ($PYTHON_BIN)" >&2; exit 2; }
  avail_mb="$(df -m --output=avail "$APP_DIR" 2>/dev/null | tail -n 1 | tr -d ' ')"
  if [[ "$avail_mb" =~ ^[0-9]+$ ]] && [[ "$avail_mb" -lt "$MIN_FREE_MB" ]]; then
    echo "ERROR: free disk ${avail_mb}MB < required ${MIN_FREE_MB}MB; refusing update" >&2; exit 2
  fi
  if [[ -n "$TARGET_TAG" ]]; then
    TARGET_SHA="$(git -C "$APP_DIR" rev-list -n 1 "$TARGET_TAG" 2>/dev/null)" \
      || { echo "ERROR: unknown tag: $TARGET_TAG" >&2; exit 2; }
  elif [[ -z "$TARGET_SHA" ]]; then
    git -C "$APP_DIR" fetch origin \
      || { echo "ERROR: git fetch origin failed" >&2; exit 2; }
    TARGET_SHA="$(git -C "$APP_DIR" rev-parse "origin/$BRANCH")" \
      || { echo "ERROR: cannot resolve origin/$BRANCH" >&2; exit 2; }
  else
    [[ "$TARGET_SHA" =~ ^[0-9a-fA-F]{4,40}$ ]] || { echo "ERROR: bad --sha value" >&2; exit 2; }
    git -C "$APP_DIR" cat-file -e "${TARGET_SHA}^{commit}" 2>/dev/null \
      || { echo "ERROR: SHA not present in $APP_DIR: $TARGET_SHA" >&2; exit 2; }
    TARGET_SHA="$(git -C "$APP_DIR" rev-parse "$TARGET_SHA")"
  fi
  PREV_SHA="$(git -C "$APP_DIR" rev-parse HEAD)"
  log "preflight: target=$TARGET_SHA prev=$PREV_SHA"
}

check_noop() {
  local deployed
  deployed="$(deployed_sha)"
  if [[ -n "$deployed" && "$deployed" == "$TARGET_SHA" && "$PREV_SHA" == "$TARGET_SHA" ]]; then
    log "NO-OP: already at ${TARGET_SHA:0:12} (code and deployed record match)"
    return 0
  fi
  return 1
}

# STEP 2/8 backup: mandatory pre-update backup, archive verified.
step_backup() {
  echo "STEP 2/8 backup"
  [[ -x "$BACKUP_SCRIPT" ]] || { echo "ERROR: backup script not executable: $BACKUP_SCRIPT" >&2; exit 2; }
  BACKUP_ARCHIVE="$(BACKUP_DIR="$BACKUP_DIR" APP_DIR="$APP_DIR" "$BACKUP_SCRIPT" | tail -n 1)"
  [[ -n "$BACKUP_ARCHIVE" && -s "$BACKUP_ARCHIVE" ]] \
    || { echo "ERROR: backup produced no archive" >&2; exit 2; }
  log "backup OK: $BACKUP_ARCHIVE"
}

# STEP 3/8 stage: export target + previous SHAs outside the persistent checkout.
stage_sha() {
  local sha="$1" dest="$2"
  git -C "$APP_DIR" archive "$sha" | tar -x -C "$dest" \
    || { echo "ERROR: cannot stage $sha" >&2; exit 2; }
  "$PYTHON_BIN" "$SCRIPT_DIR/make_release.py" --root "$dest" --sha "$sha" \
    --output "$dest/RELEASE.json" >/dev/null \
    || { echo "ERROR: cannot write manifest for $sha" >&2; exit 2; }
}

step_stage() {
  echo "STEP 3/8 stage"
  STAGE_DIR="$(mktemp -d)"
  PREV_STAGE_DIR="$(mktemp -d)"
  stage_sha "$TARGET_SHA" "$STAGE_DIR"
  stage_sha "$PREV_SHA" "$PREV_STAGE_DIR"
  log "staged target=${TARGET_SHA:0:12} prev=${PREV_SHA:0:12}"
}

# STEP 4/8 dependencies.
step_dependencies() {
  echo "STEP 4/8 dependencies"
  if [[ "$SKIP_PIP" == "1" ]]; then
    log "dependencies skipped (SKIP_PIP=1)"; return 0
  fi
  [[ -x "$VENV_PY" ]] || { echo "ERROR: venv python not executable: $VENV_PY" >&2; exit 2; }
  "$VENV_PY" -m pip install -r "$STAGE_DIR/requirements.txt" \
    || { echo "ERROR: pip install failed" >&2; exit 2; }
}

# --checksum is mandatory: same-size files within one mtime second must still
# be synced, otherwise a rollback could leave new-code bytes behind.
RSYNC_FLAGS=(-a --delete --checksum --exclude /.git --exclude /.env
  --exclude /data --exclude /logs --exclude /RELEASE.json --exclude /.deployed_sha)

swap_tree() {
  local src="$1"
  rsync "${RSYNC_FLAGS[@]}" "$src/" "$APP_DIR/" \
    || { echo "ERROR: code swap failed" >&2; exit 2; }
  cp -p "$src/RELEASE.json" "$APP_DIR/RELEASE.json"
}

fix_permissions() {
  if [[ "$(id -u)" -eq 0 ]] && id corebot >/dev/null 2>&1; then
    chown -R corebot:corebot "$APP_DIR"
    chmod 600 "$APP_DIR/.env" 2>/dev/null || true
  fi
}

stop_services() {
  "$SYSTEMCTL" stop corebot-cp.service
  "$SYSTEMCTL" stop corebot.service
}

# Restart order is a contract: Control Plane first, then the bot.
restart_services() {
  "$SYSTEMCTL" restart corebot-cp.service
  "$SYSTEMCTL" restart corebot.service
}

# STEP 5/8 stop-swap + STEP 6/8 start.
step_stop_swap_start() {
  echo "STEP 5/8 stop-swap"
  stop_services
  swap_tree "$STAGE_DIR"
  fix_permissions
  echo "STEP 6/8 start"
  restart_services
}

# STEP 7/8 readiness-gate: /health/live + /health/ready 200 within budget.
wait_readiness() {
  echo "STEP 7/8 readiness-gate"
  local attempt=1
  while [[ "$attempt" -le "$READY_ATTEMPTS" ]]; do
    if "$CURL" --fail --silent --show-error --max-time "$CURL_TIMEOUT" \
        "$HEALTH_BASE/health/live" >/dev/null 2>&1 \
      && "$CURL" --fail --silent --show-error --max-time "$CURL_TIMEOUT" \
        "$HEALTH_BASE/health/ready" >/dev/null 2>&1; then
      log "readiness OK on attempt $attempt/$READY_ATTEMPTS"
      return 0
    fi
    log "readiness attempt $attempt/$READY_ATTEMPTS not ready"
    attempt=$((attempt + 1))
    if [[ "$attempt" -le "$READY_ATTEMPTS" && "$READY_INTERVAL" != "0" ]]; then
      sleep "$READY_INTERVAL"
    fi
  done
  return 1
}

live_sha() {
  "$CURL" --fail --silent --max-time "$CURL_TIMEOUT" "$HEALTH_BASE/version" 2>/dev/null \
    | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin).get("sha",""))' 2>/dev/null || true
}

# Version parity: live /version must equal the swapped SHA, both units active.
# This forbids the "new bot + old Control Plane" state before success.
check_version_parity() {
  local expected="$1" live
  live="$(live_sha)"
  if [[ -z "$live" ]]; then
    log "version check: /version unreachable"; return 1
  fi
  if [[ "${live:0:12}" != "${expected:0:12}" ]]; then
    log "version check: live=$live expected=${expected:0:12}"; return 1
  fi
  "$SYSTEMCTL" is-active --quiet corebot-cp.service || return 1
  "$SYSTEMCTL" is-active --quiet corebot.service || return 1
  log "version parity OK: ${expected:0:12} (cp + bot active)"
  return 0
}

step_success() {
  echo "STEP 8/8 success"
  echo -n "$TARGET_SHA" > "$DEPLOYED_SHA_FILE"
  log "UPDATE OK ${TARGET_SHA:0:12} (tag: ${TARGET_TAG:-n/a}) backup=$BACKUP_ARCHIVE"
}

# Auto-rollback: previous code + persistent paths from the pre-update backup,
# then services restarted cp -> bot and health re-checked.
step_rollback() {
  local reason="$1"
  echo "STEP 8/8 rollback ($reason)"
  swap_tree "$PREV_STAGE_DIR"
  if [[ "$SKIP_PIP" != "1" && -x "$VENV_PY" ]]; then
    "$VENV_PY" -m pip install -r "$PREV_STAGE_DIR/requirements.txt" || true
  fi
  local restore_dir
  restore_dir="$(mktemp -d)"
  if tar -xzf "$BACKUP_ARCHIVE" -C "$restore_dir"; then
    [[ -f "$restore_dir/.env" ]] && cp -p "$restore_dir/.env" "$APP_DIR/.env"
    [[ -d "$restore_dir/data" ]] && cp -a "$restore_dir/data/." "$APP_DIR/data/"
  else
    log "rollback WARNING: cannot extract $BACKUP_ARCHIVE; code restored, data untouched"
  fi
  rm -rf "$restore_dir"
  fix_permissions
  restart_services
  if wait_readiness && check_version_parity "$PREV_SHA"; then
    echo -n "$PREV_SHA" > "$DEPLOYED_SHA_FILE"
    log "UPDATE FAILED ($reason) - ROLLED BACK to ${PREV_SHA:0:12} backup=$BACKUP_ARCHIVE"
    exit 3
  fi
  echo "ROLLBACK INCOMPLETE after failed update ($reason)." >&2
  echo "Previous SHA: $PREV_SHA. Pre-update backup: $BACKUP_ARCHIVE." >&2
  echo "Manual recovery: RUNBOOK section 17." >&2
  exit 4
}

main() {
  step_preflight
  if check_noop; then exit 0; fi
  if [[ "$DRY_RUN" == "1" ]]; then
    cat <<EOF
DRY-RUN plan (no files or systemd units touched):
  app=$APP_DIR target=${TARGET_SHA:0:12} prev=${PREV_SHA:0:12}
  backup=$BACKUP_SCRIPT -> $BACKUP_DIR
  stage: git archive <sha> to temp dir (persistent checkout untouched)
  deps: $VENV_PY -m pip install -r <stage>/requirements.txt
  stop: corebot-cp.service then corebot.service
  swap: rsync stage -> app (excludes .git .env data logs RELEASE.json .deployed_sha)
  start: corebot-cp.service then corebot.service
  gate: $HEALTH_BASE/health/live + /health/ready x$READY_ATTEMPTS every ${READY_INTERVAL}s
  version: live /version sha must equal target; else auto-rollback to prev
EOF
    exit 0
  fi
  step_backup
  step_stage
  step_dependencies
  step_stop_swap_start
  if wait_readiness && check_version_parity "$TARGET_SHA"; then
    step_success
    exit 0
  fi
  step_rollback "readiness or version parity failed"
}

main "$@"
