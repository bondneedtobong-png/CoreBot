#!/usr/bin/env bash
set -euo pipefail
APP_DIR=${APP_DIR:-/opt/corebot/app}
BACKUP_DIR=${BACKUP_DIR:-/opt/corebot/backups}
DRY_RUN=0
[[ ${1:-} == --dry-run ]] && DRY_RUN=1
[[ -d "$APP_DIR" ]] || { echo "Missing APP_DIR: $APP_DIR" >&2; exit 2; }
stamp=$(date -u +%Y%m%dT%H%M%SZ)
archive="$BACKUP_DIR/corebot-$stamp.tar.gz"
if [[ $DRY_RUN -eq 1 ]]; then
  echo "DRY RUN OK: backup $APP_DIR persistent data to $archive"
  exit 0
fi
mkdir -p "$BACKUP_DIR"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/data"
[[ -f "$APP_DIR/.env" ]] && cp -p "$APP_DIR/.env" "$tmp/.env"
[[ -d "$APP_DIR/data/sessions" ]] && rsync -a "$APP_DIR/data/sessions/" "$tmp/data/sessions/"
for name in corebot.db control_plane.db; do
  if [[ -f "$APP_DIR/data/$name" ]]; then
    sqlite3 "$APP_DIR/data/$name" ".backup '$tmp/data/$name'"
  fi
done
[[ -d "$APP_DIR/logs" ]] && rsync -a "$APP_DIR/logs/" "$tmp/logs/"
tar -C "$tmp" -czf "$archive" .
chmod 600 "$archive"
echo "$archive"
