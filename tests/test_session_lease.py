"""The same SQLite session has one owner even in separate application paths."""
from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest

from workers.session_lease import SessionBusyError, SessionLease


def test_session_lease_rejects_second_owner_and_releases(tmp_path):
    path = tmp_path / "account.session"
    first = SessionLease(path).acquire()
    try:
        with pytest.raises(SessionBusyError):
            SessionLease(path).acquire()
    finally:
        first.release()
    SessionLease(path).acquire().release()


def test_session_lease_blocks_other_process(tmp_path):
    path = tmp_path / "shared.session"
    code = (
        "from workers.session_lease import SessionLease, SessionBusyError; "
        "import sys; "
        "lease = SessionLease(sys.argv[1]); "
        "\ntry: lease.acquire()"
        "\nexcept SessionBusyError: sys.exit(17)"
        "\nelse: lease.release()"
    )
    with SessionLease(path):
        result = subprocess.run(
            [sys.executable, "-c", code, str(path)], check=False, capture_output=True
        )
        assert result.returncode == 17, result.stderr.decode(errors="replace")


def test_converter_requires_proxy_before_network(tmp_path, monkeypatch):
    from workers import session_converter

    def unexpected_credentials():
        raise AssertionError("must not contact Telegram without a SOCKS5 proxy")

    monkeypatch.setattr(session_converter, "_get_api_credentials", unexpected_credentials)
    result = asyncio.run(
        session_converter.convert_tdata_to_session(tmp_path, tmp_path / "sessions")
    )
    assert result["success"] is False
    assert "SOCKS5" in result["error"]


def test_parser_rejects_account_without_proxy_before_client_creation(tmp_path, monkeypatch):
    from workers.parser import account_pool

    path = tmp_path / "account.session"
    path.write_bytes(b"session")
    pool = account_pool.RotatingClients(
        [account_pool.AccountSnap(id=1, session_name="account", proxy=None)]
    )
    monkeypatch.setattr(pool, "_session_path", lambda _snap: path)

    def unexpected_client(*_args):
        raise AssertionError("parser must not create an unproxied client")

    monkeypatch.setattr(account_pool, "create_telethon_client", unexpected_client)
    with pytest.raises(RuntimeError, match="no connectable parser accounts"):
        asyncio.run(pool.next_client())
