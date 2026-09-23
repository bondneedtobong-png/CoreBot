# -*- coding: utf-8 -*-
"""Bulk-импорт прокси: единый парсер + POST /business/proxies/import."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from control_plane.business import proxies as proxies_mod
from control_plane.business.db import get_bot_db
from control_plane.deps import get_current_user, require_operator_write
from database.models import Base, Proxy, ProxyGroup
from utils.proxy_line import parse_proxy_line


# ------------------------- парсер -------------------------


def test_parser_user_format_host_port_at_user_pass():
    p = parse_proxy_line("10.0.0.1:1080@user:pw")
    assert p is not None
    assert (p.host, p.port, p.username, p.password, p.proxy_type) == (
        "10.0.0.1",
        1080,
        "user",
        "pw",
        "socks5",
    )


def test_parser_classic_user_pass_at_host_port():
    p = parse_proxy_line("user:pw@10.0.0.1:1080")
    assert p is not None
    assert (p.host, p.port, p.username, p.password) == ("10.0.0.1", 1080, "user", "pw")


def test_parser_host_port_user_pass():
    p = parse_proxy_line("10.0.0.1:1080:user:pw")
    assert p is not None
    assert (p.host, p.port, p.username, p.password) == ("10.0.0.1", 1080, "user", "pw")


def test_parser_plain_host_port():
    p = parse_proxy_line("example.com:8080")
    assert p is not None
    assert (p.host, p.port, p.username, p.password) == ("example.com", 8080, None, None)


def test_parser_scheme_prefix_sets_type():
    p = parse_proxy_line("http://10.0.0.2:8080:user:pw")
    assert p is not None
    assert p.proxy_type == "http"
    p2 = parse_proxy_line("SOCKS5://10.0.0.3:1080")
    assert p2 is not None and p2.proxy_type == "socks5"


def test_parser_rejects_garbage():
    for bad in [
        "",
        "   ",
        "noport",
        "host:99999",
        "host:0",
        "a:b@c:d:e",
        "user@host:1234",
        "host:1234@",
        "@u:p",
    ]:
        assert parse_proxy_line(bad) is None, bad


def test_parser_disambiguation_ip_wins_user_format():
    # До @ — IP:port → формат host:port@user:pass.
    p = parse_proxy_line("1.2.3.4:1080@log:pas")
    assert p is not None and (p.host, p.username) == ("1.2.3.4", "log")


def test_parser_disambiguation_login_wins_classic():
    # До @ — логин (не IP/hostname) → классика user:pass@host:port.
    p = parse_proxy_line("mylogin:s3cret@1.2.3.4:1080")
    assert p is not None and (p.host, p.username) == ("1.2.3.4", "mylogin")


# ------------------------- эндпоинт -------------------------


def _client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    def _override_db():
        db = maker()
        try:
            yield db
        finally:
            db.close()

    admin = SimpleNamespace(role="super_admin")
    app = FastAPI()
    app.include_router(proxies_mod.router)
    app.dependency_overrides[get_bot_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: admin
    app.dependency_overrides[require_operator_write] = lambda: admin
    return TestClient(app, raise_server_exceptions=False), maker


def test_import_creates_group_and_proxies():
    client, maker = _client()
    r = client.post(
        "/business/proxies/import",
        json={
            "group_name": "КЕНИЯ-989",
            "purpose": "ACCOUNT_RUNTIME",
            "lines": [
                "10.0.0.1:1080@user:pw",
                "user2:pw2@10.0.0.2:1081",
                "10.0.0.3:1082:user3:pw3",
                "10.0.0.4:1083",
                "мусор",
                "",
            ],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added"] == 4
    assert body["bad"] == 1
    assert body["skipped_duplicates"] == 0
    assert body["group_name"] == "КЕНИЯ-989"
    with maker() as db:
        assert db.query(ProxyGroup).count() == 1
        assert db.query(Proxy).count() == 4
        https = [p for p in db.query(Proxy).all() if p.host == "10.0.0.4"]
        assert https and https[0].username is None


def test_import_dedups_and_reuses_group():
    client, _ = _client()
    payload = {
        "group_name": "G",
        "purpose": "ACCOUNT_RUNTIME",
        "lines": ["10.0.0.1:1080@u:p", "10.0.0.1:1080@u:p", "10.0.0.2:1080@u:p"],
    }
    first = client.post("/business/proxies/import", json=payload).json()
    assert first["added"] == 2 and first["skipped_duplicates"] == 1
    second = client.post("/business/proxies/import", json=payload).json()
    assert second["added"] == 0 and second["skipped_duplicates"] == 3
    assert second["group_id"] == first["group_id"]


def test_import_http_type_and_list_shape():
    client, _ = _client()
    client.post(
        "/business/proxies/import",
        json={
            "group_name": "H",
            "purpose": "TDATA_CHECK",
            "lines": ["http://10.0.0.9:8080:u:p"],
        },
    )
    rows = client.get("/business/proxies").json()
    assert len(rows) == 1
    assert rows[0]["proxy_type"] == "http"
    assert rows[0]["group_name"] == "H"
    assert rows[0]["accounts_count"] == 0
    groups = client.get("/business/proxy-groups").json()
    assert groups[0]["purpose"] == "TDATA_CHECK"
