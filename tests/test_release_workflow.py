"""Task 06: versioned single-VPS release, update and rollback (unit level).

Covers: manifest build/parse/verify + checksum, no-op decision, rollback
decision logic, status rendering without secrets, GET /version payload,
update script operation order (static), dry-run FS cleanliness (shell),
broken-health -> auto-rollback (shell with mocked systemctl/curl).

Shell end-to-end runs only in dry-run/tmp mode with mocked systemd and HTTP;
nothing touches the real system. Requires bash (WSL bash.exe on Windows);
skipped otherwise. Real-VPS connections are never made.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from release_lib import (  # noqa: E402
    MANIFEST_FIELDS,
    RESTART_ORDER,
    UPDATE_ORDER,
    build_manifest,
    compute_code_checksum,
    decide_post_readiness,
    is_noop,
    load_manifest,
    render_status,
    short_sha,
    verify_manifest,
    write_manifest,
)

UPDATE_SH = REPO_ROOT / "scripts" / "update_corebot.sh"
STATUS_SH = REPO_ROOT / "scripts" / "release_status.sh"

SHA_A = "a" * 40
SHA_B = "b" * 40


def make_tree(base: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return base


# --- manifest roundtrip -----------------------------------------------------


def test_manifest_build_verify_roundtrip(tmp_path):
    make_tree(tmp_path, {"VERSION": "0.1.0\n", "main.py": "print('hi')\n"})
    manifest = write_manifest(tmp_path, tmp_path / "RELEASE.json", sha=SHA_A)
    assert set(MANIFEST_FIELDS) <= set(manifest)
    assert manifest["sha"] == SHA_A
    assert manifest["python_requires"] == ">=3.11"
    assert manifest["ubuntu"] == ["22.04", "24.04"]
    assert verify_manifest(load_manifest(tmp_path / "RELEASE.json"), tmp_path) == []


def test_manifest_checksum_mismatch_detected(tmp_path):
    make_tree(tmp_path, {"VERSION": "0.1.0\n", "main.py": "v1\n"})
    manifest = build_manifest(tmp_path, sha=SHA_A)
    assert verify_manifest(manifest, tmp_path) == []
    (tmp_path / "main.py").write_text("v2\n", encoding="utf-8")
    errors = verify_manifest(manifest, tmp_path)
    assert any("code_checksum" in e for e in errors)


def test_manifest_generated_files_excluded_from_checksum(tmp_path):
    make_tree(tmp_path, {"VERSION": "0.1.0\n", "main.py": "v1\n"})
    before = compute_code_checksum(tmp_path)
    (tmp_path / "RELEASE.json").write_text('{"x": 1}', encoding="utf-8")
    (tmp_path / ".deployed_sha").write_text(SHA_A, encoding="utf-8")
    (tmp_path / ".env").write_text("BOT_TOKEN=secret\n", encoding="utf-8")
    assert compute_code_checksum(tmp_path) == before


def test_manifest_rejects_bad_fields():
    good = {
        "version": "0.1.0",
        "sha": SHA_A,
        "python_requires": ">=3.11",
        "ubuntu": ["22.04"],
        "released_at": "2026-09-22T10:00:00Z",
        "code_checksum": "c" * 64,
    }
    assert verify_manifest(dict(good)) == []
    bad_sha = dict(good, sha="not-a-sha")
    assert any("sha" in e for e in verify_manifest(bad_sha))
    missing = dict(good)
    del missing["version"]
    assert any("version" in e for e in verify_manifest(missing))
    bad_ubuntu = dict(good, ubuntu=["20.04"])
    assert any("ubuntu" in e for e in verify_manifest(bad_ubuntu))
    bad_date = dict(good, released_at="yesterday")
    assert any("released_at" in e for e in verify_manifest(bad_date))


# --- decision logic ----------------------------------------------------------


@pytest.mark.parametrize(
    ("deployed", "target", "expected"),
    [
        (SHA_A, SHA_A, True),
        (SHA_A.upper(), SHA_A, True),
        (SHA_A, SHA_B, False),
        ("", SHA_A, False),
        (None, SHA_A, False),
        (SHA_A, None, False),
    ],
)
def test_is_noop(deployed, target, expected):
    assert is_noop(deployed, target) is expected


@pytest.mark.parametrize(
    ("ready", "version", "expected"),
    [
        (True, True, "success"),
        (False, True, "rollback"),
        (True, False, "rollback"),
        (False, False, "rollback"),
    ],
)
def test_decide_post_readiness(ready, version, expected):
    assert decide_post_readiness(ready, version) == expected


def test_short_sha():
    assert short_sha(SHA_A) == "a" * 12
    assert short_sha(None) == "unknown"


# --- status rendering never leaks secrets ------------------------------------


def test_render_status_allowlist_only():
    manifest = {
        "version": "0.1.0",
        "sha": SHA_A,
        "python_requires": ">=3.11",
        "ubuntu": ["22.04", "24.04"],
        "released_at": "2026-09-22T10:00:00Z",
        "code_checksum": "c" * 64,
        "BOT_TOKEN": "111:SECRET-TOKEN-VALUE",
        "CP_JWT_SECRET": "super-secret-jwt",
    }
    live = dict(manifest)
    out = render_status(manifest, live)
    assert "SECRET-TOKEN-VALUE" not in out
    assert "super-secret-jwt" not in out
    assert "BOT_TOKEN" not in out
    assert "CP_JWT_SECRET" not in out
    assert SHA_A in out
    assert "0.1.0" in out
    assert "match=OK" in out


def test_render_status_missing_manifest():
    out = render_status(None, None)
    assert "unknown" in out
    assert "UNAVAILABLE" in out


# --- GET /version ------------------------------------------------------------


def test_version_module_allowlisted(tmp_path, monkeypatch):
    from control_plane import version as version_mod

    manifest = {
        "version": "9.9.9",
        "sha": SHA_B,
        "python_requires": ">=3.11",
        "ubuntu": ["24.04"],
        "released_at": "2026-09-22T10:00:00Z",
        "code_checksum": "d" * 64,
        "BOT_TOKEN": "999:TOP-SECRET",
    }
    path = tmp_path / "RELEASE.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("COREBOT_RELEASE_FILE", str(path))
    info = version_mod.get_release_info()
    assert set(info) <= set(version_mod.VERSION_FIELDS)
    assert info["sha"] == SHA_B
    assert "TOP-SECRET" not in json.dumps(info)


def test_version_route_shape():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from control_plane import version as version_mod

    app = FastAPI()
    app.include_router(version_mod.router)
    body = TestClient(app).get("/version").json()
    assert set(body) <= set(version_mod.VERSION_FIELDS)
    assert "version" in body and "sha" in body


# --- update script operation order (static proof) -----------------------------


def _script_text() -> str:
    return UPDATE_SH.read_text(encoding="utf-8")


def test_update_backup_before_code_change():
    text = _script_text()
    markers = [
        "STEP 1/8 preflight",
        "STEP 2/8 backup",
        "STEP 3/8 stage",
        "STEP 4/8 dependencies",
        "STEP 5/8 stop-swap",
        "STEP 6/8 start",
        "STEP 7/8 readiness-gate",
        "STEP 8/8",
    ]
    positions = [text.index(m) for m in markers]
    assert positions == sorted(positions), "update steps out of order"
    assert text.index("swap_tree") > text.index("step_backup")
    # backup archive verified non-empty before any swap can happen
    assert '-s "$BACKUP_ARCHIVE"' in text or '-s "$BACKUP_ARCHIVE" ' in text


def test_update_never_resets_persistent_checkout():
    code_lines = [
        line
        for line in _script_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    code = "\n".join(code_lines)
    assert "reset --hard" not in code
    assert "git pull" not in code
    assert "git checkout" not in code
    assert "git archive" in code  # staged checkout into temp dir


def test_update_restart_order_cp_then_bot():
    text = _script_text()
    restarts = [
        line.strip()
        for line in text.splitlines()
        if "restart corebot" in line and line.strip().startswith('"$SYSTEMCTL"')
    ]
    assert restarts == [
        '"$SYSTEMCTL" restart corebot-cp.service',
        '"$SYSTEMCTL" restart corebot.service',
    ]
    stops = [
        line.strip()
        for line in text.splitlines()
        if "stop corebot" in line and line.strip().startswith('"$SYSTEMCTL"')
    ]
    assert stops == [
        '"$SYSTEMCTL" stop corebot-cp.service',
        '"$SYSTEMCTL" stop corebot.service',
    ]
    assert list(RESTART_ORDER) == ["corebot-cp.service", "corebot.service"]
    assert len(UPDATE_ORDER) == 8


# --- shell e2e in dry-run/tmp mode with mocked systemd + http ----------------

BASH_EXE = Path(r"C:\Windows\system32\bash.exe")


def _need_shell():
    if not BASH_EXE.exists():
        pytest.skip("bash.exe unavailable")
    if shutil.which("git") is None:
        pytest.skip("git unavailable")


def _to_wsl(path: Path) -> str:
    text = str(path.resolve())
    drive, rest = text[0].upper(), text[2:].replace("\\", "/")
    return f"/mnt/{drive.lower()}{rest}"


def _snapshot(root: Path) -> dict:
    state = {}
    for path in sorted(root.rglob("*")):
        if (
            path.is_file()
            and ".git/" not in path.as_posix()
            and "/.git" not in path.as_posix()
        ):
            state[path.relative_to(root).as_posix()] = path.read_bytes()
    return state


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


class ShellRig:
    """Fake VPS: git repo with two releases + persistent files + mock bins."""

    def __init__(self, tmp_path: Path):
        self.root = tmp_path
        self.repo = tmp_path / "fakerepo"
        self.app = tmp_path / "app"
        self.bindir = tmp_path / "bin"
        self.backups = tmp_path / "backups"
        self.bindir.mkdir()
        self.backups.mkdir()

    def build(self) -> tuple[str, str]:
        self.repo.mkdir()
        _git(["init", "-q"], self.repo)
        _git(["config", "user.email", "t@t.t"], self.repo)
        _git(["config", "user.name", "t"], self.repo)
        (self.repo / "VERSION").write_text("0.1.0\n", encoding="utf-8")
        (self.repo / "requirements.txt").write_text("# empty\n", encoding="utf-8")
        (self.repo / "app_code.txt").write_text("v1\n", encoding="utf-8")
        _git(["add", "-A"], self.repo)
        _git(["commit", "-qm", "v1"], self.repo)
        sha_v1 = _git(["rev-parse", "HEAD"], self.repo)
        (self.repo / "app_code.txt").write_text("v2\n", encoding="utf-8")
        _git(["commit", "-qam", "v2"], self.repo)
        sha_v2 = _git(["rev-parse", "HEAD"], self.repo)
        subprocess.run(
            ["git", "clone", "-q", str(self.repo), str(self.app)],
            check=True,
            capture_output=True,
        )
        _git(["checkout", "-q", sha_v1], self.app)
        (self.app / ".env").write_text(
            "BOT_TOKEN=111:AAA\nOWNER_ID=1\n", encoding="utf-8"
        )
        (self.app / "data" / "sessions").mkdir(parents=True)
        (self.app / "data" / "corebot.db").write_text("db-v1", encoding="utf-8")
        (self.app / "data" / "sessions" / "a.session").write_text(
            "sess", encoding="utf-8"
        )
        (self.app / "logs").mkdir()
        (self.app / "logs" / "corebot.log").write_text("log\n", encoding="utf-8")
        self._write_mock_bins()
        return sha_v1, sha_v2

    def _write(self, name: str, content: str) -> Path:
        path = self.bindir / name
        path.write_bytes(content.replace("\r\n", "\n").encode("utf-8"))
        return path

    def _write_mock_bins(self):
        self._write(
            "mock_systemctl.sh",
            (
                "#!/usr/bin/env bash\n"
                'echo "$*" >> "$MOCK_LOG"\n'
                'if [[ "$1" == "is-active" ]]; then echo "active"; fi\n'
                "exit 0\n"
            ),
        )
        # Health calls fail while counter <= FAIL_FIRST_HEALTH, then succeed.
        # /version always serves $VERSION_JSON_FILE (may be absent -> fail).
        self._write(
            "mock_curl.sh",
            (
                "#!/usr/bin/env bash\n"
                'url="${@: -1}"\n'
                'echo "$url" >> "$MOCK_LOG"\n'
                'if [[ "$url" == *"/version"* ]]; then\n'
                '  [[ -f "${VERSION_JSON_FILE:-}" ]] || exit 22\n'
                '  cat "$VERSION_JSON_FILE"\n'
                "  exit 0\n"
                "fi\n"
                'n=$(cat "$CURL_COUNTER" 2>/dev/null || echo 0)\n'
                "n=$((n + 1))\n"
                'echo "$n" > "$CURL_COUNTER"\n'
                "limit=${FAIL_FIRST_HEALTH:-0}\n"
                'if [[ "$n" -le "$limit" ]]; then exit 22; fi\n'
                "echo '{\"ok\":true}'\n"
                "exit 0\n"
            ),
        )
        self._write(
            "mock_backup.sh",
            (
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                'echo "backup-called $APP_DIR" >> "$MOCK_LOG"\n'
                'archive="$BACKUP_DIR/corebot-test.tar.gz"\n'
                'tar -czf "$archive" -C "$APP_DIR" .env data\n'
                'chmod 600 "$archive"\n'
                'echo "$archive"\n'
            ),
        )

    def base_env(self) -> dict:
        wsl_app = _to_wsl(self.app)
        env = dict(os.environ)
        env.update(
            {
                "APP_DIR": wsl_app,
                "VENV_PY": "/nonexistent/python",
                "PYTHON_BIN": "python3",
                "BACKUP_SCRIPT": _to_wsl(self.bindir / "mock_backup.sh"),
                "BACKUP_DIR": _to_wsl(self.backups),
                "SYSTEMCTL": _to_wsl(self.bindir / "mock_systemctl.sh"),
                "CURL": _to_wsl(self.bindir / "mock_curl.sh"),
                "HEALTH_BASE": "http://127.0.0.1:8081",
                "READY_ATTEMPTS": "2",
                "READY_INTERVAL": "0",
                "CURL_TIMEOUT": "2",
                "SKIP_VALIDATOR": "1",
                "SKIP_PIP": "1",
                "MIN_FREE_MB": "1",
                "MOCK_LOG": _to_wsl(self.root / "mock.log"),
                "CURL_COUNTER": _to_wsl(self.root / "curl.count"),
                "FAIL_FIRST_HEALTH": "0",
                "VERSION_JSON_FILE": _to_wsl(self.root / "version.json"),
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "safe.directory",
                "GIT_CONFIG_VALUE_0": "*",
            }
        )
        return env

    def run_update(self, *args: str, extra_env: dict | None = None):
        env = self.base_env()
        if extra_env:
            env.update(extra_env)
        forwarded = [k for k in env if k not in os.environ or env[k] != os.environ[k]]
        env["WSLENV"] = ":".join(sorted(set(forwarded) | {"WSLENV"}))
        return subprocess.run(
            [str(BASH_EXE), _to_wsl(UPDATE_SH), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=180,
        )

    def mock_log(self) -> str:
        path = self.root / "mock.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def chmod_mocks(self):
        script = self.root / "mkexec.sh"
        script.write_bytes(
            (
                "#!/usr/bin/env bash\nchmod +x "
                + " ".join(
                    _to_wsl(self.bindir / n)
                    for n in ("mock_systemctl.sh", "mock_curl.sh", "mock_backup.sh")
                )
                + "\n"
            ).encode("utf-8")
        )
        subprocess.run(
            [str(BASH_EXE), _to_wsl(script)],
            check=True,
            capture_output=True,
            timeout=60,
        )


def test_dry_run_changes_nothing(tmp_path):
    _need_shell()
    rig = ShellRig(tmp_path)
    sha_v1, sha_v2 = rig.build()
    rig.chmod_mocks()
    before = _snapshot(tmp_path)
    proc = rig.run_update("--sha", sha_v2, "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "DRY-RUN" in proc.stdout
    assert "NO-OP" not in proc.stdout
    assert _snapshot(tmp_path) == before
    assert rig.mock_log() == ""  # systemd/curl/backup untouched


def test_noop_repeat_same_sha(tmp_path):
    _need_shell()
    rig = ShellRig(tmp_path)
    _, sha_v2 = rig.build()
    _git(["checkout", "-q", sha_v2], rig.app)
    (rig.app / ".deployed_sha").write_text(sha_v2, encoding="utf-8")
    rig.chmod_mocks()
    before = _snapshot(tmp_path)
    proc = rig.run_update("--sha", sha_v2)
    assert proc.returncode == 0, proc.stderr
    assert "NO-OP" in proc.stdout
    assert _snapshot(tmp_path) == before


def test_broken_health_triggers_auto_rollback(tmp_path):
    _need_shell()
    rig = ShellRig(tmp_path)
    sha_v1, sha_v2 = rig.build()
    rig.chmod_mocks()
    # /version serves the PREVIOUS sha (as if new code never became live);
    # first 2 health calls fail -> initial gate fails -> rollback expected.
    (rig.root / "version.json").write_text(
        json.dumps({"version": "0.1.0", "sha": sha_v1}), encoding="utf-8"
    )
    proc = rig.run_update("--sha", sha_v2, extra_env={"FAIL_FIRST_HEALTH": "2"})
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "ROLLED BACK" in proc.stdout
    # previous code restored, persistent paths intact
    assert (rig.app / "app_code.txt").read_text(encoding="utf-8") == "v1\n"
    assert (rig.app / ".env").read_text(
        encoding="utf-8"
    ) == "BOT_TOKEN=111:AAA\nOWNER_ID=1\n"
    assert (rig.app / "data" / "corebot.db").read_text(encoding="utf-8") == "db-v1"
    assert (rig.app / "data" / "sessions" / "a.session").read_text(
        encoding="utf-8"
    ) == "sess"
    assert (rig.app / ".deployed_sha").read_text(encoding="utf-8") == sha_v1
    log = rig.mock_log()
    # stop order cp -> bot, start order cp -> bot, twice (update + rollback)
    seq = [
        line
        for line in log.splitlines()
        if line
        in (
            "stop corebot-cp.service",
            "stop corebot.service",
            "restart corebot-cp.service",
            "restart corebot.service",
        )
    ]
    assert seq == [
        "stop corebot-cp.service",
        "stop corebot.service",
        "restart corebot-cp.service",
        "restart corebot.service",
        "restart corebot-cp.service",
        "restart corebot.service",
    ]
    assert "is-active" in log  # both services re-checked active after rollback


def test_success_path_records_deployed_sha(tmp_path):
    _need_shell()
    rig = ShellRig(tmp_path)
    sha_v1, sha_v2 = rig.build()
    rig.chmod_mocks()
    (rig.root / "version.json").write_text(
        json.dumps({"version": "0.1.0", "sha": sha_v2}), encoding="utf-8"
    )
    proc = rig.run_update("--sha", sha_v2)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "UPDATE OK" in proc.stdout
    assert (rig.app / "app_code.txt").read_text(encoding="utf-8") == "v2\n"
    assert (rig.app / ".deployed_sha").read_text(encoding="utf-8") == sha_v2
    # persistent paths not overwritten by the swap
    assert (rig.app / ".env").read_text(
        encoding="utf-8"
    ) == "BOT_TOKEN=111:AAA\nOWNER_ID=1\n"
    assert (rig.app / "data" / "sessions" / "a.session").read_text(
        encoding="utf-8"
    ) == "sess"
    assert (rig.app / "RELEASE.json").exists()
    manifest = json.loads((rig.app / "RELEASE.json").read_text(encoding="utf-8"))
    assert manifest["sha"] == sha_v2
    assert verify_manifest(manifest) == []


def test_release_status_hides_secrets(tmp_path):
    _need_shell()
    manifest = {
        "version": "0.1.0",
        "sha": SHA_A,
        "python_requires": ">=3.11",
        "ubuntu": ["22.04"],
        "released_at": "2026-09-22T10:00:00Z",
        "code_checksum": "c" * 64,
        "BOT_TOKEN": "111:LEAK-ME-NOT",
    }
    path = tmp_path / "RELEASE.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    proc = subprocess.run(
        [str(BASH_EXE), _to_wsl(STATUS_SH), "--manifest", _to_wsl(path), "--no-live"],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "APP_DIR": _to_wsl(tmp_path)},
    )
    assert proc.returncode == 0, proc.stderr
    assert SHA_A in proc.stdout
    assert "LEAK-ME-NOT" not in proc.stdout
    assert "BOT_TOKEN" not in proc.stdout
