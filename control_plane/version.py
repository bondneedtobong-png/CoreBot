"""Release version endpoint for the Control Plane (task 06).

``GET /version`` reports the deployed release artifact WITHOUT secrets:
version / git SHA / Python requirement / supported Ubuntu / release date /
code checksum. Sits next to ``control_plane/health.py``; the ``/health*``
contracts are intentionally left untouched.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from fastapi import APIRouter

router = APIRouter()

APP_ROOT = Path(__file__).resolve().parent.parent

#: Allowlisted manifest keys. Nothing else is ever served here.
VERSION_FIELDS = (
    "version",
    "sha",
    "python_requires",
    "ubuntu",
    "released_at",
    "code_checksum",
)

_UNKNOWN = "unknown"


def manifest_path() -> Path:
    override = (os.getenv("COREBOT_RELEASE_FILE", "") or "").strip()
    if override:
        return Path(override)
    return APP_ROOT / "RELEASE.json"


def _git_sha_short() -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(APP_ROOT), "rev-parse", "--short=12", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return _UNKNOWN
    sha = (out.stdout or "").strip()
    return sha if sha and all(c in "0123456789abcdefABCDEF" for c in sha) else _UNKNOWN


def _product_version() -> str:
    try:
        text = (APP_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return _UNKNOWN
    return text.split()[0] if text else _UNKNOWN


def get_release_info() -> dict:
    """Return the allowlisted release dict (never secrets)."""
    try:
        raw = json.loads(manifest_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    info = {key: raw[key] for key in VERSION_FIELDS if key in raw}
    info.setdefault("version", _product_version())
    sha = str(info.get("sha", "") or "").strip()
    if not sha or sha == _UNKNOWN:
        git_sha = _git_sha_short()
        info["sha"] = git_sha if git_sha != _UNKNOWN else _UNKNOWN
    info.setdefault("python_requires", ">=3.11")
    info.setdefault("ubuntu", ["22.04", "24.04"])
    for key in ("released_at", "code_checksum"):
        info.setdefault(key, _UNKNOWN)
    return info


@router.get("/version")
def version() -> dict:
    """Deployed release identity (no secrets, no auth required on loopback)."""
    return get_release_info()
