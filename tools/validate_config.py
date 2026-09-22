"""Shared CoreBot configuration validator (task 04).

Single entry point validating bot + Control Plane configuration::

    python -m tools.validate_config --mode local|production

Exit codes: 0 = ok (warnings allowed), 1 = usage error, 2 = config error.

Design notes:

* Stdlib only, so the VPS installer can run it with system ``python3``
  before the virtualenv exists.
* Environment is re-read from ``os.environ`` on every call (never cached
  at import), so tests using ``monkeypatch.setenv`` see fresh values.
* Secret values are NEVER printed. Messages reference variable *names*
  plus lengths/masks only (see ``SECRET_NAMES``).
* ``local`` mode turns production failures into warnings (exit 0);
  ``production`` mode fails fast (exit 2).
* The panel bind itself (``uvicorn --host`` / systemd ``ExecStart``) is not
  an env var, so it is enforced by documentation + ``ExecStartPre`` hooks,
  not invented env checks. The validator only rejects non-loopback hosts
  where an env var actually carries one (e.g. ``CP_INGEST_URL``).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import urlparse

MODES = ("local", "production")

#: Values that must never appear in output. Checked by tests via capsys.
SECRET_NAMES = frozenset(
    {
        "API_ID",
        "API_HASH",
        "BOT_TOKEN",
        "CP_JWT_SECRET",
        "CP_AGENT_TOKEN",
        "CP_BOOTSTRAP_ADMIN_PASSWORD",
        "CP_TELEGRAM_BOT_TOKEN",
        "OPENROUTER_API_KEY",
        "OPENROUTER_KEY_ENCRYPTION_KEY",
        "CONTROL_BOT_PROXY_USERNAME",
        "CONTROL_BOT_PROXY_PASSWORD",
    }
)

#: Placeholder/default secrets rejected in production (case-insensitive).
UNSAFE_SECRET_MARKERS = frozenset(
    {
        "change-me-in-production",  # control_plane/config.py default
        "change-me-please",  # .env.example placeholder
        "change-me",
        "admin123",  # control_plane/config.py default
        "admin",
        "password",
    }
)

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

_TRUTHY = {"1", "true", "yes", "on"}
_API_HASH_RE = re.compile(r"[0-9a-fA-F]{32}\Z")


class ProductionConfigError(RuntimeError):
    """Raised when production configuration is invalid (names only)."""


@dataclass
class ValidationResult:
    mode: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def failed_names(self) -> list[str]:
        """Variable names from error messages (``NAME: text`` format)."""
        names = []
        for msg in self.errors:
            name = msg.split(":", 1)[0].strip()
            if name:
                names.append(name)
        return names


class _Collector:
    """Collects findings; production failures, local warnings."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.result = ValidationResult(mode=mode)

    def fail(self, name: str, text: str) -> None:
        msg = f"{name}: {text}"
        if self.mode == "production":
            self.result.errors.append(msg)
        else:
            self.result.warnings.append(msg)

    def note(self, name: str, text: str) -> None:
        self.result.warnings.append(f"{name}: {text}")


def resolve_mode(explicit: str | None, env: Mapping[str, str]) -> str:
    """Resolve ``local`` | ``production`` (explicit arg wins, then env)."""
    raw = explicit if explicit is not None else env.get("COREBOT_ENV", "local")
    mode = (raw or "local").strip().lower() or "local"
    if mode not in MODES:
        raise ValueError(f"unknown mode {raw!r}: expected one of {', '.join(MODES)}")
    return mode


def load_env_file(path: str) -> dict[str, str]:
    """Parse a dotenv-style file (stdlib only, no expansion)."""
    values: dict[str, str] = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh.read().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            if stripped.lower().startswith("export "):
                stripped = stripped[7:].lstrip()
            key, _, value = stripped.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if key:
                values[key] = value
    return values


def _is_enabled(value: str) -> bool:
    return value.strip().lower() in _TRUTHY


def _looks_like_bot_token(value: str) -> bool:
    head, sep, tail = value.partition(":")
    return bool(sep) and head.strip().isdigit() and bool(tail.strip())


def _check_int(
    col: _Collector,
    env: Mapping[str, str],
    name: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
    allow_negative: bool = False,
) -> None:
    raw = env.get(name, "")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return  # unset: code defaults apply
    text = str(raw).strip()
    try:
        number = int(text)
    except ValueError:
        col.fail(name, "must be an integer")
        return
    if not allow_negative and number < 0 and minimum is None:
        col.fail(name, "must be an integer >= 0")
        return
    if minimum is not None and number < minimum:
        col.fail(name, f"must be an integer >= {minimum}")
    elif maximum is not None and number > maximum:
        col.fail(name, f"must be an integer in {minimum}..{maximum}")


def _check_float(
    col: _Collector,
    env: Mapping[str, str],
    name: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> None:
    raw = env.get(name, "")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return
    text = str(raw).strip()
    try:
        number = float(text)
    except ValueError:
        col.fail(name, "must be a number")
        return
    if minimum is not None and number < minimum:
        bound = f"> {minimum}" if minimum == 0 else f">= {minimum}"
        col.fail(name, f"must be a number {bound}")
    elif maximum is not None and number > maximum:
        col.fail(name, f"must be a number <= {maximum}")


def _check_jwt_secret(col: _Collector, env: Mapping[str, str]) -> None:
    value = env.get("CP_JWT_SECRET", "")
    if not value or not value.strip():
        col.fail("CP_JWT_SECRET", "is missing or empty; generate with `openssl rand -hex 32`")
        return
    secret = value.strip()
    lowered = secret.lower()
    if lowered in UNSAFE_SECRET_MARKERS or lowered.startswith("change-me"):
        col.fail(
            "CP_JWT_SECRET",
            "uses a forbidden placeholder/default; generate with `openssl rand -hex 32`",
        )
    if len(secret) < 32:
        # Length only, never the value.
        col.fail(
            "CP_JWT_SECRET",
            f"is too short ({len(secret)} chars, need >= 32, recommend 64 hex via `openssl rand -hex 32`)",
        )


def _check_admin_password(col: _Collector, env: Mapping[str, str]) -> None:
    value = env.get("CP_BOOTSTRAP_ADMIN_PASSWORD", "")
    if not value or not value.strip():
        col.fail("CP_BOOTSTRAP_ADMIN_PASSWORD", "is missing or empty; set a strong password")
        return
    password = value.strip()
    lowered = password.lower()
    if lowered in UNSAFE_SECRET_MARKERS or lowered.startswith("change-me"):
        col.fail("CP_BOOTSTRAP_ADMIN_PASSWORD", "uses a forbidden placeholder/default; set a strong password")
    if len(password) < 12:
        col.fail(
            "CP_BOOTSTRAP_ADMIN_PASSWORD",
            f"is too short ({len(password)} chars, need >= 12)",
        )


def _check_db_urls(col: _Collector, env: Mapping[str, str]) -> None:
    specs = (
        ("DATABASE_URL", "sqlite+aiosqlite:////absolute/path/corebot.db"),
        ("CP_DATABASE_URL", "sqlite:////absolute/path/control_plane.db"),
        ("BOT_DATABASE_URL", "sqlite:////absolute/path/corebot.db"),
    )
    for name, hint in specs:
        value = (env.get(name, "") or "").strip()
        if not value:
            col.fail(name, f"is missing or empty; expected like {hint}")
            continue
        prefix = "sqlite+aiosqlite:////" if name == "DATABASE_URL" else "sqlite:////"
        if not value.startswith(prefix):
            col.fail(name, f"must use an absolute SQLite path like {hint}")


def _check_ingest_url(col: _Collector, env: Mapping[str, str]) -> None:
    value = (env.get("CP_INGEST_URL", "") or "").strip()
    if not value:
        return  # unset: code default is loopback
    if "0.0.0.0" in value:
        col.fail("CP_INGEST_URL", "must stay loopback (127.0.0.1), never 0.0.0.0")
        return
    try:
        parsed = urlparse(value)
    except ValueError:
        col.fail("CP_INGEST_URL", "is malformed; expected http://127.0.0.1:8081/ingest/batch")
        return
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        col.fail("CP_INGEST_URL", "is malformed; expected http://127.0.0.1:8081/ingest/batch")
        return
    if parsed.hostname not in LOOPBACK_HOSTS:
        col.fail("CP_INGEST_URL", "must stay loopback (127.0.0.1), never a public host")
    if parsed.path != "/ingest/batch":
        col.fail("CP_INGEST_URL", "must use path /ingest/batch")


def _check_public_hosts(col: _Collector, env: Mapping[str, str]) -> None:
    """Reject 0.0.0.0 wherever an env var carries a host/URL."""
    for name in ("OPENROUTER_BASE_URL", "OPENROUTER_HTTP_REFERER", "CONTROL_BOT_PROXY_HOST"):
        value = (env.get(name, "") or "").strip()
        if value and "0.0.0.0" in value:
            col.fail(name, "must not reference 0.0.0.0")


def validate(mode: str | None = None, env: Mapping[str, str] | None = None) -> ValidationResult:
    """Validate bot + Control Plane config. Env re-read on every call."""
    snapshot: Mapping[str, str] = dict(os.environ) if env is None else dict(env)
    active_mode = resolve_mode(mode, snapshot)
    col = _Collector(active_mode)

    # --- Telegram credentials (CONFIG_CONTRACT: mandatory) ---
    api_id = (snapshot.get("API_ID", "") or "").strip()
    if not api_id:
        col.fail("API_ID", "is missing or empty; set the numeric id from my.telegram.org")
    elif not api_id.isdigit() or int(api_id) <= 0:
        col.fail("API_ID", "must be a positive integer from my.telegram.org")

    api_hash = (snapshot.get("API_HASH", "") or "").strip()
    if not api_hash:
        col.fail("API_HASH", "is missing or empty; set the 32-hex value from my.telegram.org")
    elif not _API_HASH_RE.match(api_hash):
        col.fail("API_HASH", "must be 32 hex characters from my.telegram.org")

    bot_token = (snapshot.get("BOT_TOKEN", "") or "").strip()
    if not bot_token:
        col.fail("BOT_TOKEN", "is missing or empty; set the @BotFather token")
    elif not _looks_like_bot_token(bot_token):
        col.fail("BOT_TOKEN", "is malformed; expected <id>:<hash> from @BotFather")

    owner_id = (snapshot.get("OWNER_ID", "") or "").strip()
    if not owner_id or owner_id == "0":
        col.fail("OWNER_ID", "is missing or empty; set the numeric Telegram id of the owner")
    elif not owner_id.isdigit() or int(owner_id) <= 0:
        col.fail("OWNER_ID", "must be a positive numeric Telegram id")

    # --- Databases ---
    _check_db_urls(col, snapshot)

    # --- Control Plane secrets ---
    _check_jwt_secret(col, snapshot)
    admin_user = (snapshot.get("CP_BOOTSTRAP_ADMIN_USERNAME", "") or "").strip()
    if not admin_user:
        col.fail("CP_BOOTSTRAP_ADMIN_USERNAME", "is missing or empty")
    _check_admin_password(col, snapshot)

    # --- Agent telemetry ---
    _check_ingest_url(col, snapshot)
    if _is_enabled(snapshot.get("CP_AGENT_ENABLED", "0") or "0"):
        if not (snapshot.get("CP_AGENT_TOKEN", "") or "").strip():
            col.fail("CP_AGENT_TOKEN", "is required when CP_AGENT_ENABLED=1")
    agent_name = (snapshot.get("CP_AGENT_NAME", "") or "").strip()
    if "CP_AGENT_NAME" in snapshot and not agent_name:
        col.fail("CP_AGENT_NAME", "must be a non-empty string")

    # --- Parser mode (INSTANCE_CONTRACT: exactly one loop) ---
    parser_mode = (snapshot.get("PARSER_EMBEDDED", "") or "").strip()
    if parser_mode and parser_mode not in ("0", "1"):
        col.fail("PARSER_EMBEDDED", "must be exactly 0 or 1")

    # --- Logging / numerics (CONFIG_CONTRACT ranges) ---
    log_level = (snapshot.get("LOG_LEVEL", "") or "").strip()
    if log_level and log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        col.fail("LOG_LEVEL", "must be one of DEBUG, INFO, WARNING, ERROR")
    _check_int(col, snapshot, "DEFAULT_DELAY_BETWEEN_MESSAGES", minimum=0)
    _check_int(col, snapshot, "DEFAULT_DELAY_BETWEEN_ACCOUNTS", minimum=0)
    _check_int(col, snapshot, "MAX_RETRIES_ON_FLOOD", minimum=0)
    _check_float(col, snapshot, "PARSER_POLL_SEC", minimum=0)
    _check_float(col, snapshot, "CP_BUSINESS_STREAM_INTERVAL", minimum=0)
    _check_int(col, snapshot, "CP_BUSINESS_STREAM_BATCH", minimum=1)
    _check_int(col, snapshot, "NEURO_MAX_CONCURRENT", minimum=1)
    _check_int(col, snapshot, "NEURO_OPENROUTER_MAX_RETRIES", minimum=0)
    _check_float(col, snapshot, "NEURO_OPENROUTER_RETRY_BASE_SEC", minimum=0)
    _check_int(col, snapshot, "NEURO_MAX_TOKENS", minimum=0)
    _check_float(col, snapshot, "NEURO_DEFAULT_TEMPERATURE", minimum=0, maximum=2)
    _check_float(col, snapshot, "NEURO_DEFAULT_TOP_P", minimum=0, maximum=1)
    _check_int(col, snapshot, "NEURO_DEFAULT_TOP_K", minimum=0)
    _check_float(col, snapshot, "NEURO_DEFAULT_FREQUENCY_PENALTY", minimum=-2, maximum=2)
    _check_float(col, snapshot, "NEURO_DEFAULT_PRESENCE_PENALTY", minimum=-2, maximum=2)
    _check_float(col, snapshot, "NEURO_DEFAULT_REPETITION_PENALTY", minimum=0)
    _check_float(col, snapshot, "NEURO_DEFAULT_MIN_P", minimum=0, maximum=1)
    _check_float(col, snapshot, "NEURO_DEFAULT_TOP_A", minimum=0, maximum=1)
    _check_int(col, snapshot, "MAILING_BASE_UTC_OFFSET", minimum=-12, maximum=14)

    # --- Control Bot proxy ---
    proxy_type = (snapshot.get("CONTROL_BOT_PROXY_TYPE", "") or "").strip().lower()
    if proxy_type:
        if proxy_type not in ("socks5", "http"):
            col.fail("CONTROL_BOT_PROXY_TYPE", "must be empty, socks5 or http")
        else:
            if not (snapshot.get("CONTROL_BOT_PROXY_HOST", "") or "").strip():
                col.fail("CONTROL_BOT_PROXY_HOST", "is required when CONTROL_BOT_PROXY_TYPE is set")
            _check_int(col, snapshot, "CONTROL_BOT_PROXY_PORT", minimum=1, maximum=65535)
            if not (snapshot.get("CONTROL_BOT_PROXY_PORT", "") or "").strip():
                col.fail("CONTROL_BOT_PROXY_PORT", "is required when CONTROL_BOT_PROXY_TYPE is set")

    # --- Control Plane Telegram alerts ---
    cp_tg_token = (snapshot.get("CP_TELEGRAM_BOT_TOKEN", "") or "").strip()
    if cp_tg_token:
        if not _looks_like_bot_token(cp_tg_token):
            col.fail("CP_TELEGRAM_BOT_TOKEN", "is malformed; expected <id>:<hash> or empty")
        chat_id = (snapshot.get("CP_TELEGRAM_ALERT_CHAT_ID", "") or "").strip()
        if not chat_id:
            col.fail("CP_TELEGRAM_ALERT_CHAT_ID", "is required when CP_TELEGRAM_BOT_TOKEN is set")
        else:
            neg = chat_id.startswith("-")
            if not chat_id.lstrip("-").isdigit() or (neg and len(chat_id) < 2):
                col.fail("CP_TELEGRAM_ALERT_CHAT_ID", "must be a numeric chat id")

    # --- OpenRouter ---
    base_url = (snapshot.get("OPENROUTER_BASE_URL", "") or "").strip()
    if base_url:
        if "0.0.0.0" in base_url:
            col.fail("OPENROUTER_BASE_URL", "must not reference 0.0.0.0")
        else:
            try:
                parsed = urlparse(base_url)
            except ValueError:
                parsed = None
            if parsed is None or parsed.scheme != "https" or not parsed.hostname:
                col.fail("OPENROUTER_BASE_URL", "must be an https URL")
    _check_public_hosts(col, snapshot)

    return col.result


def require_valid_production_config(mode: str | None = "production") -> ValidationResult:
    """Validate and raise :class:`ProductionConfigError` on failure.

    The message lists variable *names* only, never values.
    """
    result = validate(mode or "production")
    if not result.ok:
        names = ", ".join(result.failed_names) or "unknown"
        raise ProductionConfigError(
            f"production configuration invalid ({len(result.errors)} error(s): {names}); "
            "run `python -m tools.validate_config --mode production` for details"
        )
    return result


def enforce_production_config() -> ValidationResult | None:
    """Fail-fast gate for service entry points.

    No-op unless ``COREBOT_ENV=production``. Raises
    :class:`ProductionConfigError` before any DB/parser connection.
    Re-reads the environment on every call.
    """
    if (os.getenv("COREBOT_ENV", "local") or "local").strip().lower() != "production":
        return None
    return require_valid_production_config("production")


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # usage errors -> exit 1
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="tools.validate_config",
        description="Validate CoreBot bot + Control Plane configuration.",
    )
    parser.add_argument(
        "--mode",
        default=None,
        help="local|production (default: $COREBOT_ENV, fallback local)",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="dotenv-style file whose values override process env for this run",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    snapshot: dict[str, str] = dict(os.environ)
    if args.env_file:
        if not os.path.isfile(args.env_file):
            print(f"tools.validate_config: error: env file not found: {args.env_file}", file=sys.stderr)
            return 1
        try:
            snapshot.update(load_env_file(args.env_file))
        except OSError as exc:
            print(f"tools.validate_config: error: cannot read env file: {exc}", file=sys.stderr)
            return 1

    try:
        mode = resolve_mode(args.mode, snapshot)
    except ValueError as exc:
        print(f"tools.validate_config: error: {exc}", file=sys.stderr)
        return 1

    result = validate(mode, snapshot)
    for msg in result.errors:
        print(f"ERROR: {msg}")
    for msg in result.warnings:
        print(f"WARNING: {msg}")
    print(
        f"Checked bot + Control Plane config in {mode} mode: "
        f"{len(result.errors)} error(s), {len(result.warnings)} warning(s)."
    )
    return 2 if result.errors else 0


if __name__ == "__main__":
    sys.exit(main())
