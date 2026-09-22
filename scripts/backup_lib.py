"""Backup manifest helpers (task 09).

Single source of truth for the CoreBot data-backup manifest
(``backup_manifest.json`` inside each ``corebot-*.tar.gz`` archive) plus the
sibling ``<archive>.sha256`` sidecar, retention planning and SQLite helpers.

Stdlib only: this module must be importable with the system ``python3`` on a
fresh VPS (no venv) and must NEVER touch secrets beyond hashing files that
contain them (hashes and sizes may leave the host; file *contents* never do).

Companion to ``scripts/release_lib.py``: same style (``build_*`` /
``verify_*`` returning a list of problems, allowlisted status rendering), but
a different domain — release_lib describes *code*, this module describes
*data backups*. Nothing is duplicated: release identity is re-read from the
allowlisted ``RELEASE.json``/``VERSION``/git format, never redefined.

Backup manifest format (one line):
``corebot-backup/1{instance,release,created_at_utc,databases,files,total_bytes,duration_sec,tool}``
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

#: Manifest envelope version. Bump only with a documented migration.
BACKUP_FORMAT = "corebot-backup/1"

#: File name of the manifest inside the tar.gz archive.
MANIFEST_NAME = "backup_manifest.json"

#: Suffix of the checksum sidecar next to the archive (``<archive>.sha256``).
SIDECAR_SUFFIX = ".sha256"

#: Files every backup is expected to contain (SLO minimum). Missing entries
#: are tolerated (fresh install may lack a DB) but recorded explicitly.
EXPECTED_FILES = (".env", "data/corebot.db", "data/control_plane.db")

#: Retention defaults (SLO RPO <= 24 h via daily backups; numbers fixed here
#: and mirrored in backup_corebot.sh env knobs of the same names).
RETENTION_COUNT = 7
RETENTION_DAYS = 30

#: Preflight disk rule: required_free = staged_bytes * SPACE_MULTIPLIER +
#: SPACE_RESERVE_MB (fixed numbers, mirrored in backup_corebot.sh).
SPACE_MULTIPLIER = 2
SPACE_RESERVE_MB = 256

#: Backup-age alert threshold, hours (SLO #6; mirrors
#: control_plane.services.snapshot.BACKUP_MAX_AGE_HOURS, re-stated here so
#: this module stays stdlib-only and importable without the CP dependency
#: tree — never diverged silently: tests assert equality).
BACKUP_MAX_AGE_HOURS = 26.0

#: Allowlisted release keys copied from RELEASE.json (same allowlist idea as
#: control_plane/version.py VERSION_FIELDS; secrets can never appear here
#: because the source manifest never carries them).
RELEASE_FIELDS = (
    "version",
    "sha",
    "python_requires",
    "ubuntu",
    "released_at",
    "code_checksum",
)

TOOL_NAME = "backup_corebot.sh (task 09)"


# ---------------------------------------------------------------------------
# Small primitives
# ---------------------------------------------------------------------------


def utc_stamp(when: datetime | None = None) -> str:
    """Compact UTC stamp for archive names (``%Y%m%dT%H%M%SZ``)."""
    moment = when or datetime.now(timezone.utc)
    return moment.strftime("%Y%m%dT%H%M%SZ")


def utc_iso(when: datetime | None = None) -> str:
    """ISO-8601 UTC (``%Y-%m-%dT%H:%M:%SZ``)."""
    moment = when or datetime.now(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: str | Path) -> str:
    """Hex sha256 of a file (streamed, works for large DBs)."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Release / instance identity (re-reads the task-06 format, never redefines)
# ---------------------------------------------------------------------------


def _git_sha(app_dir: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(app_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    sha = (out.stdout or "").strip()
    if len(sha) == 40 and all(c in "0123456789abcdefABCDEF" for c in sha):
        return sha.lower()
    return "unknown"


def read_release_info(app_dir: str | Path) -> dict:
    """Allowlisted release identity for the manifest (never secrets)."""
    app_dir = Path(app_dir)
    info: dict = {}
    try:
        raw = json.loads((app_dir / "RELEASE.json").read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            info = {k: raw[k] for k in RELEASE_FIELDS if k in raw}
    except (OSError, ValueError):
        info = {}
    if not info.get("version"):
        try:
            text = (app_dir / "VERSION").read_text(encoding="utf-8").strip()
            info["version"] = text.split()[0] if text else "unknown"
        except OSError:
            info["version"] = "unknown"
    if not info.get("sha") or info.get("sha") == "unknown":
        info["sha"] = _git_sha(app_dir)
    info.setdefault("python_requires", ">=3.11")
    info.setdefault("ubuntu", ["22.04", "24.04"])
    info.setdefault("released_at", "unknown")
    info.setdefault("code_checksum", "unknown")
    return info


def read_instance_identity(app_dir: str | Path) -> dict:
    """Instance id/name for the manifest (identifiers only, no secrets)."""
    app_dir = Path(app_dir)
    instance_id = (os.getenv("INSTANCE_ID", "") or "").strip()
    if not instance_id:
        try:
            instance_id = (app_dir / ".instance_id").read_text(encoding="utf-8").strip().split()[0]
        except (OSError, IndexError):
            instance_id = "unknown"
    name = (
        (os.getenv("INSTANCE_NAME", "") or "").strip()
        or (os.getenv("TENANT_NAME", "") or "").strip()
    )
    if not name:
        try:
            name = socket.gethostname()
        except OSError:
            name = "unknown"
    return {"id": instance_id or "unknown", "name": name or "unknown"}


# ---------------------------------------------------------------------------
# SQLite helpers (consistent backup + schema identity + integrity)
# ---------------------------------------------------------------------------


def sqlite_backup(src: str | Path, dst: str | Path) -> None:
    """Consistent online backup via the sqlite3 backup API (WAL-safe).

    Same contract as ``sqlite3 <db> ".backup <dst>"`` used by
    backup_corebot.sh when the CLI exists: never a blind copy of a live
    ``.db``/``-wal`` pair, readers/writers keep running.
    """
    src, dst = Path(src), Path(dst)
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30.0)
    try:
        dst_conn = sqlite3.connect(str(dst), timeout=30.0)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def db_identity(db_path: str | Path) -> dict:
    """Schema identifier: ``user_version`` + hash of the table DDL set.

    ``tables_hash`` = sha256 over the sorted ``"name:NUL:sql"`` lines of
    ``sqlite_master`` (``type='table'`` excluding ``sqlite_%`` internals), so
    any DDL drift changes the fingerprint while row content does not.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30.0)
    try:
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        ).fetchall()
    finally:
        conn.close()
    digest = hashlib.sha256()
    tables: list[str] = []
    for name, sql in rows:
        tables.append(str(name))
        digest.update(str(name).encode("utf-8"))
        digest.update(b"\0")
        digest.update((sql or "").encode("utf-8"))
        digest.update(b"\n")
    return {
        "user_version": user_version,
        "tables_hash": digest.hexdigest(),
        "tables": sorted(tables),
    }


def db_integrity_check(db_path: str | Path) -> str:
    """Run ``PRAGMA integrity_check``; returns ``"ok"`` or raises."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=60.0)
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    finally:
        conn.close()
    if len(rows) == 1 and str(rows[0][0]).lower() == "ok":
        return "ok"
    raise RuntimeError(f"integrity_check failed for {db_path}: {rows[:5]!r}")


# ---------------------------------------------------------------------------
# Manifest build / verify
# ---------------------------------------------------------------------------


def scan_stage(stage: str | Path) -> tuple[list[dict], dict]:
    """Hash every file in *stage* (except the manifest itself).

    Returns ``(files, databases)`` where ``files`` is a sorted list of
    ``{path, size, sha256}`` (POSIX relative paths) and ``databases`` maps
    each staged ``*.db`` path to its :func:`db_identity`.
    """
    stage = Path(stage)
    files: list[dict] = []
    databases: dict[str, dict] = {}
    for path in sorted(stage.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(stage).as_posix()
        if rel == MANIFEST_NAME:
            continue
        files.append({"path": rel, "size": path.stat().st_size, "sha256": sha256_file(path)})
        if path.suffix == ".db":
            databases[rel] = db_identity(path)
    files.sort(key=lambda entry: entry["path"])
    return files, databases


def build_manifest(
    *,
    app_dir: str | Path,
    files: list[dict],
    databases: dict[str, dict],
    created_at: str,
    duration_sec: float,
    instance: dict | None = None,
    release: dict | None = None,
) -> dict:
    """Assemble the manifest dict (JSON-serializable, no secrets)."""
    total = sum(int(entry["size"]) for entry in files)
    return {
        "format": BACKUP_FORMAT,
        "instance": instance or read_instance_identity(app_dir),
        "release": release or read_release_info(app_dir),
        "created_at_utc": created_at,
        "databases": databases,
        "files": files,
        "total_bytes": total,
        "duration_sec": round(float(duration_sec), 1),
        "tool": TOOL_NAME,
        "expected": list(EXPECTED_FILES),
    }


def write_manifest(manifest: dict, stage: str | Path) -> Path:
    """Write the manifest into the staging dir; returns its path."""
    dest = Path(stage) / MANIFEST_NAME
    dest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return dest


def load_manifest(stage: str | Path) -> dict:
    """Parse the staged manifest (raises on missing/invalid JSON)."""
    return json.loads((Path(stage) / MANIFEST_NAME).read_text(encoding="utf-8"))


def verify_manifest(manifest: dict, stage: str | Path) -> list[str]:
    """Check manifest shape and per-file sha256/size against *stage*.

    Returns a list of problems (empty = valid). Restore refuses the archive
    whenever this list is non-empty.
    """
    errors: list[str] = []
    if not isinstance(manifest, dict):
        return ["manifest: not a JSON object"]
    if manifest.get("format") != BACKUP_FORMAT:
        errors.append(f"manifest: 'format' must be {BACKUP_FORMAT!r}")
    for field in ("instance", "release", "created_at_utc", "databases", "files"):
        if field not in manifest:
            errors.append(f"manifest: missing field {field!r}")
    try:
        datetime.strptime(str(manifest.get("created_at_utc", "")), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        errors.append("manifest: 'created_at_utc' must be UTC '%Y-%m-%dT%H:%M:%SZ'")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        errors.append("manifest: 'files' must be a non-empty list")
        return errors
    seen: set[str] = set()
    stage = Path(stage)
    for entry in files:
        if not isinstance(entry, dict):
            errors.append("manifest: file entry must be an object")
            continue
        rel = str(entry.get("path", ""))
        want_sha = str(entry.get("sha256", ""))
        want_size = entry.get("size")
        if not rel or rel in seen or rel.startswith("/") or ".." in Path(rel).parts:
            errors.append(f"manifest: bad file path {rel!r}")
            continue
        seen.add(rel)
        if len(want_sha) != 64 or any(c not in "0123456789abcdef" for c in want_sha.lower()):
            errors.append(f"manifest: bad sha256 for {rel!r}")
            continue
        if not isinstance(want_size, int) or want_size < 0:
            errors.append(f"manifest: bad size for {rel!r}")
            continue
        candidate = stage / rel
        if not candidate.is_file():
            errors.append(f"manifest: missing file {rel!r}")
            continue
        if candidate.stat().st_size != want_size:
            errors.append(f"manifest: size mismatch for {rel!r}")
            continue
        if sha256_file(candidate) != want_sha.lower():
            errors.append(f"manifest: checksum mismatch for {rel!r}")
    databases = manifest.get("databases")
    if not isinstance(databases, dict):
        errors.append("manifest: 'databases' must be an object")
    else:
        for rel, ident in databases.items():
            if not isinstance(ident, dict) or "tables_hash" not in ident or "user_version" not in ident:
                errors.append(f"manifest: bad db identity for {rel!r}")
    return errors


# ---------------------------------------------------------------------------
# Sidecar (<archive>.sha256) next to the archive
# ---------------------------------------------------------------------------


def sidecar_path(archive: str | Path) -> Path:
    """Sidecar path for an archive (``<archive>.sha256``)."""
    return Path(f"{archive}{SIDECAR_SUFFIX}")


def write_sidecar(archive: str | Path, manifest: dict) -> Path:
    """Write the sidecar: archive hash first, then one line per payload file.

    ``sha256sum -c`` compatible format (``<sha><two spaces><name>``); payload
    names are manifest-relative paths, the first line names the archive file
    itself so a truncated download fails fast before extraction.
    """
    archive = Path(archive)
    lines = [f"{sha256_file(archive)}  {archive.name}"]
    for entry in manifest.get("files", []):
        lines.append(f"{entry['sha256']}  {entry['path']}")
    dest = sidecar_path(archive)
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dest


def read_sidecar(archive: str | Path) -> tuple[list[str], dict[str, str]]:
    """Parse the sidecar; returns ``(errors, {name: sha256})``."""
    archive = Path(archive)
    dest = sidecar_path(archive)
    try:
        lines = dest.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return ([f"sidecar: cannot read {dest.name}: {exc}"], {})
    entries: dict[str, str] = {}
    errors: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2 or len(parts[0]) != 64:
            errors.append(f"sidecar: malformed line {line!r}")
            continue
        entries[parts[1]] = parts[0].lower()
    if not entries:
        errors.append("sidecar: empty")
    return errors, entries


def verify_sidecar(archive: str | Path, stage: str | Path | None = None) -> list[str]:
    """Verify the archive hash via the sidecar; optionally cross-check the
    staged manifest payload hashes against the sidecar table."""
    archive = Path(archive)
    errors, entries = read_sidecar(archive)
    if errors and not entries:
        return errors
    want = entries.get(archive.name)
    if want is None:
        errors.append(f"sidecar: no entry for archive {archive.name!r}")
    elif sha256_file(archive) != want:
        errors.append(f"sidecar: archive checksum mismatch for {archive.name!r}")
    if stage is not None:
        try:
            manifest = load_manifest(stage)
        except (OSError, ValueError) as exc:
            return errors + [f"manifest: cannot load for cross-check: {exc}"]
        for entry in manifest.get("files", []):
            rel = str(entry.get("path", ""))
            want_file = entries.get(rel)
            if want_file is None:
                errors.append(f"sidecar: no entry for payload {rel!r}")
            elif str(entry.get("sha256", "")).lower() != want_file:
                errors.append(f"sidecar: payload checksum mismatch for {rel!r}")
    return errors


# ---------------------------------------------------------------------------
# Retention (pure: never deletes the newest successful backup)
# ---------------------------------------------------------------------------


def select_retention_victims(
    entries: list[tuple[str, float]],
    *,
    keep_count: int = RETENTION_COUNT,
    max_age_days: int = RETENTION_DAYS,
    now: float | None = None,
) -> list[str]:
    """Choose archive names to delete; the newest entry is never a victim.

    ``entries`` = ``[(archive_name, mtime_epoch)]``. Victims are archives
    older than *max_age_days* plus the oldest beyond *keep_count* newest —
    except that a lone last backup is always kept (even when stale/over
    count), so retention can never leave the directory with zero backups.
    """
    import time as _time

    if len(entries) <= 1:
        return []
    moment = now if now is not None else _time.time()
    ordered = sorted(entries, key=lambda item: (item[1], item[0]))
    newest = ordered[-1][0]
    victims: list[str] = []
    cutoff = moment - max_age_days * 86400.0
    for name, mtime in ordered:
        if name != newest and mtime < cutoff:
            victims.append(name)
    survivors = [name for name, _ in ordered if name not in victims]
    while len(survivors) > keep_count:
        oldest = next(name for name, _ in ordered if name in survivors and name != newest)
        victims.append(oldest)
        survivors.remove(oldest)
    return sorted(victims)


# ---------------------------------------------------------------------------
# Human summary (allowlisted fields only — never file contents / secrets)
# ---------------------------------------------------------------------------


def render_summary(manifest: dict) -> str:
    """One-screen restore/backup summary without secrets."""
    lines = [
        f"format={manifest.get('format', 'unknown')}",
        f"created_at_utc={manifest.get('created_at_utc', 'unknown')}",
    ]
    instance = manifest.get("instance") or {}
    lines.append(f"instance={instance.get('id', 'unknown')} ({instance.get('name', 'unknown')})")
    release = manifest.get("release") or {}
    lines.append(f"release={release.get('version', 'unknown')} sha={release.get('sha', 'unknown')}")
    databases = manifest.get("databases") or {}
    for rel in sorted(databases):
        ident = databases[rel] or {}
        lines.append(
            f"db {rel}: user_version={ident.get('user_version', '?')} "
            f"tables_hash={str(ident.get('tables_hash', '?'))[:12]}"
        )
    lines.append(f"files={len(manifest.get('files', []))} total_bytes={manifest.get('total_bytes', '?')}")
    lines.append(f"duration_sec={manifest.get('duration_sec', '?')}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI used by backup_corebot.sh / restore_corebot.sh (scripts stay thin)
# ---------------------------------------------------------------------------


def _cmd_manifest_build(args: argparse.Namespace) -> int:
    stage = Path(args.stage)
    files, databases = scan_stage(stage)
    manifest = build_manifest(
        app_dir=args.app_dir,
        files=files,
        databases=databases,
        created_at=args.created_at,
        duration_sec=float(args.duration_sec),
        instance={"id": args.instance_id, "name": args.instance_name},
    )
    problems = verify_manifest(manifest, stage)
    if problems:
        print("manifest self-check failed:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    write_manifest(manifest, stage)
    print(json.dumps({"files": len(files), "total_bytes": manifest["total_bytes"]}))
    return 0


def _cmd_manifest_verify(args: argparse.Namespace) -> int:
    try:
        manifest = load_manifest(args.stage)
    except (OSError, ValueError) as exc:
        print(f"manifest: cannot load: {exc}", file=sys.stderr)
        return 1
    problems = verify_manifest(manifest, Path(args.stage))
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    return 1 if problems else 0


def _cmd_sidecar_write(args: argparse.Namespace) -> int:
    try:
        manifest = load_manifest(args.stage)
    except (OSError, ValueError) as exc:
        print(f"sidecar: cannot load manifest: {exc}", file=sys.stderr)
        return 1
    dest = write_sidecar(args.archive, manifest)
    print(str(dest))
    return 0


def _cmd_sidecar_verify(args: argparse.Namespace) -> int:
    problems = verify_sidecar(args.archive, args.stage)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    return 1 if problems else 0


def _cmd_sqlite_backup(args: argparse.Namespace) -> int:
    try:
        sqlite_backup(args.src, args.dst)
    except Exception as exc:  # noqa: BLE001 - surfaced to the shell caller
        print(f"sqlite-backup failed: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_integrity_check(args: argparse.Namespace) -> int:
    try:
        print(db_integrity_check(args.db))
    except Exception as exc:  # noqa: BLE001 - surfaced to the shell caller
        print(f"integrity_check failed: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_summary(args: argparse.Namespace) -> int:
    try:
        manifest = load_manifest(args.stage)
    except (OSError, ValueError) as exc:
        print(f"summary: cannot load manifest: {exc}", file=sys.stderr)
        return 1
    print(render_summary(manifest), end="")
    return 0


def _cmd_retention_plan(args: argparse.Namespace) -> int:
    import time as _time

    backup_dir = Path(args.dir)
    entries: list[tuple[str, float]] = []
    try:
        names = sorted(p.name for p in backup_dir.glob("corebot-*.tar.gz"))
    except OSError as exc:
        print(f"retention: cannot list {backup_dir}: {exc}", file=sys.stderr)
        return 1
    for name in names:
        try:
            entries.append((name, (backup_dir / name).stat().st_mtime))
        except OSError:
            continue
    victims = select_retention_victims(
        entries,
        keep_count=args.keep,
        max_age_days=args.max_age_days,
        now=_time.time(),
    )
    for name in victims:
        print(name)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="backup_lib", description="CoreBot backup manifest helpers.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    build = sub.add_parser("manifest-build", help="scan stage dir and write backup_manifest.json")
    build.add_argument("--app-dir", required=True)
    build.add_argument("--stage", required=True)
    build.add_argument("--instance-id", default="unknown")
    build.add_argument("--instance-name", default="unknown")
    build.add_argument("--created-at", required=True)
    build.add_argument("--duration-sec", default="0")
    build.set_defaults(func=_cmd_manifest_build)
    verify = sub.add_parser("manifest-verify", help="verify staged manifest + checksums")
    verify.add_argument("--stage", required=True)
    verify.set_defaults(func=_cmd_manifest_verify)
    sidecar_write = sub.add_parser("sidecar-write", help="write <archive>.sha256")
    sidecar_write.add_argument("--archive", required=True)
    sidecar_write.add_argument("--stage", required=True)
    sidecar_write.set_defaults(func=_cmd_sidecar_write)
    sidecar_verify = sub.add_parser("sidecar-verify", help="verify archive (+ staged manifest) vs sidecar")
    sidecar_verify.add_argument("--archive", required=True)
    sidecar_verify.add_argument("--stage", default=None)
    sidecar_verify.set_defaults(func=_cmd_sidecar_verify)
    backup = sub.add_parser("sqlite-backup", help="consistent online SQLite backup (backup API)")
    backup.add_argument("--src", required=True)
    backup.add_argument("--dst", required=True)
    backup.set_defaults(func=_cmd_sqlite_backup)
    integrity = sub.add_parser("integrity-check", help="PRAGMA integrity_check (prints ok)")
    integrity.add_argument("--db", required=True)
    integrity.set_defaults(func=_cmd_integrity_check)
    retention = sub.add_parser("retention-plan", help="print victim archive names (never the newest)")
    retention.add_argument("--dir", required=True)
    retention.add_argument("--keep", type=int, default=RETENTION_COUNT)
    retention.add_argument("--max-age-days", type=int, default=RETENTION_DAYS)
    retention.set_defaults(func=_cmd_retention_plan)
    summary = sub.add_parser("summary", help="print allowlisted manifest summary (no secrets)")
    summary.add_argument("--stage", required=True)
    summary.set_defaults(func=_cmd_summary)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
