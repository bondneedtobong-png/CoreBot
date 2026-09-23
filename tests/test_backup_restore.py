"""Task 09: automatic backups and verified restore (unit + shell level).

Covers: manifest build/verify roundtrip, checksum-mismatch refusal, sidecar
roundtrip, retention (never deletes the last backup), summary without
secrets, DB schema identity, backup/restore shell roundtrip (archive 600,
restored .env 600, live-dir refusal, non-empty refusal), backup under WAL
write load (integrity_check ok after restore), systemd unit content.

Shell tests need bash (WSL bash.exe on Windows); skipped otherwise. POSIX
permission bits are meaningless on /mnt/c (drvfs reports 777 for
everything), so every shell test keeps its data in WSL /tmp (ext4) and
asserts modes with `stat` INSIDE the driver script. Only the driver/setup
scripts themselves live on /mnt/c (read-only from WSL). Windows-side
asserts are exit code + stdout markers only. Real VPS/systemd/cron are
never touched — the "instance" is a synthetic fixture (SYNTHETIC .env,
test sqlite DBs, fake sessions).
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from backup_lib import (  # noqa: E402
    BACKUP_FORMAT,
    BACKUP_MAX_AGE_HOURS,
    RETENTION_COUNT,
    RETENTION_DAYS,
    build_manifest,
    db_identity,
    db_integrity_check,
    load_manifest,
    read_release_info,
    render_summary,
    scan_stage,
    select_retention_victims,
    sqlite_backup,
    verify_manifest,
    verify_sidecar,
    write_manifest,
    write_sidecar,
)

BACKUP_SH = (
    REPO_ROOT / "skills" / "corebot-vps-deploy" / "scripts" / "backup_corebot.sh"
)
RESTORE_SH = (
    REPO_ROOT / "skills" / "corebot-vps-deploy" / "scripts" / "restore_corebot.sh"
)
SYSTEMD_DIR = REPO_ROOT / "skills" / "corebot-vps-deploy" / "systemd"

SYNTHETIC_ENV = (
    "API_ID=12345\n"
    "API_HASH=SYNTHETIC_HASH_FOR_TESTS_ONLY\n"
    "BOT_TOKEN=000000:SYNTHETIC_TEST_ONLY\n"
    "OWNER_ID=123456789\n"
)


def make_db(path: Path, user_version: int = 0, rows: int = 3) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA user_version={int(user_version)}")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS probe (id INTEGER PRIMARY KEY, v TEXT)"
        )
        for i in range(rows):
            conn.execute("INSERT INTO probe (v) VALUES (?)", (f"v{i}",))
        conn.commit()
    finally:
        conn.close()


def make_stage(base: Path) -> Path:
    """Synthetic staged payload (mirrors the backup staging layout)."""
    base.mkdir(parents=True, exist_ok=True)
    (base / ".env").write_text(SYNTHETIC_ENV, encoding="utf-8")
    make_db(base / "data" / "corebot.db", user_version=3)
    make_db(base / "data" / "control_plane.db", user_version=5)
    (base / "data" / "sessions").mkdir(parents=True, exist_ok=True)
    (base / "data" / "sessions" / "acc.session").write_bytes(b"fake-session-bytes")
    (base / "logs").mkdir(exist_ok=True)
    (base / "logs" / "corebot.log").write_text("log\n", encoding="utf-8")
    return base


def build_stage_manifest(stage: Path) -> dict:
    files, databases = scan_stage(stage)
    manifest = build_manifest(
        app_dir=REPO_ROOT,
        files=files,
        databases=databases,
        created_at="2026-09-22T10:00:00Z",
        duration_sec=1.2,
        instance={"id": "test-instance", "name": "testhost"},
        release={"version": "0.1.0", "sha": "a" * 40},
    )
    write_manifest(manifest, stage)
    return manifest


# --- manifest / sidecar (pure python) -----------------------------------------


def test_manifest_build_verify_roundtrip(tmp_path):
    stage = make_stage(tmp_path / "stage")
    manifest = build_stage_manifest(stage)
    assert manifest["format"] == BACKUP_FORMAT
    assert manifest["instance"]["id"] == "test-instance"
    assert set(manifest["databases"]) == {"data/corebot.db", "data/control_plane.db"}
    assert verify_manifest(load_manifest(stage), stage) == []


def test_manifest_checksum_mismatch_detected(tmp_path):
    stage = make_stage(tmp_path / "stage")
    manifest = build_stage_manifest(stage)
    assert verify_manifest(manifest, stage) == []
    (stage / "data" / "sessions" / "acc.session").write_bytes(b"Xake-session-bytes")
    errors = verify_manifest(manifest, stage)
    assert any("checksum mismatch" in e for e in errors)


def test_manifest_rejects_bad_timestamp():
    bad = {
        "format": BACKUP_FORMAT,
        "instance": {},
        "release": {},
        "created_at_utc": "yesterday",
        "databases": {},
        "files": [{"path": "x", "size": 1, "sha256": "0" * 64}],
    }
    assert any("created_at_utc" in e for e in verify_manifest(bad, REPO_ROOT))


def test_sidecar_roundtrip_and_archive_mismatch(tmp_path):
    import tarfile

    stage = make_stage(tmp_path / "stage")
    manifest = build_stage_manifest(stage)
    archive = tmp_path / "corebot-20260922T100000Z.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(stage, arcname=".")
    write_sidecar(archive, manifest)
    assert verify_sidecar(archive, stage) == []
    with open(archive, "ab") as fh:
        fh.write(b"\x00")
    errors = verify_sidecar(archive, stage)
    assert any("archive checksum mismatch" in e for e in errors)


def test_render_summary_has_no_secrets(tmp_path):
    stage = make_stage(tmp_path / "stage")
    manifest = build_stage_manifest(stage)
    out = render_summary(manifest)
    assert "SYNTHETIC_TEST_ONLY" not in out
    assert "SYNTHETIC_HASH_FOR_TESTS_ONLY" not in out
    assert "test-instance" in out
    assert "files=" in out


def test_db_identity_changes_on_ddl(tmp_path):
    db = tmp_path / "t.db"
    make_db(db)
    before = db_identity(db)
    assert before["user_version"] == 0
    assert "probe" in before["tables"]
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("CREATE TABLE extra (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()
    after = db_identity(db)
    assert after["tables_hash"] != before["tables_hash"]
    assert "extra" in after["tables"]
    assert db_integrity_check(db) == "ok"


def test_sqlite_backup_under_wal_write_load(tmp_path):
    """Concurrent WAL writer + backup API: snapshot stays consistent."""
    src = tmp_path / "live.db"
    make_db(src)
    stop = threading.Event()
    errors: list[str] = []

    def writer():
        conn = sqlite3.connect(str(src), timeout=30.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            i = 0
            while not stop.is_set():
                try:
                    conn.execute("INSERT INTO probe (v) VALUES (?)", (f"w{i}",))
                    conn.commit()
                except Exception as exc:  # noqa: BLE001
                    errors.append(str(exc))
                    break
                i += 1
        finally:
            conn.close()

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        sqlite_backup(src, tmp_path / "snap.db")
    finally:
        stop.set()
        thread.join(timeout=60)
    assert not errors
    assert db_integrity_check(tmp_path / "snap.db") == "ok"


def test_retention_never_deletes_last():
    now = 1_786_000_000.0
    # Lone backup is kept even when ancient.
    assert (
        select_retention_victims([("corebot-old.tar.gz", now - 400 * 86400)], now=now)
        == []
    )
    assert select_retention_victims([], now=now) == []
    # Age rule deletes old archives but never the newest.
    entries = [
        ("corebot-003.tar.gz", now - 1 * 86400),
        ("corebot-001.tar.gz", now - 40 * 86400),
        ("corebot-002.tar.gz", now - 35 * 86400),
    ]
    victims = select_retention_victims(entries, now=now)
    assert sorted(victims) == ["corebot-001.tar.gz", "corebot-002.tar.gz"]
    # Count rule keeps the newest N.
    many = [(f"corebot-{i:03d}.tar.gz", now - (10 - i) * 3600) for i in range(10)]
    victims = select_retention_victims(many, now=now)
    assert len(victims) == 10 - RETENTION_COUNT
    assert "corebot-009.tar.gz" not in victims


def test_backup_age_threshold_matches_watchdog():
    from control_plane.services.snapshot import BACKUP_MAX_AGE_HOURS as SNAPSHOT_MAX

    assert BACKUP_MAX_AGE_HOURS == SNAPSHOT_MAX == 26.0


def test_retention_numbers_fixed():
    assert (RETENTION_COUNT, RETENTION_DAYS) == (7, 30)


def test_release_info_reuses_task06_format(tmp_path):
    (tmp_path / "VERSION").write_text("0.1.0\n", encoding="utf-8")
    info = read_release_info(tmp_path)
    assert info["version"] == "0.1.0"
    assert "sha" in info
    assert set(info) <= {
        "version",
        "sha",
        "python_requires",
        "ubuntu",
        "released_at",
        "code_checksum",
    }


# --- shell tests (WSL bash.exe; all data in WSL /tmp) --------------------------


BASH_EXE = Path(r"C:\Windows\system32\bash.exe")

SETUP_PY = (
    "import sqlite3, sys\n"
    "from pathlib import Path\n"
    "app = Path(sys.argv[1])\n"
    "app.mkdir(parents=True, exist_ok=True)\n"
    "app.joinpath('.env').write_text(" + repr(SYNTHETIC_ENV) + ", encoding='utf-8')\n"
    "for name, uv in (('corebot.db', 3), ('control_plane.db', 5)):\n"
    "    db = app / 'data' / name\n"
    "    db.parent.mkdir(parents=True, exist_ok=True)\n"
    "    conn = sqlite3.connect(str(db))\n"
    "    conn.execute('PRAGMA journal_mode=WAL')\n"
    "    conn.execute(f'PRAGMA user_version={uv}')\n"
    "    conn.execute('CREATE TABLE probe (id INTEGER PRIMARY KEY, v TEXT)')\n"
    "    conn.executemany('INSERT INTO probe (v) VALUES (?)', [(f'v{i}',) for i in range(3)])\n"
    "    conn.commit()\n"
    "    conn.close()\n"
    "app.joinpath('data/sessions').mkdir(parents=True, exist_ok=True)\n"
    "app.joinpath('data/sessions/acc.session').write_bytes(b'fake-session-bytes')\n"
    "app.joinpath('logs').mkdir(exist_ok=True)\n"
    "app.joinpath('logs/corebot.log').write_text('log\\n', encoding='utf-8')\n"
)

WRITER_PY = (
    "import sqlite3, sys, time\n"
    "db, secs = sys.argv[1], float(sys.argv[2])\n"
    "conn = sqlite3.connect(db, timeout=30.0)\n"
    "conn.execute('PRAGMA journal_mode=WAL')\n"
    "i, t0 = 0, time.time()\n"
    "while time.time() - t0 < secs:\n"
    "    conn.execute('INSERT INTO probe (v) VALUES (?)', (f'load-{i}',))\n"
    "    conn.commit()\n"
    "    i += 1\n"
    "conn.close()\n"
    "print(f'wrote {i} rows')\n"
)


def _need_shell():
    if not BASH_EXE.exists():
        pytest.skip("bash.exe unavailable")


def _to_wsl(path: Path) -> str:
    text = str(path.resolve())
    drive, rest = text[0].upper(), text[2:].replace("\\", "/")
    return f"/mnt/{drive.lower()}{rest}"


def _run_driver(tmp_path: Path, body: str) -> subprocess.CompletedProcess:
    """Write setup/writer/driver scripts to tmp_path, run driver in WSL."""
    (tmp_path / "setup.py").write_text(SETUP_PY, encoding="utf-8")
    (tmp_path / "writer.py").write_text(WRITER_PY, encoding="utf-8")
    header = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "LIB=" + _to_wsl(REPO_ROOT / "scripts" / "backup_lib.py") + "\n"
        "BACKUP=" + _to_wsl(BACKUP_SH) + "\n"
        "RESTORE=" + _to_wsl(RESTORE_SH) + "\n"
        "SETUP=" + _to_wsl(tmp_path / "setup.py") + "\n"
        "WRITER=" + _to_wsl(tmp_path / "writer.py") + "\n"
        "ROOT=$(mktemp -d)\n"
        "trap 'rm -rf \"$ROOT\"' EXIT\n"
        "APP=$ROOT/app; BACKUPS=$ROOT/backups\n"
        'mkdir -p "$APP" "$BACKUPS"\n'
        'python3 "$SETUP" "$APP"\n'
        'export APP_DIR="$APP" BACKUP_DIR="$BACKUPS" PYTHON_BIN=python3\n'
        "export INSTANCE_ID=drill-instance INSTANCE_NAME=drillhost SYSTEMCTL=true\n"
    )
    driver = tmp_path / "driver.sh"
    driver.write_bytes((header + body).encode("utf-8"))
    return subprocess.run(
        [str(BASH_EXE), _to_wsl(driver)], capture_output=True, text=True, timeout=300
    )


def test_backup_script_produces_archive_manifest_sidecar(tmp_path):
    _need_shell()
    proc = _run_driver(
        tmp_path,
        """\
archive=$("$BACKUP" | tail -n 1)
[ -f "$archive" ]
[ "$(stat -c %a "$archive")" = 600 ]
[ "$(stat -c %a "$BACKUPS")" = 700 ]
[ -f "$archive.sha256" ]
[ "$(stat -c %a "$archive.sha256")" = 600 ]
grep -q "$(basename "$archive")" "$BACKUPS/.last_backup_ok"
[ "$(stat -c %a "$BACKUPS/.last_backup_ok")" = 600 ]
tar -tzf "$archive" > "$ROOT/listing.txt"
grep -q backup_manifest.json "$ROOT/listing.txt"
e="$ROOT/extract"; mkdir -p "$e"; tar -xzf "$archive" -C "$e"
python3 "$LIB" manifest-verify --stage "$e"
python3 "$LIB" sidecar-verify --archive "$archive" --stage "$e"
echo BACKUP-OK
""",
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert "BACKUP-OK" in proc.stdout


def test_backup_dry_run_changes_nothing(tmp_path):
    _need_shell()
    proc = _run_driver(
        tmp_path,
        """\
out=$("$BACKUP" --dry-run | tail -n 1)
[ ! -e "$out" ]
[ -z "$(ls "$BACKUPS"/corebot-*.tar.gz 2>/dev/null || true)" ]
[ ! -e "$BACKUPS/.last_backup_ok" ]
echo DRYRUN-OK
""",
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert "DRYRUN-OK" in proc.stdout


def test_restore_roundtrip_and_perms(tmp_path):
    _need_shell()
    proc = _run_driver(
        tmp_path,
        """\
archive=$("$BACKUP" | tail -n 1)
target=$ROOT/restored
"$RESTORE" --archive "$archive" --target "$target" > "$ROOT/restore.out"
[ "$(stat -c %a "$target/.env")" = 600 ]
[ "$(stat -c %a "$target/data/corebot.db")" = 644 ]
[ "$(stat -c %a "$target/data")" = 755 ]
python3 "$LIB" integrity-check --db "$target/data/corebot.db"
python3 "$LIB" integrity-check --db "$target/data/control_plane.db"
! grep -q SYNTHETIC_TEST_ONLY "$ROOT/restore.out"
echo RESTORE-OK
""",
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert "RESTORE-OK" in proc.stdout


def test_restore_refuses_nonempty_dir(tmp_path):
    _need_shell()
    proc = _run_driver(
        tmp_path,
        """\
archive=$("$BACKUP" | tail -n 1)
target=$ROOT/restored; mkdir -p "$target"; echo x > "$target/something.txt"
set +e
"$RESTORE" --archive "$archive" --target "$target" 2> "$ROOT/err"
rc=$?
set -e
[ "$rc" = 4 ]
grep -q "not empty" "$ROOT/err"
echo REFUSE-NONEMPTY-OK
""",
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert "REFUSE-NONEMPTY-OK" in proc.stdout


def test_restore_refuses_live_even_with_allow_nonempty(tmp_path):
    _need_shell()
    proc = _run_driver(
        tmp_path,
        """\
archive=$("$BACKUP" | tail -n 1)
live=$ROOT/live
python3 "$SETUP" "$live"
before=$(cat "$live/data/sessions/acc.session")
set +e
"$RESTORE" --archive "$archive" --target "$live" --allow-nonempty 2> "$ROOT/err"
rc=$?
set -e
[ "$rc" = 4 ]
grep -q "LIVE" "$ROOT/err"
[ "$(cat "$live/data/sessions/acc.session")" = "$before" ]
echo REFUSE-LIVE-OK
""",
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert "REFUSE-LIVE-OK" in proc.stdout


def test_restore_refuses_checksum_mismatch(tmp_path):
    _need_shell()
    proc = _run_driver(
        tmp_path,
        """\
archive=$("$BACKUP" | tail -n 1)
printf '\\x00' >> "$archive"
set +e
"$RESTORE" --archive "$archive" --target "$ROOT/restored" 2> "$ROOT/err"
rc=$?
set -e
[ "$rc" = 3 ]
[ ! -e "$ROOT/restored/.env" ]
echo REFUSE-CHECKSUM-OK
""",
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert "REFUSE-CHECKSUM-OK" in proc.stdout


def test_restore_refuses_manifest_mismatch(tmp_path):
    _need_shell()
    proc = _run_driver(
        tmp_path,
        """\
archive=$("$BACKUP" | tail -n 1)
e="$ROOT/repack"; mkdir -p "$e"
tar -xzf "$archive" -C "$e"
echo tampered-payload > "$e/data/sessions/acc.session"
tar -C "$e" -czf "$archive" .
sha256sum "$archive" | sed "s| .*|  $(basename "$archive")|" > "$archive.sha256"
set +e
"$RESTORE" --archive "$archive" --target "$ROOT/restored" 2> "$ROOT/err"
rc=$?
set -e
[ "$rc" = 5 ]
[ ! -e "$ROOT/restored/.env" ]
echo REFUSE-MANIFEST-OK
""",
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert "REFUSE-MANIFEST-OK" in proc.stdout


def test_shell_retention_keeps_newest_and_caps_count(tmp_path):
    _need_shell()
    proc = _run_driver(
        tmp_path,
        """\
for i in $(seq 0 8); do
  f="$BACKUPS/corebot-2025070${i}T000000Z.tar.gz"
  echo fake > "$f"; echo x > "$f.sha256"
  touch -d '40 days ago' "$f" "$f.sha256"
done
archive=$("$BACKUP" | tail -n 1)
n=$(ls "$BACKUPS"/corebot-*.tar.gz | wc -l)
[ "$n" -le 7 ]
[ -f "$archive" ]
echo RETENTION-OK
""",
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert "RETENTION-OK" in proc.stdout


def test_backup_under_wal_write_load_shell(tmp_path):
    """Writer hammers corebot.db (WAL) while backup+restore run; integrity ok."""
    _need_shell()
    proc = _run_driver(
        tmp_path,
        """\
python3 "$WRITER" "$APP/data/corebot.db" 20 & writer=$!
archive=$("$BACKUP" | tail -n 1)
wait "$writer"
python3 "$LIB" sidecar-verify --archive "$archive"
"$RESTORE" --archive "$archive" --target "$ROOT/restored"
python3 "$LIB" integrity-check --db "$ROOT/restored/data/corebot.db"
python3 "$LIB" integrity-check --db "$ROOT/restored/data/control_plane.db"
echo "LOAD-DRILL OK $(basename "$archive")"
""",
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert "LOAD-DRILL OK" in proc.stdout


# --- systemd units (static: no systemd on this machine) ------------------------


def _unit(name: str) -> str:
    return (SYSTEMD_DIR / name).read_text(encoding="utf-8")


def test_backup_service_unit():
    body = _unit("corebot-backup.service")
    assert "Type=oneshot" in body
    assert "backup_corebot.sh" in body
    assert "OnFailure=" in body
    assert "User=corebot" in body


def test_backup_timer_unit():
    body = _unit("corebot-backup.timer")
    assert "OnCalendar=" in body
    assert "RandomizedDelaySec=" in body
    assert "Persistent=true" in body
    assert "Unit=corebot-backup.service" in body


def test_timer_and_service_idempotent_single_job():
    """Re-running install/enable is safe; only one job can exist."""
    timer = _unit("corebot-backup.timer")
    service = _unit("corebot-backup.service")
    # Exactly one Unit= binding and one OnCalendar= line: a single job.
    assert timer.count("OnCalendar=") == 1
    assert timer.count("Unit=") == 1
    # Script-level flock is the concurrency guard referenced by the unit.
    backup_body = BACKUP_SH.read_text(encoding="utf-8")
    assert "flock -n" in backup_body
    assert "WantedBy=" in timer and "WantedBy=" in service
