"""Generate a reviewable person mask with the local ComfyUI Python environment."""

from __future__ import annotations

import asyncio
from pathlib import Path

from bot.config import DATA_DIR
from services.comfyui.lifecycle import DEFAULT_INSTALL_DIR


_WORKER = Path(__file__).with_name("person_mask_worker.py")
_MODEL = DEFAULT_INSTALL_DIR / "models" / "ultralytics" / "segm" / "yolo11n-seg.pt"
_PYTHON = DEFAULT_INSTALL_DIR / ".venv" / "Scripts" / "python.exe"
_TIMEOUT_SECONDS = 150
_lock = asyncio.Lock()


class PersonMaskError(RuntimeError):
    """A person could not be safely isolated from the scene template."""


def _in_data(path: Path) -> bool:
    try:
        return path.resolve().is_relative_to(DATA_DIR.resolve())
    except (OSError, ValueError):
        return False


async def generate_person_mask(template: Path, output: Path) -> None:
    """Write a black-background, white-person PNG for a single detected person."""
    if (
        not isinstance(template, Path) or not isinstance(output, Path)
        or not _in_data(template) or not _in_data(output)
        or not template.is_file() or output.suffix.lower() != ".png"
        or output.exists()
    ):
        raise ValueError("invalid local mask paths")
    if not _PYTHON.is_file() or not _WORKER.is_file():
        raise PersonMaskError("ComfyUI Python environment is unavailable")

    async with _lock:
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                str(_PYTHON), str(_WORKER), str(template), str(output), str(_MODEL),
                cwd=str(DEFAULT_INSTALL_DIR),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(process.wait(), timeout=_TIMEOUT_SECONDS)
            except TimeoutError as exc:
                process.kill()
                await process.wait()
                raise PersonMaskError("Automatic person detection timed out") from exc
            if process.returncode != 0 or not output.is_file() or output.stat().st_size > 10_000_000:
                raise PersonMaskError("Could not isolate exactly one person")
        except asyncio.CancelledError:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            output.unlink(missing_ok=True)
            raise
        except (OSError, PersonMaskError) as exc:
            output.unlink(missing_ok=True)
            if isinstance(exc, PersonMaskError):
                raise
            raise PersonMaskError("Automatic person detection failed") from exc
