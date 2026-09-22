"""Config/security validation tests (task 04).

Four env classes (valid, empty, unsafe, malformed) x local/production,
CLI exit codes, secret masking (capsys), no-caching, and production
fail-fast gates for both entry points.

All fixtures use synthetic values only (``TEST-...``); no real secrets.
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest
from fastapi import APIRouter

from tools import validate_config as vc

KNOWN_KEYS = (
    "COREBOT_ENV",
    "API_ID",
    "API_HASH",
    "BOT_TOKEN",
    "OWNER_ID",
    "DATABASE_URL",
    "CP_DATABASE_URL",
    "BOT_DATABASE_URL",
    "LOG_LEVEL",
    "DEFAULT_DELAY_BETWEEN_MESSAGES",
    "DEFAULT_DELAY_BETWEEN_ACCOUNTS",
    "MAX_RETRIES_ON_FLOOD",
    "PARSER_EMBEDDED",
    "PARSER_POLL_SEC",
    "CONTROL_BOT_PROXY_TYPE",
    "CONTROL_BOT_PROXY_HOST",
    "CONTROL_BOT_PROXY_PORT",
    "CONTROL_BOT_PROXY_USERNAME",
    "CONTROL_BOT_PROXY_PASSWORD",
    "CP_AGENT_ENABLED",
    "CP_INGEST_URL",
    "CP_AGENT_TOKEN",
    "CP_AGENT_NAME",
    "CP_AGENT_VERSION",
    "CP_JWT_SECRET",
    "CP_BOOTSTRAP_ADMIN_USERNAME",
    "CP_BOOTSTRAP_ADMIN_PASSWORD",
    "CP_TELEGRAM_BOT_TOKEN",
    "CP_TELEGRAM_ALERT_CHAT_ID",
    "CP_BUSINESS_STREAM_INTERVAL",
    "CP_BUSINESS_STREAM_BATCH",
    "OPENROUTER_API_KEY",
    "OPENROUTER_KEY_ENCRYPTION_KEY",
    "OPENROUTER_BASE_URL",
    "OPENROUTER_HTTP_REFERER",
    "NEUROCHAT_ENABLED",
    "DEFAULT_NEURO_MODEL",
    "NEURO_MAX_CONCURRENT",
    "NEURO_OPENROUTER_MAX_RETRIES",
    "NEURO_OPENROUTER_RETRY_BASE_SEC",
    "NEURO_MAX_TOKENS",
    "NEURO_DEFAULT_TEMPERATURE",
    "NEURO_DEFAULT_TOP_P",
    "NEURO_DEFAULT_TOP_K",
    "NEURO_DEFAULT_FREQUENCY_PENALTY",
    "NEURO_DEFAULT_PRESENCE_PENALTY",
    "NEURO_DEFAULT_REPETITION_PENALTY",
    "NEURO_DEFAULT_MIN_P",
    "NEURO_DEFAULT_TOP_A",
    "MAILING_BASE_UTC_OFFSET",
)

VALID_ENV = {
    "API_ID": "7654321",
    "API_HASH": "0123456789abcdef0123456789abcdef",
    "BOT_TOKEN": "123456789:TEST-synthetic-token",
    "OWNER_ID": "987654321",
    "DATABASE_URL": "sqlite+aiosqlite:////opt/corebot/app/data/corebot.db",
    "CP_DATABASE_URL": "sqlite:////opt/corebot/app/data/control_plane.db",
    "BOT_DATABASE_URL": "sqlite:////opt/corebot/app/data/corebot.db",
    "CP_JWT_SECRET": "ab12cd34" * 8,  # 64 synthetic hex chars
    "CP_BOOTSTRAP_ADMIN_USERNAME": "TEST-admin",
    "CP_BOOTSTRAP_ADMIN_PASSWORD": "TEST-strong-password-01",
}

UNSAFE_ENV = dict(
    VALID_ENV,
    CP_JWT_SECRET="change-me-in-production",
    CP_BOOTSTRAP_ADMIN_PASSWORD="admin123",
)

SHORT_JWT_ENV = dict(VALID_ENV, CP_JWT_SECRET="TEST-short")

MALFORMED_ENV = dict(
    VALID_ENV,
    API_ID="TEST-not-a-number",
    OWNER_ID="TEST-not-a-number",
    BOT_TOKEN="TEST-no-colon-here",
    CP_INGEST_URL="http://[::1/nonsense",
    PARSER_EMBEDDED="TEST-maybe",
    LOG_LEVEL="TEST-verbose",
    MAILING_BASE_UTC_OFFSET="TEST-plus-five",
)

SECRET_MARKERS = (
    "TEST-synthetic-token",
    "TEST-strong-password-01",
    "TEST-super-secret-jwt-marker",
)


@pytest.fixture
def clean_env(monkeypatch):
    """Clear all known keys so the real .env can never leak into tests."""
    for key in KNOWN_KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def _run_cli(monkeypatch, env_map: dict[str, str], *argv: str) -> int:
    for key in KNOWN_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in env_map.items():
        monkeypatch.setenv(key, value)
    return vc.main(list(argv))


# --- 1. valid env --------------------------------------------------------


def test_valid_env_production_ok():
    result = vc.validate("production", VALID_ENV)
    assert result.ok
    assert result.errors == []


def test_valid_env_local_ok_no_errors():
    result = vc.validate("local", VALID_ENV)
    assert result.ok
    assert result.errors == []


# --- 2. empty env --------------------------------------------------------


def test_empty_env_production_fails():
    result = vc.validate("production", {})
    assert not result.ok
    names = result.failed_names
    for expected in (
        "API_ID",
        "API_HASH",
        "BOT_TOKEN",
        "OWNER_ID",
        "CP_JWT_SECRET",
        "CP_BOOTSTRAP_ADMIN_PASSWORD",
    ):
        assert expected in names


def test_empty_env_local_warns_but_ok():
    result = vc.validate("local", {})
    assert result.ok
    assert result.errors == []
    assert len(result.warnings) > 0


# --- 3. unsafe env -------------------------------------------------------


def test_unsafe_defaults_production_fails():
    result = vc.validate("production", UNSAFE_ENV)
    assert not result.ok
    assert "CP_JWT_SECRET" in result.failed_names
    assert "CP_BOOTSTRAP_ADMIN_PASSWORD" in result.failed_names


def test_unsafe_defaults_local_warns_but_ok():
    result = vc.validate("local", UNSAFE_ENV)
    assert result.ok
    joined = "\n".join(result.warnings)
    assert "CP_JWT_SECRET" in joined
    assert "CP_BOOTSTRAP_ADMIN_PASSWORD" in joined


def test_short_jwt_production_fails_local_warns():
    prod = vc.validate("production", SHORT_JWT_ENV)
    assert not prod.ok
    assert "CP_JWT_SECRET" in prod.failed_names
    local = vc.validate("local", SHORT_JWT_ENV)
    assert local.ok
    assert any("CP_JWT_SECRET" in w for w in local.warnings)


@pytest.mark.parametrize(
    "bad_password",
    ["admin123", "admin", "password", "change-me-please", "change-me-later", "ADMIN123"],
)
def test_weak_admin_passwords_rejected_in_production(bad_password):
    env = dict(VALID_ENV, CP_BOOTSTRAP_ADMIN_PASSWORD=bad_password)
    result = vc.validate("production", env)
    assert not result.ok
    assert "CP_BOOTSTRAP_ADMIN_PASSWORD" in result.failed_names


@pytest.mark.parametrize(
    "bad_jwt",
    ["change-me-in-production", "change-me-please", "admin123", "short"],
)
def test_weak_jwt_rejected_in_production(bad_jwt):
    env = dict(VALID_ENV, CP_JWT_SECRET=bad_jwt)
    result = vc.validate("production", env)
    assert not result.ok
    assert "CP_JWT_SECRET" in result.failed_names


def test_relative_sqlite_paths_rejected_in_production():
    env = dict(
        VALID_ENV,
        DATABASE_URL="sqlite+aiosqlite:///data/corebot.db",
        CP_DATABASE_URL="sqlite:///data/control_plane.db",
        BOT_DATABASE_URL="sqlite:///data/corebot.db",
    )
    result = vc.validate("production", env)
    assert not result.ok
    assert "DATABASE_URL" in result.failed_names
    assert "CP_DATABASE_URL" in result.failed_names
    assert "BOT_DATABASE_URL" in result.failed_names


def test_relative_sqlite_paths_warn_in_local():
    env = dict(
        VALID_ENV,
        DATABASE_URL="sqlite+aiosqlite:///data/corebot.db",
        CP_DATABASE_URL="sqlite:///data/control_plane.db",
        BOT_DATABASE_URL="sqlite:///data/corebot.db",
    )
    result = vc.validate("local", env)
    assert result.ok


def test_public_ingest_url_rejected_in_production():
    env = dict(VALID_ENV, CP_INGEST_URL="http://0.0.0.0:8081/ingest/batch")
    result = vc.validate("production", env)
    assert not result.ok
    assert "CP_INGEST_URL" in result.failed_names


def test_loopback_ingest_url_ok():
    for url in ("http://127.0.0.1:8081/ingest/batch", "http://localhost:8081/ingest/batch"):
        result = vc.validate("production", dict(VALID_ENV, CP_INGEST_URL=url))
        assert result.ok, url


# --- 4. malformed env ----------------------------------------------------


def test_malformed_env_production_fails():
    result = vc.validate("production", MALFORMED_ENV)
    assert not result.ok
    names = result.failed_names
    for expected in ("API_ID", "OWNER_ID", "BOT_TOKEN", "PARSER_EMBEDDED", "LOG_LEVEL"):
        assert expected in names, expected


def test_malformed_env_local_warns_but_ok():
    result = vc.validate("local", MALFORMED_ENV)
    assert result.ok
    assert result.errors == []
    assert len(result.warnings) > 0


def test_agent_token_required_when_enabled():
    env = dict(VALID_ENV, CP_AGENT_ENABLED="1", CP_AGENT_TOKEN="")
    result = vc.validate("production", env)
    assert not result.ok
    assert "CP_AGENT_TOKEN" in result.failed_names


def test_bad_proxy_port_rejected():
    env = dict(
        VALID_ENV,
        CONTROL_BOT_PROXY_TYPE="socks5",
        CONTROL_BOT_PROXY_HOST="TEST-proxy.local",
        CONTROL_BOT_PROXY_PORT="TEST-not-a-port",
    )
    result = vc.validate("production", env)
    assert not result.ok
    assert "CONTROL_BOT_PROXY_PORT" in result.failed_names


# --- CLI exit codes ------------------------------------------------------


def test_cli_exit_codes(clean_env, capsys):
    assert _run_cli(clean_env, VALID_ENV, "--mode", "production") == 0
    assert _run_cli(clean_env, {}, "--mode", "local") == 0  # warnings, still 0
    assert _run_cli(clean_env, {}, "--mode", "production") == 2
    assert _run_cli(clean_env, UNSAFE_ENV, "--mode", "production") == 2
    assert _run_cli(clean_env, MALFORMED_ENV, "--mode", "production") == 2
    assert _run_cli(clean_env, VALID_ENV, "--mode", "bogus") == 1
    assert _run_cli(clean_env, VALID_ENV, "--mode", "production", "--env-file", "no-such.env") == 1


def test_cli_mode_defaults_to_local(clean_env, capsys):
    assert _run_cli(clean_env, {}) == 0
    out = capsys.readouterr().out
    assert "local mode" in out


def test_cli_mode_from_corebot_env(clean_env, capsys):
    assert _run_cli(clean_env, {"COREBOT_ENV": "production"}) == 2
    assert _run_cli(clean_env, {"COREBOT_ENV": "local"}) == 0


def test_cli_env_file_overrides_process_env(clean_env, tmp_path, capsys):
    env_file = tmp_path / "TEST-corebot.env"
    env_file.write_text(
        "API_ID=7654321\n"
        "API_HASH=0123456789abcdef0123456789abcdef\n"
        "BOT_TOKEN=123456789:TEST-synthetic-token\n"
        "OWNER_ID=987654321\n"
        "DATABASE_URL=sqlite+aiosqlite:////opt/corebot/app/data/corebot.db\n"
        "CP_DATABASE_URL=sqlite:////opt/corebot/app/data/control_plane.db\n"
        "BOT_DATABASE_URL=sqlite:////opt/corebot/app/data/corebot.db\n"
        f"CP_JWT_SECRET={'ab12cd34' * 8}\n"
        "CP_BOOTSTRAP_ADMIN_USERNAME=TEST-admin\n"
        "CP_BOOTSTRAP_ADMIN_PASSWORD=TEST-strong-password-01\n",
        encoding="utf-8",
    )
    assert _run_cli(clean_env, {}, "--mode", "production", "--env-file", str(env_file)) == 0


def test_cli_env_file_unsafe_reports_error(clean_env, tmp_path, capsys):
    env_file = tmp_path / "TEST-unsafe.env"
    env_file.write_text(
        "CP_JWT_SECRET=change-me-in-production\nCP_BOOTSTRAP_ADMIN_PASSWORD=admin123\n",
        encoding="utf-8",
    )
    assert _run_cli(clean_env, {}, "--mode", "production", "--env-file", str(env_file)) == 2


# --- secret masking ------------------------------------------------------


def _env_with_markers() -> dict[str, str]:
    return dict(
        VALID_ENV,
        BOT_TOKEN="123456789:TEST-synthetic-token",
        CP_JWT_SECRET="TEST-super-secret-jwt-marker-0123456789abcdef",
        CP_AGENT_TOKEN="TEST-super-secret-jwt-marker-agent",
        CP_BOOTSTRAP_ADMIN_PASSWORD="TEST-strong-password-01",
        OPENROUTER_API_KEY="sk-or-TEST-super-secret-jwt-marker",
    )


def test_secrets_never_in_stdout_stderr(clean_env, capsys):
    code = _run_cli(clean_env, dict(_env_with_markers(), CP_JWT_SECRET="x"), "--mode", "production")
    assert code == 2
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    for marker in SECRET_MARKERS:
        assert marker not in combined, marker


def test_secrets_never_leak_on_success_path(clean_env, capsys):
    code = _run_cli(clean_env, _env_with_markers(), "--mode", "local")
    assert code == 0
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    for marker in SECRET_MARKERS:
        assert marker not in combined, marker


def test_require_raises_without_values(clean_env):
    for key in KNOWN_KEYS:
        clean_env.delenv(key, raising=False)
    clean_env.setenv("COREBOT_ENV", "production")
    with pytest.raises(vc.ProductionConfigError) as excinfo:
        vc.require_valid_production_config("production")
    text = str(excinfo.value)
    assert "API_ID" in text  # names are fine
    for marker in SECRET_MARKERS:
        assert marker not in text


# --- no caching ----------------------------------------------------------


def test_env_reread_on_every_call(clean_env):
    clean_env.setenv("API_ID", "TEST-not-a-number")
    first = vc.validate("production", None)
    assert "API_ID" in first.failed_names
    clean_env.setenv("API_ID", "7654321")
    second = vc.validate(
        "production",
        dict(
            VALID_ENV,
            CP_INGEST_URL="http://127.0.0.1:8081/ingest/batch",
        ),
    )
    assert second.ok


def test_enforce_gate_noop_unless_production(clean_env):
    clean_env.setenv("COREBOT_ENV", "local")
    assert vc.enforce_production_config() is None
    clean_env.delenv("COREBOT_ENV", raising=False)
    assert vc.enforce_production_config() is None


def test_enforce_gate_raises_on_bad_production(clean_env):
    clean_env.setenv("COREBOT_ENV", "production")
    with pytest.raises(vc.ProductionConfigError):
        vc.enforce_production_config()


def test_enforce_gate_passes_on_good_production(clean_env):
    for key, value in VALID_ENV.items():
        clean_env.setenv(key, value)
    clean_env.setenv("COREBOT_ENV", "production")
    clean_env.setenv("CP_INGEST_URL", "http://127.0.0.1:8081/ingest/batch")
    assert vc.enforce_production_config() is not None


# --- lifespan fail-fast (startup behavior) -------------------------------

TDATA_MODULE = "control_plane.business.tdata_routes"
MAIN_MODULE = "control_plane.main"


@pytest.fixture
def _stub_tdata():
    stub = None
    if TDATA_MODULE not in sys.modules:
        stub = types.ModuleType(TDATA_MODULE)
        stub.router = APIRouter()
        sys.modules[TDATA_MODULE] = stub
    main_present_before = MAIN_MODULE in sys.modules
    yield
    if stub is not None and sys.modules.get(TDATA_MODULE) is stub:
        del sys.modules[TDATA_MODULE]
    if not main_present_before:
        sys.modules.pop(MAIN_MODULE, None)


def _fresh_main():
    sys.modules.pop(MAIN_MODULE, None)
    import control_plane.main as main

    return main


def test_lifespan_rejects_production_defaults_before_db(clean_env, _stub_tdata):
    """Production + unsafe env: lifespan raises before bot_db.connect()."""
    from unittest import mock

    from fastapi.testclient import TestClient

    main = _fresh_main()
    clean_env.setenv("COREBOT_ENV", "production")
    clean_env.setenv("PARSER_EMBEDDED", "1")
    monkeypatch_bootstrap = mock.Mock(side_effect=AssertionError("bootstrap must not run"))
    main.bootstrap_defaults = monkeypatch_bootstrap
    fake_db = mock.Mock()
    fake_db.connect = mock.AsyncMock()
    fake_db.disconnect = mock.AsyncMock()
    main.bot_db = fake_db

    with pytest.raises(vc.ProductionConfigError):
        with TestClient(main.app):
            pass  # pragma: no cover

    fake_db.connect.assert_not_called()
    monkeypatch_bootstrap.assert_not_called()


def test_lifespan_starts_with_valid_production_env(clean_env, _stub_tdata):
    """Production + synthetic valid env: startup proceeds normally."""
    from unittest import mock

    from fastapi.testclient import TestClient

    main = _fresh_main()
    for key, value in VALID_ENV.items():
        clean_env.setenv(key, value)
    clean_env.setenv("COREBOT_ENV", "production")
    clean_env.setenv("PARSER_EMBEDDED", "0")
    clean_env.setenv("CP_INGEST_URL", "http://127.0.0.1:8081/ingest/batch")
    main.bootstrap_defaults = mock.Mock()
    fake_db = mock.Mock()
    fake_db.connect = mock.AsyncMock()
    fake_db.disconnect = mock.AsyncMock()
    main.bot_db = fake_db

    with TestClient(main.app) as client:
        assert client.get("/health").status_code == 200
    main.bootstrap_defaults.assert_called_once()
