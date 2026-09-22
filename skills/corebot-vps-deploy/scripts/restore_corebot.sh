#!/usr/bin/env bash
# Restore a CoreBot backup into an EMPTY directory (task 09).
#
# Pipeline: sidecar verify -> extract to temp -> manifest+checksum verify
# (BEFORE anything is trusted) -> live/non-empty guards -> copy -> fix
# permissions -> integrity_check both DBs -> readiness plan + summary.
#
# Live-instance protection: restore REFUSES a target that looks like a live
# instance (.env exists, data/ exists, or *.db present) — ALWAYS, even with
# --allow-nonempty. --allow-nonempty only tolerates stray non-live files
# (e.g. lost+found on a fresh mount); live overwrite is never allowed.
#
# Permissions after restore (INSTANCE_CONTRACT): dirs 755, files 644,
# .env 600. The archive itself stays 600 and is never modified.
#
# Summary output contains hashes/sizes/counts only — never secrets.
#
# Exit codes: 0 ok; 2 usage/config error; 3 archive/sidecar problem;
# 4 target refused (non-empty or live markers); 5 manifest/checksum
# mismatch; 6 integrity_check failure; 7 copy/permission failure.
#
# Env knobs: PYTHON_BIN (default python3), BACKUP_LIB (default
# <repo>/scripts/backup_lib.py), SYSTEMCTL (default systemctl, used only to
# detect an active live service for the refusal message).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
BACKUP_LIB="${BACKUP_LIB:-$REPO_ROOT/scripts/backup_lib.py}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"

ARCHIVE=""
TARGET=""
ALLOW_NONEMPTY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --archive) ARCHIVE="${2:?--archive needs a value}"; shift 2 ;;
    --target) TARGET="${2:?--target needs a value}"; shift 2 ;;
    --allow-nonempty) ALLOW_NONEMPTY=1; shift ;;
    -h|--help)
      echo "Usage: restore_corebot.sh --archive PATH --target EMPTY_DIR [--allow-nonempty]"
      exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$ARCHIVE" && -n "$TARGET" ]] || { echo "Usage: restore_corebot.sh --archive PATH --target EMPTY_DIR [--allow-nonempty]" >&2; exit 2; }
[[ -f "$BACKUP_LIB" ]] || { echo "ERROR: missing backup lib: $BACKUP_LIB" >&2; exit 2; }

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] restore: $*" >&2; }

# --- archive + sidecar preflight (nothing is trusted before this) ------------
[[ -f "$ARCHIVE" ]] || { log "ERROR: archive not found: $ARCHIVE"; exit 3; }
[[ -f "$ARCHIVE.sha256" ]] || { log "ERROR: sidecar missing: $ARCHIVE.sha256 (backups always ship one)"; exit 3; }
"$PYTHON_BIN" "$BACKUP_LIB" sidecar-verify --archive "$ARCHIVE" \
  || { log "ERROR: sidecar verification failed; refusing restore"; exit 3; }
log "sidecar ok: $ARCHIVE"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
tar -xzf "$ARCHIVE" -C "$work" || { log "ERROR: cannot extract $ARCHIVE"; exit 3; }
[[ -f "$work/backup_manifest.json" ]] || { log "ERROR: archive has no backup_manifest.json"; exit 5; }
"$PYTHON_BIN" "$BACKUP_LIB" manifest-verify --stage "$work" \
  || { log "ERROR: manifest/checksum verification failed; refusing restore"; exit 5; }
"$PYTHON_BIN" "$BACKUP_LIB" sidecar-verify --archive "$ARCHIVE" --stage "$work" \
  || { log "ERROR: manifest does not match sidecar; refusing restore"; exit 5; }
log "manifest+checksums ok"

# --- target guards -------------------------------------------------------------
if [[ -e "$TARGET" && ! -d "$TARGET" ]]; then
  log "ERROR: target exists and is not a directory: $TARGET"; exit 4
fi
mkdir -p "$TARGET"
# Live markers are checked FIRST and unconditionally: a live instance is
# never overwritten, --allow-nonempty does not lift this ban.
if [[ -e "$TARGET/.env" || -d "$TARGET/data" || -e "$TARGET/data/corebot.db" || -e "$TARGET/data/control_plane.db" ]]; then
  log "ERROR: target looks like a LIVE instance (.env/data present): $TARGET — live overwrite is always forbidden"
  if "$SYSTEMCTL" is-active --quiet corebot.service 2>/dev/null; then
    log "hint: corebot.service is currently active; stop it and restore into an EMPTY directory instead"
  fi
  exit 4
fi
if [[ -n "$(ls -A "$TARGET")" ]]; then
  if [[ "$ALLOW_NONEMPTY" != "1" ]]; then
    log "ERROR: target not empty: $TARGET (pass --allow-nonempty only for stray non-live files)"
    exit 4
  fi
  log "warning: target has stray non-live files; proceeding with --allow-nonempty"
fi

# --- space preflight ------------------------------------------------------------
need_bytes="$(du -sb "$work" 2>/dev/null | cut -f1)"
avail_bytes="$(df -B1 --output=avail "$TARGET" 2>/dev/null | tail -n 1 | tr -d ' ')"
if [[ "$avail_bytes" =~ ^[0-9]+$ ]] && [[ "$avail_bytes" -lt "$need_bytes" ]]; then
  log "ERROR: free ${avail_bytes}B < needed ${need_bytes}B for $TARGET"; exit 4
fi

# --- copy + permissions (INSTANCE_CONTRACT) --------------------------------------
cp -a "$work/." "$TARGET/" || { log "ERROR: copy to $TARGET failed"; exit 7; }
find "$TARGET" -type d -exec chmod 755 {} + || { log "ERROR: dir permissions failed"; exit 7; }
find "$TARGET" -type f -exec chmod 644 {} + || { log "ERROR: file permissions failed"; exit 7; }
if [[ -f "$TARGET/.env" ]]; then
  chmod 600 "$TARGET/.env"
fi
log "permissions: dirs 755, files 644, .env 600"

# --- integrity of both DBs -------------------------------------------------------
for name in corebot.db control_plane.db; do
  if [[ -f "$TARGET/data/$name" ]]; then
    "$PYTHON_BIN" "$BACKUP_LIB" integrity-check --db "$TARGET/data/$name" >/dev/null \
      || { log "ERROR: integrity_check failed for data/$name"; exit 6; }
    log "integrity_check ok: data/$name"
  else
    log "warning: restored tree has no data/$name"
  fi
done

# --- summary (no secrets) + readiness plan ----------------------------------------
"$PYTHON_BIN" "$BACKUP_LIB" summary --stage "$work"
cat >&2 <<EOF
readiness plan (run these on the target host before going live):
  chown -R corebot:corebot $TARGET
  chmod 600 $TARGET/.env
  COREBOT_ENV=production python3 -m tools.validate_config --mode production
  systemctl enable --now corebot-cp.service corebot.service   # or restore into /opt/corebot/app while stopped
  curl --fail http://127.0.0.1:8081/health/live && curl --fail http://127.0.0.1:8081/health/ready
EOF
log "RESTORE OK: $ARCHIVE -> $TARGET"
