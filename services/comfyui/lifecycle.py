"""On-demand lifecycle for the user's local ComfyUI installation."""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp


DEFAULT_BASE_URL = "http://127.0.0.1:8188"
DEFAULT_INSTALL_DIR = Path(r"C:\Users\bond\Documents\comfy\ComfyUI")
STARTUP_TIMEOUT_SECONDS = 90.0
_startup_lock = asyncio.Lock()


class ComfyUILifecycleError(RuntimeError):
    """ComfyUI could not be started or did not become ready in time."""


def _validate_default_endpoint(base_url: str) -> tuple[str, int]:
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ComfyUILifecycleError("automatic ComfyUI startup is limited to 127.0.0.1:8188") from exc
    if (
        parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
        or port != 8188 or parsed.path not in ("", "/")
        or parsed.username or parsed.password or parsed.query or parsed.fragment
    ):
        raise ComfyUILifecycleError("automatic ComfyUI startup is limited to 127.0.0.1:8188")
    return parsed.hostname, port


async def _ready(base_url: str) -> bool:
    timeout = aiohttp.ClientTimeout(total=1.5)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            async with session.get(base_url + "/system_stats", allow_redirects=False) as response:
                return response.status == 200
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
        return False


def _launch_comfyui(install_dir: Path) -> None:
    python = install_dir / ".venv" / "Scripts" / "python.exe"
    main = install_dir / "main.py"
    if not python.is_file() or not main.is_file():
        raise ComfyUILifecycleError("ComfyUI install is missing its venv Python or main.py")
    if os.name != "nt":
        raise ComfyUILifecycleError("automatic ComfyUI startup is supported on Windows only")
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
    environment = os.environ.copy()
    # A custom node prints characters outside the Windows cp1251 console page;
    # ComfyUI otherwise exits while formatting its own startup warning.
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    try:
        subprocess.Popen(
            [str(python), str(main), "--listen", "127.0.0.1", "--port", "8188"],
            cwd=str(install_dir), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=flags, close_fds=True, env=environment,
        )
    except OSError as exc:
        raise ComfyUILifecycleError("could not launch the local ComfyUI process") from exc


async def ensure_running(
    base_url: str = DEFAULT_BASE_URL,
    *, install_dir: Path = DEFAULT_INSTALL_DIR,
    timeout_seconds: float = STARTUP_TIMEOUT_SECONDS,
) -> None:
    """Start the fixed local ComfyUI install if absent, then await readiness."""
    _validate_default_endpoint(base_url)
    if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 180:
        raise ValueError("invalid ComfyUI startup timeout")
    async with _startup_lock:
        if await _ready(base_url):
            return
        _launch_comfyui(Path(install_dir))
        deadline = time.monotonic() + float(timeout_seconds)
        while time.monotonic() < deadline:
            await asyncio.sleep(min(0.5, max(0, deadline - time.monotonic())))
            if await _ready(base_url):
                return
    raise ComfyUILifecycleError("ComfyUI did not become ready before the startup deadline")
