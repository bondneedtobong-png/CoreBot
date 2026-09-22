#!/usr/bin/env bash
# Show deployed CoreBot release identity without secrets (task 06).
# Reads the RELEASE.json manifest plus live GET /version and compares SHAs.
# Only allowlisted fields (version/sha/python/ubuntu/date/checksum) are ever
# printed; secrets are never read (no .env access) and cannot leak.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/corebot/app}"
MANIFEST="${MANIFEST:-$APP_DIR/RELEASE.json}"
HEALTH_BASE="${HEALTH_BASE:-http://127.0.0.1:8081}"
CURL="${CURL:-curl}"
CURL_TIMEOUT="${CURL_TIMEOUT:-10}"
NO_LIVE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-dir) APP_DIR="${2:?}"; MANIFEST="$APP_DIR/RELEASE.json"; shift 2 ;;
    --manifest) MANIFEST="${2:?}"; shift 2 ;;
    --health-base) HEALTH_BASE="${2:?}"; shift 2 ;;
    --no-live) NO_LIVE=1; shift ;;
    -h|--help) echo "Usage: release_status.sh [--app-dir PATH] [--manifest PATH] [--health-base URL] [--no-live]"; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

PYBIN="python3"
command -v "$PYBIN" >/dev/null || { echo "ERROR: python3 not found" >&2; exit 2; }

if [[ -f "$MANIFEST" ]]; then
  "$PYBIN" - "$MANIFEST" <<'EOF'
import json, sys
m = json.load(open(sys.argv[1], encoding="utf-8"))
print("manifest:")
for k in ("version", "sha", "python_requires", "ubuntu", "released_at", "code_checksum"):
    v = m.get(k, "unknown")
    if isinstance(v, list):
        v = ",".join(v)
    print(f"  {k}={v if v != '' else 'unknown'}")
EOF
else
  echo "manifest: missing ($MANIFEST) - version unknown, instance unsupported"
fi

if [[ "$NO_LIVE" == "1" ]]; then exit 0; fi

LIVE_JSON="$("$CURL" --fail --silent --max-time "$CURL_TIMEOUT" "$HEALTH_BASE/version" 2>/dev/null || true)"
if [[ -z "$LIVE_JSON" ]]; then
  echo "live /version: UNAVAILABLE ($HEALTH_BASE)"
  exit 0
fi
MANIFEST_PATH="$MANIFEST" "$PYBIN" - "$LIVE_JSON" <<'EOF'
import json, os, sys
live = json.loads(sys.argv[1])
print("live /version:")
for k in ("version", "sha", "python_requires", "ubuntu", "released_at", "code_checksum"):
    if k in live:
        v = live[k]
        if isinstance(v, list):
            v = ",".join(v)
        print(f"  {k}={v}")
try:
    m = json.load(open(os.environ["MANIFEST_PATH"], encoding="utf-8"))
    ms, ls = str(m.get("sha", "")).lower(), str(live.get("sha", "")).lower()
    if ms and ls and ms != "unknown" and ls != "unknown":
        print("match=" + ("OK" if ls.startswith(ms[:12]) or ms.startswith(ls[:12]) else "MISMATCH"))
except (OSError, ValueError):
    pass
EOF
