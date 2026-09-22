from __future__ import annotations

import asyncio
from types import SimpleNamespace


def _request(parser_task=None):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(parser_task=parser_task)))


def test_readiness_reports_all_components_healthy(monkeypatch):
    from control_plane import health

    monkeypatch.setattr(health, "probe_control_plane_db", lambda: True)

    async def bot_ok():
        return True

    monkeypatch.setattr(health, "probe_bot_db", bot_ok)
    monkeypatch.setattr(
        health.background_tasks,
        "snapshot",
        lambda: {"active": 1, "active_names": ["mailing-1"], "failed": 0, "failures": []},
    )

    response = asyncio.run(health.readiness(_request()))

    assert response.status_code == 200
    assert response.body == (
        b'{"ok":true,"components":{"control_plane_db":"ok",'
        b'"bot_db":"ok","parser":"disabled","background_tasks":"ok"}}'
    )


def test_readiness_returns_503_for_failed_dependency(monkeypatch):
    from control_plane import health

    monkeypatch.setattr(health, "probe_control_plane_db", lambda: False)

    async def bot_ok():
        return True

    monkeypatch.setattr(health, "probe_bot_db", bot_ok)
    monkeypatch.setattr(
        health.background_tasks,
        "snapshot",
        lambda: {
            "active": 0,
            "active_names": [],
            "failed": 1,
            "failures": [{"name": "mailing-1", "error_type": "RuntimeError"}],
        },
    )

    response = asyncio.run(health.readiness(_request()))

    assert response.status_code == 503
    assert b'"control_plane_db":"unavailable"' in response.body
    assert b'"background_tasks":"failed"' in response.body
    assert b"RuntimeError" not in response.body

def test_bot_db_probe_does_not_require_embedded_parser_engine(monkeypatch):
    from control_plane import health

    class FakeSession:
        def execute(self, _statement):
            return object()

        def close(self):
            return None

    monkeypatch.setattr(health, "BotSession", lambda: FakeSession(), raising=False)
    assert asyncio.run(health.probe_bot_db()) is True
