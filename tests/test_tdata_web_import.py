"""TData import web API tests."""
from __future__ import annotations

import asyncio
import io
import zipfile
from pathlib import Path

from control_plane.business.tdata import find_tdata_roots


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


def _extract(data: bytes) -> Path:
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="tdata-test-"))
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        z.extractall(tmp)
    return tmp


def test_find_tdata_roots_finds_direct_and_nested():
    root = _extract(_zip({
        "plain/tdata/settings": b"settings",
        "plain/tdata/key_datas": b"key",
        "nested/outer/tdata/settings": b"settings",
        "nested/outer/tdata/key_datas": b"key",
        "empty.txt": b"",
    }))
    roots = find_tdata_roots(root)
    assert {r.name for r in roots} == {"tdata"}


def test_find_tdata_roots_ignores_incomplete_tdata():
    root = _extract(_zip({
        "broken/tdata/settings": b"settings",
        "broken/other.txt": b"",
    }))
    assert find_tdata_roots(root) == []


def test_import_route_is_mounted():
    from control_plane.main import app

    openapi = app.openapi()
    assert "/business/tdata/import" in openapi["paths"]


def test_run_tdata_import_converts_and_reports(monkeypatch, tmp_path):
    from control_plane.business.tdata_routes import run_tdata_import
    import control_plane.business.tdata_routes as tr

    archive = tmp_path / "upload.zip"
    entries = {
        "a/tdata/settings": b"s",
        "a/tdata/key_datas": b"k",
        "b/tdata/settings": b"s",
        "b/tdata/key_datas": b"k",
    }
    archive.write_bytes(_zip(entries))

    calls: list[str] = []

    async def fake_convert(tdata_path, sessions_dir, password=None):
        calls.append(Path(tdata_path).name)
        return {
            "success": True,
            "session_name": f"sess_{len(calls)}",
            "phone": f"+100000{len(calls)}",
            "username": f"user{len(calls)}",
            "first_name": "Test",
            "last_name": "User",
            "user_id": len(calls),
        }

    monkeypatch.setattr(tr, "convert_tdata_to_session", fake_convert)
    monkeypatch.setattr(tr, "_create_account_from_tdata", lambda db, res, label=None: len(calls))

    class FakeDB:
        pass

    result = asyncio.run(run_tdata_import(archive.read_bytes(), FakeDB(), "admin", tmp_path / "sessions"))

    assert result["ok"] is True
    assert result["total"] == 2
    assert result["converted"] == 2
    assert result["failed"] == 0
    assert len(calls) == 2
