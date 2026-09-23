"""Lifespan bootstrap tests (task 03).

Covers: import without side effects, single startup/shutdown via TestClient,
embedded-parser task lifecycle, and startup-failure cleanup.

NOTE: ``control_plane.business.tdata_routes`` needs ``python-multipart``
(``UploadFile`` param; provided via requirements-dev.txt since task 10, which
fixed the pre-existing tdata failures). It is stubbed here with an empty
router while *this* module runs so lifespan tests stay hermetic and ordering
independent. The stub is removed afterwards, restoring baseline behaviour
for the tdata tests.
"""

from __future__ import annotations

import asyncio
import sys
import types
from unittest import mock

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

TDATA_MODULE = "control_plane.business.tdata_routes"
MAIN_MODULE = "control_plane.main"


@pytest.fixture(scope="module", autouse=True)
def _stub_tdata_routes():
    """Inject an empty-router stub only if the real module is not imported."""
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


def _load_main():
    """Fresh ``control_plane.main`` import (isolated app object per test)."""
    sys.modules.pop(MAIN_MODULE, None)
    import control_plane.main as main

    return main


def _fake_bot_db(**kwargs):
    fake = mock.Mock()
    fake.connect = mock.AsyncMock(**kwargs.pop("connect_kwargs", {}))
    fake.disconnect = mock.AsyncMock(**kwargs.pop("disconnect_kwargs", {}))
    return fake


def test_import_has_no_side_effects():
    """Bare import must not bootstrap users, connect bot_db or start parser."""
    import control_plane.models  # noqa: F401  (real Base; class defs are side-effect free)

    sys.modules.pop(MAIN_MODULE, None)
    with (
        mock.patch("control_plane.database.engine"),
        mock.patch("control_plane.database.SessionLocal") as mock_session,
        mock.patch("database.repository.db") as mock_bot_db,
    ):
        import control_plane.main as main

    try:
        mock_session.assert_not_called()
        mock_bot_db.connect.assert_not_called()
        mock_bot_db.disconnect.assert_not_called()
        assert getattr(main.app.state, "parser_task", None) is None
        # lifespan is wired, legacy hooks are gone
        assert main.app.router.lifespan_context is not None
        assert main.app.router.on_startup == []
        assert main.app.router.on_shutdown == []
        assert main.app.openapi()["info"]["title"] == "CoreBot Control Plane"
    finally:
        # drop the mock-bound module object; later tests reimport clean
        sys.modules.pop(MAIN_MODULE, None)


def test_startup_shutdown_once_parser_disabled(monkeypatch):
    """PARSER_EMBEDDED=0: one startup + one shutdown per TestClient block."""
    main = _load_main()
    monkeypatch.setenv("PARSER_EMBEDDED", "0")
    bootstrap_calls: list[int] = []
    monkeypatch.setattr(main, "bootstrap_defaults", lambda: bootstrap_calls.append(1))
    fake_db = _fake_bot_db()
    monkeypatch.setattr(main, "bot_db", fake_db)

    with TestClient(main.app) as client:
        assert len(bootstrap_calls) == 1
        assert getattr(main.app.state, "parser_task", None) is None
        assert client.get("/health").status_code == 200
        assert client.get("/health").json() == {"ok": True}
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/live").json() == {"ok": True}
    assert len(bootstrap_calls) == 1
    fake_db.connect.assert_not_called()
    fake_db.disconnect.assert_awaited_once()
    assert getattr(main.app.state, "parser_task", None) is None

    # a second block runs the lifespan exactly once more
    with TestClient(main.app):
        pass
    assert len(bootstrap_calls) == 2
    assert fake_db.disconnect.await_count == 2


def test_parser_enabled_task_lifecycle(monkeypatch):
    """PARSER_EMBEDDED=1: named task starts on startup, cancelled on shutdown."""
    main = _load_main()
    monkeypatch.setenv("PARSER_EMBEDDED", "1")
    bootstrap_calls: list[int] = []
    monkeypatch.setattr(main, "bootstrap_defaults", lambda: bootstrap_calls.append(1))
    fake_db = _fake_bot_db()
    monkeypatch.setattr(main, "bot_db", fake_db)

    cancelled: list[bool] = []

    async def fake_parser_loop() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    monkeypatch.setattr(main, "run_parser_forever", fake_parser_loop)

    with TestClient(main.app):
        assert len(bootstrap_calls) == 1
        fake_db.connect.assert_awaited_once()
        task = getattr(main.app.state, "parser_task", None)
        assert task is not None
        assert task.get_name() == "embedded-parser-loop"
        assert not task.done()

    assert cancelled == [True]
    assert task.done()
    fake_db.disconnect.assert_awaited_once()


def test_bootstrap_failure_leaves_no_tasks_or_connections(monkeypatch):
    """bootstrap raising: TestClient raises, no parser task, engine cleaned up."""
    main = _load_main()
    monkeypatch.setenv("PARSER_EMBEDDED", "1")

    def boom() -> None:
        raise RuntimeError("bootstrap failed")

    monkeypatch.setattr(main, "bootstrap_defaults", boom)
    fake_db = _fake_bot_db()
    monkeypatch.setattr(main, "bot_db", fake_db)

    created_names: list[str | None] = []
    real_create_task = asyncio.create_task

    def spy_create_task(coro, **kwargs):
        created_names.append(kwargs.get("name"))
        return real_create_task(coro, **kwargs)

    monkeypatch.setattr(asyncio, "create_task", spy_create_task)

    with pytest.raises(RuntimeError, match="bootstrap failed"):
        with TestClient(main.app):
            pass  # pragma: no cover

    assert "embedded-parser-loop" not in created_names
    fake_db.connect.assert_not_called()
    fake_db.disconnect.assert_awaited_once()
    assert getattr(main.app.state, "parser_task", None) is None


def test_connect_failure_creates_no_parser_task(monkeypatch):
    """bot_db.connect raising: no parser task left behind, disconnect attempted."""
    main = _load_main()
    monkeypatch.setenv("PARSER_EMBEDDED", "1")
    monkeypatch.setattr(main, "bootstrap_defaults", lambda: None)
    fake_db = _fake_bot_db(
        connect_kwargs={"side_effect": ConnectionError("bot db down")}
    )
    monkeypatch.setattr(main, "bot_db", fake_db)

    created_names: list[str | None] = []
    real_create_task = asyncio.create_task

    def spy_create_task(coro, **kwargs):
        created_names.append(kwargs.get("name"))
        return real_create_task(coro, **kwargs)

    monkeypatch.setattr(asyncio, "create_task", spy_create_task)

    with pytest.raises(ConnectionError, match="bot db down"):
        with TestClient(main.app):
            pass  # pragma: no cover

    assert "embedded-parser-loop" not in created_names
    fake_db.connect.assert_awaited_once()
    fake_db.disconnect.assert_awaited_once()
    assert getattr(main.app.state, "parser_task", None) is None
