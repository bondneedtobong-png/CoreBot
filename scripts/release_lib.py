"""Release artifact helpers (task 06).

Single source of truth for the CoreBot release manifest (``RELEASE.json``),
the deployed-SHA record (``.deployed_sha``) and the update/rollback decision
logic shared by ``scripts/make_release.py`` and the pytest suite.

Stdlib only: this module must be importable with the system ``python3`` on a
fresh VPS (no venv) and must NEVER touch secrets (``.env`` is never read).

Release artifact format (one line):
``<tag> (<sha12>) + RELEASE.json{version,sha,python_requires,ubuntu,released_at,code_checksum}``

Update order (one line):
``preflight -> backup -> stage -> dependencies -> stop/swap(cp,bot) -> start(cp,bot) -> readiness-gate -> success | auto-rollback``
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

MANIFEST_FILENAME = "RELEASE.json"
DEPLOYED_SHA_FILENAME = ".deployed_sha"
VERSION_FILENAME = "VERSION"

PYTHON_REQUIRES = ">=3.11"
UBUNTU_SUPPORTED = ["22.04", "24.04"]

MANIFEST_FIELDS = (
    "version",
    "sha",
    "python_requires",
    "ubuntu",
    "released_at",
    "code_checksum",
)

#: Fields allowed in human-readable status output. Anything else (even if a
#: future manifest ever carried it) is never printed -> secrets cannot leak.
STATUS_FIELDS = (
    "version",
    "sha",
    "python_requires",
    "ubuntu",
    "released_at",
    "code_checksum",
)

#: Persistent instance paths. Update/rollback must never overwrite or delete
#: these except by restoring them verbatim from the pre-update backup.
PERSISTENT_PATHS = (".env", "data", "logs", "data/sessions")

#: Canonical update step order. scripts/update_corebot.sh implements exactly
#: this order; tests/test_release_workflow.py asserts it statically.
UPDATE_ORDER = (
    "preflight",
    "backup",
    "stage",
    "dependencies",
    "stop-swap",
    "start",
    "readiness-gate",
    "success-or-rollback",
)

#: Restart order: Control Plane first, bot second. State "new bot + old CP"
#: is forbidden (checked via /version before success).
RESTART_ORDER = ("corebot-cp.service", "corebot.service")

#: Directories never included in the code checksum (generated / runtime data).
CHECKSUM_SKIP_DIRS = frozenset(
    {
        ".git",
        "data",
        "logs",
        "__pycache__",
        ".venv",
        "venv",
        ".pytest_cache",
        ".mypy_cache",
        "node_modules",
    }
)

#: Files never included in the code checksum (generated / local records).
CHECKSUM_SKIP_FILES = frozenset(
    {
        MANIFEST_FILENAME,
        DEPLOYED_SHA_FILENAME,
        ".env",
    }
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_code_checksum(root: str | Path) -> str:
    """sha256 over all tracked-code files under *root* (deterministic).

    Walks the directory, skips ``CHECKSUM_SKIP_DIRS`` / ``CHECKSUM_SKIP_FILES``
    / ``*.pyc`` / ``*.log``, hashes ``relpath + NUL + content`` for each file
    in sorted order. The manifest itself is excluded so a manifest can be
    generated and then verified against the same tree.
    """
    root = Path(root)
    entries: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in CHECKSUM_SKIP_DIRS for part in rel.parts):
            continue
        if rel.name in CHECKSUM_SKIP_FILES:
            continue
        if path.suffix in (".pyc", ".log"):
            continue
        entries.append(rel)
    digest = hashlib.sha256()
    for rel in sorted(entries):
        digest.update(rel.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update((root / rel).read_bytes())
    return digest.hexdigest()


def git_sha(root: str | Path) -> str | None:
    """Full HEAD SHA of the git checkout at *root*, or None (best effort)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = (out.stdout or "").strip()
    return (
        sha
        if len(sha) == 40 and all(c in "0123456789abcdef" for c in sha.lower())
        else None
    )


def read_version(root: str | Path) -> str:
    """Product version from the VERSION file (fallback 'unknown')."""
    try:
        text = (Path(root) / VERSION_FILENAME).read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"
    return text.split()[0] if text else "unknown"


def build_manifest(
    root: str | Path,
    sha: str | None = None,
    released_at: str | None = None,
) -> dict:
    """Build the manifest dict for the code tree at *root*."""
    resolved = (sha or "").strip() or git_sha(root) or "unknown"
    return {
        "version": read_version(root),
        "sha": resolved,
        "python_requires": PYTHON_REQUIRES,
        "ubuntu": list(UBUNTU_SUPPORTED),
        "released_at": released_at or _utc_now_iso(),
        "code_checksum": compute_code_checksum(root),
    }


def write_manifest(root: str | Path, path: str | Path, sha: str | None = None) -> dict:
    """Build and write the manifest; returns the manifest dict."""
    manifest = build_manifest(root, sha=sha)
    dest = Path(path)
    dest.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def load_manifest(path: str | Path) -> dict:
    """Parse a manifest file (raises on missing/invalid JSON)."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def verify_manifest(manifest: dict, root: str | Path | None = None) -> list[str]:
    """Return a list of problems (empty = valid).

    If *root* is given, the code checksum is recomputed and compared.
    """
    errors: list[str] = []
    if not isinstance(manifest, dict):
        return ["manifest: not a JSON object"]
    for field in MANIFEST_FIELDS:
        if field not in manifest:
            errors.append(f"manifest: missing field {field!r}")
    sha = str(manifest.get("sha", ""))
    if sha != "unknown" and not (
        len(sha) == 40 and all(c in "0123456789abcdefABCDEF" for c in sha)
    ):
        errors.append("manifest: 'sha' must be a 40-hex git SHA or 'unknown'")
    if manifest.get("python_requires") != PYTHON_REQUIRES:
        errors.append(f"manifest: 'python_requires' must be {PYTHON_REQUIRES!r}")
    ubuntu = manifest.get("ubuntu")
    if (
        not isinstance(ubuntu, list)
        or not ubuntu
        or any(not isinstance(v, str) or v not in UBUNTU_SUPPORTED for v in ubuntu)
    ):
        errors.append(
            f"manifest: 'ubuntu' must be a non-empty subset of {UBUNTU_SUPPORTED}"
        )
    released_at = str(manifest.get("released_at", ""))
    try:
        datetime.strptime(released_at, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        errors.append("manifest: 'released_at' must be UTC '%Y%m%dT%H%M%SZ'-style ISO")
    checksum = str(manifest.get("code_checksum", ""))
    if len(checksum) != 64 or any(
        c not in "0123456789abcdef" for c in checksum.lower()
    ):
        errors.append("manifest: 'code_checksum' must be a 64-hex sha256")
    if not errors and root is not None and sha != "unknown":
        actual = compute_code_checksum(root)
        if actual != checksum.lower():
            errors.append("manifest: 'code_checksum' does not match the code tree")
    return errors


def normalize_sha(sha: str | None) -> str:
    return (sha or "").strip().lower()


def is_noop(deployed_sha: str | None, target_sha: str | None) -> bool:
    """True when the target SHA is already deployed (repeat run = no-op)."""
    deployed, target = normalize_sha(deployed_sha), normalize_sha(target_sha)
    return bool(deployed) and bool(target) and deployed == target


def decide_post_readiness(ready_ok: bool, version_ok: bool) -> str:
    """'success' only when readiness AND version parity hold, else 'rollback'."""
    return "success" if (ready_ok and version_ok) else "rollback"


def short_sha(sha: str | None) -> str:
    sha = normalize_sha(sha)
    return sha[:12] if sha else "unknown"


def render_status(manifest: dict | None, live: dict | None) -> str:
    """Render status using allowlisted fields only (never secrets)."""
    lines: list[str] = []
    if manifest:
        lines.append("manifest:")
        for field in STATUS_FIELDS:
            value = manifest.get(field, "unknown")
            if field == "ubuntu" and isinstance(value, list):
                value = ",".join(value)
            lines.append(f"  {field}={value if value != '' else 'unknown'}")
    else:
        lines.append("manifest: missing (version unknown, instance unsupported)")
    if live:
        lines.append("live /version:")
        for field in STATUS_FIELDS:
            if field in live:
                value = live[field]
                if field == "ubuntu" and isinstance(value, list):
                    value = ",".join(value)
                lines.append(f"  {field}={value}")
        m_sha, l_sha = (
            normalize_sha((manifest or {}).get("sha")),
            normalize_sha(live.get("sha")),
        )
        if m_sha and l_sha and m_sha != "unknown" and l_sha != "unknown":
            lines.append(
                f"match={'OK' if l_sha.startswith(m_sha[:12]) or m_sha.startswith(l_sha[:12]) else 'MISMATCH'}"
            )
    else:
        lines.append("live /version: UNAVAILABLE")
    return "\n".join(lines) + "\n"
