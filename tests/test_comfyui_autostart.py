"""ComfyUI on-demand startup tests; no real ComfyUI process is started."""

from __future__ import annotations

import asyncio
import os

import pytest

from services.comfyui import lifecycle


def test_existing_server_is_reused(monkeypatch):
    calls = []

    async def ready(_url):
        return True

    monkeypatch.setattr(lifecycle, "_ready", ready)
    monkeypatch.setattr(lifecycle, "_launch_comfyui", lambda _path: calls.append("launch"))
    asyncio.run(lifecycle.ensure_running())
    assert calls == []


def test_missing_install_fails_before_process_launch(tmp_path):
    with pytest.raises(lifecycle.ComfyUILifecycleError, match="missing"):
        lifecycle._launch_comfyui(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="Windows ComfyUI launch contract")
def test_launch_uses_utf8_for_custom_node_startup_logs(tmp_path, monkeypatch):
    python = tmp_path / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    (tmp_path / "main.py").touch()
    captured = {}

    def popen(args, **kwargs):
        captured.update(args=args, **kwargs)

    monkeypatch.setattr(lifecycle.subprocess, "Popen", popen)
    lifecycle._launch_comfyui(tmp_path)
    assert captured["env"]["PYTHONIOENCODING"] == "utf-8"
    assert captured["env"]["PYTHONUTF8"] == "1"
    assert captured["args"][-4:] == ["--listen", "127.0.0.1", "--port", "8188"]


def test_launch_waits_until_server_is_ready(monkeypatch, tmp_path):
    probes = iter([False, False, True])
    launches = []

    async def ready(_url):
        return next(probes)

    monkeypatch.setattr(lifecycle, "_ready", ready)
    monkeypatch.setattr(lifecycle, "_launch_comfyui", lambda path: launches.append(path))
    asyncio.run(lifecycle.ensure_running(install_dir=tmp_path, timeout_seconds=2))
    assert launches == [tmp_path]


def test_startup_timeout_is_bounded(monkeypatch, tmp_path):
    launches = []

    async def ready(_url):
        return False

    monkeypatch.setattr(lifecycle, "_ready", ready)
    monkeypatch.setattr(lifecycle, "_launch_comfyui", lambda path: launches.append(path))
    with pytest.raises(lifecycle.ComfyUILifecycleError, match="deadline"):
        asyncio.run(lifecycle.ensure_running(install_dir=tmp_path, timeout_seconds=0.01))
    assert launches == [tmp_path]


def test_concurrent_requests_only_launch_once(monkeypatch, tmp_path):
    ready_state = False
    launches = []

    async def ready(_url):
        return ready_state

    def launch(path):
        nonlocal ready_state
        launches.append(path)
        ready_state = True

    monkeypatch.setattr(lifecycle, "_ready", ready)
    monkeypatch.setattr(lifecycle, "_launch_comfyui", launch)

    async def run_both():
        await asyncio.gather(
            lifecycle.ensure_running(install_dir=tmp_path, timeout_seconds=2),
            lifecycle.ensure_running(install_dir=tmp_path, timeout_seconds=2),
        )

    asyncio.run(run_both())
    assert launches == [tmp_path]


@pytest.mark.parametrize("url", [
    "http://localhost:8188", "http://127.0.0.1:8189", "http://127.0.0.1:8188/path",
])
def test_startup_is_restricted_to_configured_loopback(url):
    with pytest.raises(lifecycle.ComfyUILifecycleError, match="limited"):
        asyncio.run(lifecycle.ensure_running(url))
