#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/corebot/app}"
VENV_PY="${VENV_PY:-/opt/corebot/venv/bin/python}"
SERVICE="${SERVICE:-corebot.service}"
BRANCH="${BRANCH:-main}"

echo "== CoreBot update =="
echo "APP_DIR=$APP_DIR"
echo "BRANCH=$BRANCH"
echo "SERVICE=$SERVICE"

if [[ ! -d "$APP_DIR/.git" ]]; then
  echo "ERROR: git repo not found: $APP_DIR"
  exit 1
fi

if [[ ! -x "$VENV_PY" ]]; then
  echo "ERROR: python not found in venv: $VENV_PY"
  exit 1
fi

cd "$APP_DIR"

echo "-> Ensuring safe.directory (fix dubious ownership)"
git config --global --add safe.directory "$APP_DIR" || true

echo "-> Updating code from git"
git fetch origin
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"

echo "-> Installing/updating dependencies"
"$VENV_PY" -m pip install -r requirements.txt

echo "-> Optional pip cache cleanup"
"$VENV_PY" -m pip cache purge || true

echo "-> Restarting service"
sudo systemctl restart "$SERVICE"

echo "-> Service status"
sudo systemctl status "$SERVICE" --no-pager -l

echo "-> Last logs"
journalctl -u "$SERVICE" -n 120 --no-pager

echo "== Update finished =="

