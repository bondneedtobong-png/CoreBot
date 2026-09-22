#!/usr/bin/env bash
# CoreBot data backup with manifest, retention and preflight (task 09).
#
# Contents (SLO minimum): .env, data/corebot.db, data/control_plane.db,
# data/sessions/ (plus logs/ best-effort). SQLite snapshots are consistent:
# `sqlite3 <db> ".backup <dst>"` when the CLI exists, otherwise the same
# backup API via scripts/backup_lib.py (stdlib) — never a blind copy of a
# live .db/-wal pair, the bot keeps running.
#
# Archive layout: backup_manifest.json (instance id/name, release
# version/SHA, UTC timestamp, DB user_version + tables_hash, per-file
# sha256/size, total bytes, duration) + payload. Sibling <archive>.sha256
# holds the archive hash plus the payload table (`sha256sum -c` compatible);
# restore_corebot.sh verifies both BEFORE extracting anything trusted.
#
# At-rest protection (default): BACKUP_DIR mode 700, archives/sidecars/
# markers mode 600. Optional --encrypt additionally writes <archive>.enc
# (openssl aes-256-cbc/pbkdf2, passphrase from a 600 file, never the repo)
# for off-host copies; the local 700/600 archive stays the watchdog-visible
# one (watchdog/instance_status read archive mtime, not .enc).
#
# Stdout contract: ONLY the final archive path goes to stdout (the update
# flow consumes `... | tail -n 1`); everything else goes to stderr.
#
# Exit codes: 0 ok (lock-held skip prints no path — callers treat empty output
# as "no fresh backup", which pre-swap checks already reject safely);
# 2 usage/config error; 3 preflight (disk space) refusal; 4 backup failure.
#
# Env knobs (defaults = production paths; override for drills/tests):
#   APP_DIR, BACKUP_DIR, PYTHON_BIN (default python3),
#   INSTANCE_ID / INSTANCE_NAME (default .instance_id file / hostname),
#   RETENTION_COUNT (7), RETENTION_DAYS (30),
#   SPACE_MULTIPLIER (2), SPACE_RESERVE_MB (256),
#   BACKUP_PASSPHRASE_FILE (required with --encrypt).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
APP_DIR="${APP_DIR:-/opt/corebot/app}"
BACKUP_DIR="${BACKUP_DIR:-/opt/corebot/backups}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
BACKUP_LIB="${BACKUP_LIB:-$REPO_ROOT/scripts/backup_lib.py}"
RETENTION_COUNT="${RETENTION_COUNT:-7}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
SPACE_MULTIPLIER="${SPACE_MULTIPLIER:-2}"
SPACE_RESERVE_MB="${SPACE_RESERVE_MB:-256}"
BACKUP_PASSPHRASE_FILE="${BACKUP_PASSPHRASE_FILE:-}"

ENCRYPT=0
DRY_RUN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --encrypt) ENCRYPT=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help)
      echo "Usage: backup_corebot.sh [--encrypt] [--dry-run]"
      echo "Env: APP_DIR BACKUP_DIR PYTHON_BIN BACKUP_LIB INSTANCE_ID INSTANCE_NAME"
      echo "     RETENTION_COUNT RETENTION_DAYS SPACE_MULTIPLIER SPACE_RESERVE_MB"
      echo "     BACKUP_PASSPHRASE_FILE (with --encrypt)"
      exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] backup: $*" >&2; }

[[ -d "$APP_DIR" ]] || { log "ERROR: missing APP_DIR: $APP_DIR"; exit 2; }
[[ -f "$BACKUP_LIB" ]] || { log "ERROR: missing backup lib: $BACKUP_LIB"; exit 2; }
if [[ "$ENCRYPT" == "1" ]]; then
  [[ -n "$BACKUP_PASSPHRASE_FILE" ]] || { log "ERROR: --encrypt needs BACKUP_PASSPHRASE_FILE"; exit 2; }
  [[ -f "$BACKUP_PASSPHRASE_FILE" ]] || { log "ERROR: passphrase file missing: $BACKUP_PASSPHRASE_FILE"; exit 2; }
  command -v openssl >/dev/null || { log "ERROR: openssl not found (needed for --encrypt)"; exit 2; }
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
created_iso="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
archive="$BACKUP_DIR/corebot-$stamp.tar.gz"

if [[ "$DRY_RUN" == "1" ]]; then
  echo "DRY RUN OK: backup $APP_DIR persistent data to $archive" >&2
  echo "DRY RUN plan: preflight(disk x$SPACE_MULTIPLIER+${SPACE_RESERVE_MB}MB) -> sqlite .backup -> manifest -> tar.gz(600) -> sidecar -> retention(keep=$RETENTION_COUNT,${RETENTION_DAYS}d) -> .last_backup_ok" >&2
  echo "$archive"
  exit 0
fi

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

# Non-parallelism: concurrent timer/manual runs serialize here; the loser
# exits 0 without touching anything (a skipped duplicate is not a failure).
exec 9>"$BACKUP_DIR/.backup.lock"
if ! flock -n 9; then
  log "another backup is already running (lock held); skipping duplicate"
  exit 0
fi

start_sec="$SECONDS"

# --- preflight: free space >= staged_bytes * MULTIPLIER + RESERVE ---------
data_bytes=0
if [[ -d "$APP_DIR/data" ]]; then
  data_bytes="$(du -sb "$APP_DIR/data" 2>/dev/null | cut -f1)"
fi
env_bytes=0
if [[ -f "$APP_DIR/.env" ]]; then
  env_bytes="$(wc -c <"$APP_DIR/.env" | tr -d ' ')"
fi
staged_bytes=$((data_bytes + env_bytes))
required_bytes=$((staged_bytes * SPACE_MULTIPLIER + SPACE_RESERVE_MB * 1024 * 1024))
avail_bytes="$(df -B1 --output=avail "$BACKUP_DIR" 2>/dev/null | tail -n 1 | tr -d ' ')"
if [[ "$avail_bytes" =~ ^[0-9]+$ ]] && [[ "$avail_bytes" -lt "$required_bytes" ]]; then
  log "ERROR: free ${avail_bytes}B < required ${required_bytes}B (data x$SPACE_MULTIPLIER + ${SPACE_RESERVE_MB}MB); refusing backup"
  exit 3
fi
log "preflight ok: data=${staged_bytes}B required=${required_bytes}B avail=${avail_bytes}B"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/data"

# --- consistent SQLite snapshots (online, WAL-safe) -------------------------
sqlite_snapshot() {
  local src="$1" dst="$2"
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$src" ".backup '$dst'"
  else
    "$PYTHON_BIN" "$BACKUP_LIB" sqlite-backup --src "$src" --dst "$dst"
  fi
}
for name in corebot.db control_plane.db; do
  if [[ -f "$APP_DIR/data/$name" ]]; then
    sqlite_snapshot "$APP_DIR/data/$name" "$tmp/data/$name" \
      || { log "ERROR: sqlite snapshot failed for $name"; exit 4; }
  else
    log "warning: missing $APP_DIR/data/$name (recorded as absent)"
  fi
done

# --- .env / sessions / logs --------------------------------------------------
if [[ -f "$APP_DIR/.env" ]]; then
  cp -p "$APP_DIR/.env" "$tmp/.env"
else
  log "warning: missing $APP_DIR/.env (recorded as absent)"
fi
if [[ -d "$APP_DIR/data/sessions" ]]; then
  mkdir -p "$tmp/data/sessions"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a "$APP_DIR/data/sessions/" "$tmp/data/sessions/"
  else
    cp -a "$APP_DIR/data/sessions/." "$tmp/data/sessions/"
  fi
else
  log "warning: missing $APP_DIR/data/sessions (recorded as absent)"
fi
if [[ -d "$APP_DIR/logs" ]]; then
  mkdir -p "$tmp/logs"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a "$APP_DIR/logs/" "$tmp/logs/" || log "warning: logs copy partial"
  else
    cp -a "$APP_DIR/logs/." "$tmp/logs/" || log "warning: logs copy partial"
  fi
fi

# --- manifest (JSON inside the archive) --------------------------------------
instance_id="${INSTANCE_ID:-}"
if [[ -z "$instance_id" && -f "$APP_DIR/.instance_id" ]]; then
  instance_id="$(cut -d' ' -f1 <"$APP_DIR/.instance_id")"
fi
instance_name="${INSTANCE_NAME:-${TENANT_NAME:-$(hostname 2>/dev/null || echo unknown)}}"
duration_so_far="$((SECONDS - start_sec))"
"$PYTHON_BIN" "$BACKUP_LIB" manifest-build \
  --app-dir "$APP_DIR" --stage "$tmp" \
  --instance-id "${instance_id:-unknown}" --instance-name "${instance_name:-unknown}" \
  --created-at "$created_iso" --duration-sec "$duration_so_far" >&2 \
  || { log "ERROR: manifest build failed"; exit 4; }

# --- archive + sidecar --------------------------------------------------------
tar -C "$tmp" -czf "$archive" .
chmod 600 "$archive"
"$PYTHON_BIN" "$BACKUP_LIB" sidecar-write --archive "$archive" --stage "$tmp" >/dev/null \
  || { log "ERROR: sidecar write failed"; exit 4; }
chmod 600 "$archive.sha256"

# --- retention (count + age; the newest backup is never deleted) --------------
if victims="$("$PYTHON_BIN" "$BACKUP_LIB" retention-plan \
    --dir "$BACKUP_DIR" --keep "$RETENTION_COUNT" --max-age-days "$RETENTION_DAYS" 2>/dev/null)"; then
  for victim in $victims; do
    [[ -n "$victim" ]] || continue
    rm -f "$BACKUP_DIR/$victim" "$BACKUP_DIR/$victim.sha256" "$BACKUP_DIR/$victim.enc" \
      && log "retention: removed $victim" \
      || log "warning: retention could not remove $victim"
  done
else
  log "warning: retention plan failed; keeping everything"
fi

# --- cron marker (UTC; watchdog/instance_status independently derive
# --- backup_age_h from archive mtime — the marker is the human/cron-readable
# --- twin of the same event, written only on success) --------------------------
echo "$created_iso $archive" > "$BACKUP_DIR/.last_backup_ok"
chmod 600 "$BACKUP_DIR/.last_backup_ok"

# --- optional encrypted off-host copy ------------------------------------------
if [[ "$ENCRYPT" == "1" ]]; then
  openssl enc -aes-256-cbc -pbkdf2 -salt \
    -in "$archive" -out "$archive.enc" -pass "file:$BACKUP_PASSPHRASE_FILE" \
    || { log "ERROR: openssl encryption failed"; exit 4; }
  chmod 600 "$archive.enc"
  log "encrypted copy: $archive.enc"
fi

duration_total="$((SECONDS - start_sec))"
log "BACKUP OK: $archive (${duration_total}s)"
echo "$archive"
