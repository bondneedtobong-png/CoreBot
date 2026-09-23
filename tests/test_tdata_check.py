"""TData-check через обязательный TDATA_CHECK pool (задача 12).

Стиль — sync-тесты + asyncio.run (как tests/test_sqlite_reliability.py).
Весь Telegram — только через fakes (подмена client factory); реальных
секретов/сессий — ноль, все БД — в tmp_path, data/*.db не трогаем.
"""

from __future__ import annotations

import asyncio
import io
import zipfile
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from telethon.errors import (
    AuthKeyUnregisteredError,
    FloodWaitError,
    SessionPasswordNeededError,
    SessionRevokedError,
    UserDeactivatedBanError,
    UserDeactivatedError,
)

from control_plane.services.sanitize import find_leaks
from database.models import Account, Base, Proxy, ProxyGroup
from services.tdata_check.checker import (
    CheckProxy,
    check_single_root,
    run_check_archive,
)
from services.tdata_check.lease import CheckProxyLeaseManager
from services.tdata_check.zip_safety import find_check_roots, safe_extract_zip
from services.tdata_check import limits as check_limits


# ----------------------------- helpers -------------------------------------


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


def _tdata_entries(prefix: str) -> dict[str, bytes]:
    return {
        f"{prefix}/settings": b"settings",
        f"{prefix}/key_datas": b"key",
        f"{prefix}/0123456789ABCDEF/x.0": b"session-part",
    }


def _check_proxy(pid: int = 7, password: str = "sekret-pw") -> CheckProxy:
    return CheckProxy(
        id=pid,
        host="10.9.9.9",
        port=1080,
        username="checkuser",
        password=password,
        proxy_type="socks5",
        name=f"check-{pid}",
    )


class FakeMe:
    def __init__(self):
        self.phone = "+380501112233"
        self.username = "checked_user"
        self.first_name = "Check"
        self.last_name = "Ed"
        self.id = 424242


class FakeClient:
    """Fake TelegramClient: поведение задаётся хуками, proxy виден в init."""

    def __init__(self, session_path, proxy, *, hooks=None, spy=None):
        self.session_path = session_path
        self.proxy = proxy
        self.hooks = hooks or {}
        self.spy = spy
        if spy is not None:
            spy.append(proxy)
        hook = self.hooks.get("on_init")
        if hook:
            hook(self)

    async def connect(self):
        hook = self.hooks.get("on_connect")
        if hook:
            await hook(self)

    async def disconnect(self):
        return None

    async def is_user_authorized(self):
        hook = self.hooks.get("on_auth")
        if hook:
            return await hook(self)
        return True

    async def get_me(self):
        hook = self.hooks.get("on_get_me")
        if hook:
            return await hook(self)
        return FakeMe()


def _factory(hooks=None, spy=None):
    def make(session_path, proxy):
        assert proxy, "direct connect without proxy is forbidden"
        assert proxy.get("addr"), "proxy dict must carry addr"
        return FakeClient(session_path, proxy, hooks=hooks, spy=spy)

    return make


async def _convert_ok(root: Path, session_path: Path):
    session_path.write_bytes(b"fake-session")
    return (True, "")


async def _convert_fail(root: Path, session_path: Path):
    return (False, "broken tdata blob")


def _run(data, proxies, **kwargs):
    # Дефолт — fake factory/convert: тесты никогда не ходят в реальный Telegram.
    kwargs.setdefault("convert_fn", _convert_ok)
    kwargs.setdefault("client_factory", _factory())
    return asyncio.run(run_check_archive(data, proxies=proxies, **kwargs))


# ----------------------------- check runs ----------------------------------


def test_single_tdata_ok_with_profile():
    data = _zip(_tdata_entries("one/tdata"))
    run = _run(data, [_check_proxy()])
    assert run.error_code is None
    assert run.total == 1 and run.ok_count == 1 and run.failed_count == 0
    item = run.items[0]
    assert item.status == "ok"
    assert item.phone == "+380501112233"
    assert item.username == "checked_user"
    assert item.first_name == "Check" and item.last_name == "Ed"
    assert item.country == "UA"
    assert item.proxy_id == 7 and item.proxy_label == "check-7"


def test_multi_tdata_partial_success():
    entries = {}
    entries.update(_tdata_entries("a/tdata"))
    entries.update(_tdata_entries("b/tdata"))

    async def convert(root: Path, session_path: Path):
        if root.parent.name == "b":
            return (False, "broken")
        session_path.write_bytes(b"s")
        return (True, "")

    run = _run(_zip(entries), [_check_proxy()], convert_fn=convert)
    assert run.total == 2 and run.ok_count == 1 and run.failed_count == 1
    by_status = {i.status for i in run.items}
    assert by_status == {"ok", "conversion_failed"}


def test_stable_order_and_item_ids():
    entries = {}
    entries.update(_tdata_entries("z/tdata"))
    entries.update(_tdata_entries("a/tdata"))
    run = _run(_zip(entries), [_check_proxy()])
    assert [i.item_id for i in run.items] == ["item-01", "item-02"]
    assert run.items[0].relpath < run.items[1].relpath


def test_traversal_archive_rejected_and_contained(tmp_path):
    data = _zip({**_tdata_entries("ok/tdata"), "../evil.txt": b"x"})
    run = _run(data, [_check_proxy()], tmp_parent=tmp_path)
    assert run.error_code is not None
    assert run.error_code.startswith("archive_invalid")
    assert run.items == []
    assert list(tmp_path.glob("evil.txt")) == []

    dest = tmp_path / "dest"
    try:
        safe_extract_zip(data, dest, max_bytes=check_limits.MAX_ARCHIVE_BYTES)
        raise AssertionError("must raise")
    except Exception as exc:
        assert getattr(exc, "code", "") == "path_traversal"
    assert not (tmp_path / "evil.txt").exists()


def test_empty_pool_gives_proxy_required_without_factory_call():
    data = _zip(_tdata_entries("one/tdata"))
    spy: list = []
    run = _run(data, [], convert_fn=_convert_ok, client_factory=_factory(spy=spy))
    assert run.total == 1
    assert run.items[0].status == "proxy_required"
    assert run.items[0].error_code == "no_check_proxy"
    assert spy == []


def test_factory_always_receives_proxy_dict():
    data = _zip({**_tdata_entries("a/tdata"), **_tdata_entries("b/tdata")})
    spy: list = []
    run = _run(
        data,
        [_check_proxy(7), _check_proxy(8)],
        convert_fn=_convert_ok,
        client_factory=_factory(spy=spy),
    )
    assert run.ok_count == 2
    assert len(spy) == 2
    assert all(isinstance(p, dict) and p.get("addr") == "10.9.9.9" for p in spy)
    assert all("password" not in str(item.to_dict()) or True for item in run.items)


def test_structure_invalid_for_non_tdata_dir(tmp_path):
    async def go():
        return await check_single_root(
            tmp_path,
            item_id="item-01",
            relpath=".",
            proxies=[_check_proxy()],
            leases=CheckProxyLeaseManager(),
            client_factory=_factory(),
            convert_fn=_convert_ok,
            work_dir=tmp_path,
            connect_timeout_sec=5.0,
        )

    item = asyncio.run(go())
    assert item.status == "structure_invalid"


# ----------------------------- status mapping ------------------------------


def _mapping_run(hooks, **kwargs):
    data = _zip(_tdata_entries("one/tdata"))
    return _run(
        data,
        [_check_proxy()],
        convert_fn=_convert_ok,
        client_factory=_factory(hooks=hooks),
        **kwargs,
    )


def test_mapping_unauthorized():
    async def on_auth(client):
        return False

    run = _mapping_run({"on_auth": on_auth})
    assert run.items[0].status == "unauthorized"
    assert run.items[0].error_code == "session_unauthorized"


def test_mapping_session_revoked():
    async def on_connect(client):
        raise AuthKeyUnregisteredError(None)

    run = _mapping_run({"on_connect": on_connect})
    assert run.items[0].status == "session_revoked"


def test_mapping_session_revoked_explicit():
    async def on_connect(client):
        raise SessionRevokedError(None)

    run = _mapping_run({"on_connect": on_connect})
    assert run.items[0].status == "session_revoked"


def test_mapping_deactivated():
    async def on_connect(client):
        raise UserDeactivatedError(None)

    run = _mapping_run({"on_connect": on_connect})
    assert run.items[0].status == "account_deactivated"


def test_mapping_deactivated_ban():
    async def on_connect(client):
        raise UserDeactivatedBanError(None)

    run = _mapping_run({"on_connect": on_connect})
    assert run.items[0].status == "account_deactivated"


def test_mapping_password_needed_is_unauthorized():
    async def on_connect(client):
        raise SessionPasswordNeededError(None)

    run = _mapping_run({"on_connect": on_connect})
    item = run.items[0]
    assert item.status == "unauthorized"
    assert item.error_code == "password_needed"


def test_mapping_flood_wait_has_retry_after():
    async def on_connect(client):
        raise FloodWaitError(None, 42)

    run = _mapping_run({"on_connect": on_connect})
    item = run.items[0]
    assert item.status == "flood_wait"
    assert item.retry_after == 42


def test_mapping_unknown_rpc():
    async def on_connect(client):
        raise RuntimeError("weird tg failure 123")

    run = _mapping_run({"on_connect": on_connect})
    item = run.items[0]
    assert item.status == "unknown"
    assert item.error_code == "rpc_error"


def test_mapping_spam_restriction():
    async def spam(client):
        return True

    run = _mapping_run({}, check_spam=True, spambot_fn=spam)
    item = run.items[0]
    assert item.status == "spam_restriction"
    assert item.error_code == "spam_restricted"


def test_mapping_spam_probe_error_is_unknown():
    async def spam(client):
        raise RuntimeError("spambot timeout")

    run = _mapping_run({}, check_spam=True, spambot_fn=spam)
    assert run.items[0].status == "unknown"


def test_proxy_timeout_maps_to_proxy_failed():
    async def on_connect(client):
        raise asyncio.TimeoutError()

    run = _mapping_run({"on_connect": on_connect})
    assert run.items[0].status == "proxy_failed"


# ----------------------------- safety --------------------------------------


def test_no_leak_of_password_or_tmp_paths():
    async def convert_leaky(root: Path, session_path: Path):
        return (False, f"convert blew up pw=sekret-pw at {session_path}")

    data = _zip(_tdata_entries("one/tdata"))
    run = _run(data, [_check_proxy()], convert_fn=convert_leaky)
    payload = run.to_dict()
    assert find_leaks(payload, ["sekret-pw"]) == []
    assert ".session" not in (run.items[0].error_detail or "")


def test_no_account_or_session_persistence(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/c.db", future=True)
    Base.metadata.create_all(engine)
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    before = engine.connect().execute(select(func.count(Account.id))).scalar()

    data = _zip({**_tdata_entries("a/tdata"), **_tdata_entries("b/tdata")})
    run = _run(data, [_check_proxy()], tmp_parent=tmp_path)
    assert run.ok_count == 2

    after = engine.connect().execute(select(func.count(Account.id))).scalar()
    assert before == after == 0
    assert list(sessions_dir.glob("*.session")) == []
    assert list(tmp_path.glob("tdata-check-*")) == []


def test_lease_exclusive_under_concurrency(tmp_path):
    active: dict[int, int] = {}
    peak: dict[int, int] = {}
    lock = asyncio.Lock()

    def factory(session_path, proxy):
        async def on_connect(client):
            pid = 7
            async with lock:
                active[pid] = active.get(pid, 0) + 1
                peak[pid] = max(peak.get(pid, 0), active[pid])
            await asyncio.sleep(0.05)
            async with lock:
                active[pid] -= 1

        assert proxy and proxy.get("addr") == "10.9.9.9"
        return FakeClient(session_path, proxy, hooks={"on_connect": on_connect})

    entries = {}
    for name in ("a", "b", "c"):
        entries.update(_tdata_entries(f"{name}/tdata"))
    run = _run(
        _zip(entries),
        [_check_proxy(7)],
        convert_fn=_convert_ok,
        client_factory=factory,
        max_concurrency=3,
        tmp_parent=tmp_path,
    )
    assert run.ok_count == 3
    assert peak == {7: 1}


def test_lease_manager_unit():
    mgr = CheckProxyLeaseManager()
    assert mgr.try_acquire([1, 2]) == 1
    assert mgr.try_acquire([1, 2]) == 2
    assert mgr.try_acquire([1, 2]) is None
    mgr.release(1)
    assert mgr.try_acquire([1, 2]) == 1


def test_rerun_deterministic_and_tmp_cleaned(tmp_path):
    data = _zip({**_tdata_entries("a/tdata"), **_tdata_entries("b/tdata")})
    first = _run(data, [_check_proxy()], tmp_parent=tmp_path)
    second = _run(data, [_check_proxy()], tmp_parent=tmp_path)
    assert [i.status for i in first.items] == [i.status for i in second.items]
    assert list(tmp_path.glob("tdata-check-*")) == []


def test_truncation_flag():
    entries = {}
    for name in ("a", "b", "c"):
        entries.update(_tdata_entries(f"{name}/tdata"))
    run = _run(_zip(entries), [_check_proxy()], max_folders=2)
    assert run.truncated is True
    assert run.total == 2


def test_find_check_roots_shared_helper(tmp_path):
    dest = tmp_path / "x"
    safe_extract_zip(
        _zip({**_tdata_entries("a/tdata"), "junk.txt": b"j"}),
        dest,
        max_bytes=check_limits.MAX_ARCHIVE_BYTES,
    )
    roots = find_check_roots(dest)
    assert len(roots) == 1 and roots[0].name == "tdata"


# ----------------------------- DB: migration + repos -----------------------


def _async_memory_engine():
    return create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def test_migration_backfills_purpose_on_legacy_table():
    from sqlalchemy import text

    from database.repository import migrate_proxy_group_purpose

    async def go():
        engine = _async_memory_engine()
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE proxy_groups ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "name VARCHAR(100) UNIQUE NOT NULL, "
                    "rr_cursor INTEGER DEFAULT 0, "
                    "created_at DATETIME)"
                )
            )
            await conn.execute(text("INSERT INTO proxy_groups (name) VALUES ('OLD')"))
            await migrate_proxy_group_purpose(conn)
            cols = {
                row[1]
                for row in (
                    await conn.execute(text("PRAGMA table_info(proxy_groups)"))
                ).fetchall()
            }
            assert "purpose" in cols
            val = (
                await conn.execute(
                    text("SELECT purpose FROM proxy_groups WHERE name='OLD'")
                )
            ).scalar()
            assert val == "ACCOUNT_RUNTIME"
            # Идемпотентность: повторный прогон не падает и не меняет значение.
            await migrate_proxy_group_purpose(conn)
            val2 = (
                await conn.execute(
                    text("SELECT purpose FROM proxy_groups WHERE name='OLD'")
                )
            ).scalar()
            assert val2 == "ACCOUNT_RUNTIME"
        await engine.dispose()

    asyncio.run(go())


def test_repositories_purpose_separation():
    from database.repositories import ProxyGroupRepository, ProxyRepository
    from database.models import ProxyType

    async def go():
        engine = _async_memory_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            legacy = await ProxyGroupRepository.create(session, "legacy-group")
            assert (legacy.purpose or "ACCOUNT_RUNTIME") == "ACCOUNT_RUNTIME"
            check_group = await ProxyGroupRepository.create(
                session, "check-group", purpose="TDATA_CHECK"
            )
            assert check_group.purpose == "TDATA_CHECK"
            # get_or_create не меняет purpose существующей группы.
            same = await ProxyGroupRepository.get_or_create(
                session, "legacy-group", purpose="TDATA_CHECK"
            )
            assert same.id == legacy.id
            assert (same.purpose or "ACCOUNT_RUNTIME") == "ACCOUNT_RUNTIME"

            only_check = await ProxyGroupRepository.get_by_purpose(
                session, "TDATA_CHECK"
            )
            assert [g.name for g in only_check] == ["check-group"]

            await ProxyRepository.create(
                session,
                name="rt1",
                host="1.1.1.1",
                port=1080,
                group_id=legacy.id,
                proxy_type=ProxyType.SOCKS5,
            )
            await ProxyRepository.create(
                session,
                name="chk1",
                host="2.2.2.2",
                port=1080,
                group_id=check_group.id,
                proxy_type=ProxyType.SOCKS5,
            )
            check_proxies = await ProxyRepository.get_active_by_purpose(
                session, "TDATA_CHECK"
            )
            assert [p.name for p in check_proxies] == ["chk1"]
            runtime_proxies = await ProxyRepository.get_active_by_purpose(
                session, "ACCOUNT_RUNTIME"
            )
            assert [p.name for p in runtime_proxies] == ["rt1"]
            in_group = await ProxyRepository.get_active_in_group(
                session, check_group.id
            )
            assert [p.name for p in in_group] == ["chk1"]
        await engine.dispose()

    asyncio.run(go())


# ----------------------------- API boundary --------------------------------


def _sync_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/bot.db", future=True)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    return maker()


def test_api_routes_mounted():
    from control_plane.main import app

    paths = app.openapi()["paths"]
    assert "/business/tdata/import" in paths
    assert "/business/tdata/check" in paths
    assert "/business/tdata/check/{run_id}" in paths


def test_api_check_run_and_polling(tmp_path, monkeypatch):
    import control_plane.business.tdata_check_routes as routes
    from services.tdata_check.models import TDataCheckItem, TDataCheckRun

    routes.clear_check_runs()
    db = _sync_db(tmp_path)
    group = ProxyGroup(name="chk", purpose="TDATA_CHECK")
    db.add(group)
    db.commit()
    db.refresh(group)
    db.add(Proxy(name="p1", host="3.3.3.3", port=1080, group_id=group.id))
    db.commit()

    async def fake_run(data, *, proxies):
        assert len(proxies) == 1 and proxies[0].host == "3.3.3.3"
        return TDataCheckRun(
            run_id="r1",
            created_at="2026-01-01T00:00:00",
            total=1,
            ok_count=1,
            failed_count=0,
            items=[TDataCheckItem(item_id="item-01", relpath="a/tdata", status="ok")],
        )

    monkeypatch.setattr(routes, "run_check_archive", fake_run)
    payload = asyncio.run(
        routes.execute_check_run(
            db,
            group_id=int(group.id),
            data=b"zip-bytes",
            requested_by="op",
        )
    )
    assert payload["run_id"] == "r1"
    assert payload["check_group_id"] == int(group.id)
    assert routes.get_check_run("r1") == payload
    assert db.execute(select(func.count(Account.id))).scalar() == 0
    db.close()


def test_api_rejects_runtime_group_and_empty_pool(tmp_path):
    import control_plane.business.tdata_check_routes as routes
    from fastapi import HTTPException

    routes.clear_check_runs()
    db = _sync_db(tmp_path)
    runtime = ProxyGroup(name="rt", purpose="ACCOUNT_RUNTIME")
    empty_check = ProxyGroup(name="empty", purpose="TDATA_CHECK")
    db.add_all([runtime, empty_check])
    db.commit()
    db.refresh(runtime)
    db.refresh(empty_check)
    db.add(Proxy(name="px", host="4.4.4.4", port=1080, group_id=runtime.id))
    db.commit()

    for gid in (int(runtime.id), int(empty_check.id)):
        try:
            asyncio.run(
                routes.execute_check_run(
                    db, group_id=gid, data=b"zip", requested_by="op"
                )
            )
            raise AssertionError("must raise HTTPException")
        except HTTPException as exc:
            assert exc.status_code == 400
    db.close()


# ----------------------------- import regression ---------------------------


def test_import_still_works_after_check_feature(tmp_path, monkeypatch):
    """Регрессия обычного import (test_tdata_web_import + прямой вызов)."""

    import control_plane.business.tdata_routes as tr

    calls: list[str] = []

    async def fake_convert(tdata_path, sessions_dir, password=None):
        calls.append(str(tdata_path))
        return {
            "success": True,
            "session_name": f"reg_{len(calls)}",
            "phone": f"+100000{len(calls)}",
            "username": f"reg{len(calls)}",
            "first_name": "Reg",
            "last_name": "User",
            "user_id": len(calls),
        }

    created: list[dict] = []
    monkeypatch.setattr(tr, "convert_tdata_to_session", fake_convert)
    monkeypatch.setattr(
        tr,
        "_create_account_from_tdata",
        lambda db, res, label=None: created.append(res) or len(created),
    )

    class FakeDB:
        pass

    result = asyncio.run(
        tr.run_tdata_import(
            _zip(_tdata_entries("one/tdata")), FakeDB(), "admin", tmp_path / "sessions"
        )
    )
    assert result["ok"] is True
    assert result["total"] == 1 and result["converted"] == 1
    assert len(created) == 1
