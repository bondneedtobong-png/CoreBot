"""Reject obviously oversized faces in a template-based generated photo."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from services.comfyui.lifecycle import DEFAULT_INSTALL_DIR


_WORKER = Path(__file__).with_name("scene_quality_worker.py")
_PYTHON = DEFAULT_INSTALL_DIR / ".venv" / "Scripts" / "python.exe"
_MODEL = DEFAULT_INSTALL_DIR / "models" / "ultralytics" / "bbox" / "face_yolov8m.pt"


def _oversized_face(source: list[float] | None, generated: list[float] | None) -> bool:
    if source is None or generated is None or len(source) != 4 or len(generated) != 4:
        return False
    source_width, source_height = source[2] - source[0], source[3] - source[1]
    generated_width = generated[2] - generated[0]
    generated_height = generated[3] - generated[1]
    if source_width <= 0 or source_height <= 0:
        return False
    return generated_width > source_width * 1.8 or generated_height > source_height * 1.8


async def has_oversized_face(source: Path, generated: Path) -> bool:
    """Best-effort guard; unavailable local detector does not block generation."""
    if not all(path.is_file() for path in (_WORKER, _PYTHON, _MODEL)):
        return False
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            str(_PYTHON), str(_WORKER), str(source), str(generated), str(_MODEL),
            cwd=str(DEFAULT_INSTALL_DIR), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=45)
        if process.returncode != 0 or len(stdout) > 4096:
            return False
        result = json.loads(stdout)
        return _oversized_face(result.get("source"), result.get("generated"))
    except asyncio.CancelledError:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        raise
    except (OSError, TimeoutError, ValueError, AttributeError, TypeError):
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        return False
