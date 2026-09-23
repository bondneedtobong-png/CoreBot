# -*- coding: utf-8 -*-
"""Проверка прокси — настоящий handshake, а не голый TCP-connect.

Мёртвые логины/пароли обязаны давать FAIL: SOCKS5 валидирует
greeting + user/pass (RFC 1929) + CONNECT, HTTP — CONNECT +
Proxy-Authorization. Регрессия на жалобу: «старые прокси, а тест
говорит работают», плюс немой TCP-listener не должен давать OK.
"""

from __future__ import annotations

import socket
import threading

from control_plane.business.proxies import check_proxy_endpoint


def _recv_exact(conn: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise OSError("closed")
        buf += chunk
    return buf


def _serve_once(listener: socket.socket, handler) -> threading.Thread:
    def _run() -> None:
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(5)
            try:
                handler(conn)
            except OSError:
                pass

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


def _listen() -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    s.settimeout(10)
    return s


def _socks5_handler(*, require_auth: bool, user: str, pwd: str, grant: bool):
    def _handle(conn: socket.socket) -> None:
        ver, nmethods = _recv_exact(conn, 2)
        assert ver == 0x05
        methods = _recv_exact(conn, nmethods)
        if require_auth:
            if 0x02 not in methods:
                conn.sendall(bytes((0x05, 0xFF)))
                return
            conn.sendall(bytes((0x05, 0x02)))
            v, ulen = _recv_exact(conn, 2)
            u = _recv_exact(conn, ulen)
            (plen,) = _recv_exact(conn, 1)
            p = _recv_exact(conn, plen)
            if v != 0x01 or u.decode() != user or p.decode() != pwd:
                conn.sendall(bytes((0x01, 0x01)))
                return
            conn.sendall(bytes((0x01, 0x00)))
        else:
            conn.sendall(bytes((0x05, 0x00)))
        req = _recv_exact(conn, 4)
        atyp = req[3]
        if atyp == 0x01:
            _recv_exact(conn, 6)
        elif atyp == 0x03:
            (ln,) = _recv_exact(conn, 1)
            _recv_exact(conn, ln + 2)
        elif atyp == 0x04:
            _recv_exact(conn, 18)
        rep = 0x00 if grant else 0x05
        conn.sendall(bytes((0x05, rep, 0x00, 0x01, 0, 0, 0, 0, 0, 0)))

    return _handle


def _socks5_ok_case(require_auth: bool, grant: bool, **creds):
    s = _listen()
    port = s.getsockname()[1]
    t = _serve_once(
        s, _socks5_handler(require_auth=require_auth, user="u", pwd="p", grant=grant)
    )
    try:
        return check_proxy_endpoint("127.0.0.1", port, "socks5", timeout=5, **creds)
    finally:
        t.join(timeout=10)
        s.close()


def _http_handler(*, code: int):
    def _handle(conn: socket.socket) -> None:
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = conn.recv(1024)
            if not chunk:
                return
            raw += chunk
        first = raw.split(b"\r\n", 1)[0].decode("latin-1")
        assert first.startswith("CONNECT "), first
        conn.sendall(f"HTTP/1.1 {code} X\r\n\r\n".encode("latin-1"))

    return _handle


def _http_ok_case(code: int, **creds):
    s = _listen()
    port = s.getsockname()[1]
    t = _serve_once(s, _http_handler(code=code))
    try:
        return check_proxy_endpoint("127.0.0.1", port, "http", timeout=5, **creds)
    finally:
        t.join(timeout=10)
        s.close()


def _free_port() -> int:
    s = _listen()
    port = s.getsockname()[1]
    s.close()
    return port


def test_closed_port_is_fail_not_ok():
    ok, detail = check_proxy_endpoint("127.0.0.1", _free_port(), "socks5", timeout=3)
    assert ok is False
    assert detail.startswith("tcp failed")


def test_dumb_tcp_listener_is_not_ok():
    """Немой listener (принимает TCP и молчит) — не «рабочий прокси»."""
    s = _listen()
    port = s.getsockname()[1]
    t = _serve_once(s, lambda conn: conn.recv(1))
    try:
        ok, detail = check_proxy_endpoint("127.0.0.1", port, "socks5", timeout=3)
    finally:
        t.join(timeout=10)
        s.close()
    assert ok is False, detail


def test_socks5_no_auth_grant_ok():
    ok, detail = _socks5_ok_case(False, True)
    assert ok is True, detail
    assert "socks5 ok" in detail


def test_socks5_auth_good_creds_ok():
    ok, detail = _socks5_ok_case(True, True, username="u", password="p")
    assert ok is True, detail


def test_socks5_auth_bad_creds_fail():
    """Ключевая регрессия: мёртвый логин/пароль обязан давать FAIL."""
    ok, detail = _socks5_ok_case(True, True, username="u", password="wrong")
    assert ok is False
    assert "auth failed" in detail


def test_socks5_auth_required_but_no_creds_fail():
    ok, detail = _socks5_ok_case(True, True)
    assert ok is False


def test_socks5_connect_refused_fail():
    ok, detail = _socks5_ok_case(False, False)
    assert ok is False
    assert "refused" in detail


def test_http_connect_ok():
    ok, detail = _http_ok_case(200, username="u", password="p")
    assert ok is True, detail


def test_http_407_is_auth_fail():
    ok, detail = _http_ok_case(407, username="u", password="dead")
    assert ok is False
    assert "407" in detail


def test_bad_port_does_not_raise():
    ok, detail = check_proxy_endpoint("127.0.0.1", "notaport", "socks5", timeout=3)
    assert ok is False
    assert detail == "bad port"
