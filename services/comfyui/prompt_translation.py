"""Local translation of Cyrillic prompts before they reach SDXL's text encoder."""

from __future__ import annotations

import asyncio
from pathlib import Path
import re

from services.comfyui.lifecycle import DEFAULT_INSTALL_DIR


_WORKER = Path(__file__).with_name("prompt_translation_worker.py")
_PYTHON = DEFAULT_INSTALL_DIR / ".venv" / "Scripts" / "python.exe"
_CYRILLIC = re.compile(r"[\u0400-\u04ff]")


class PromptTranslationError(RuntimeError):
    """The local translator cannot turn a Cyrillic prompt into English."""


async def translate_if_needed(prompt: str) -> str:
    if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 1300:
        raise ValueError("prompt must contain 1–1300 characters")
    prompt = prompt.strip()
    if not _CYRILLIC.search(prompt):
        return prompt
    if not _PYTHON.is_file() or not _WORKER.is_file():
        raise PromptTranslationError("Local translation environment is unavailable")
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            str(_PYTHON), str(_WORKER), cwd=str(DEFAULT_INSTALL_DIR),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        output, _ = await asyncio.wait_for(
            process.communicate(prompt.encode("utf-8")), timeout=60,
        )
        if process.returncode != 0 or not 1 <= len(output) <= 4096:
            raise PromptTranslationError("Local RU→EN translation failed")
        translated = output.decode("utf-8").strip()
        if not translated or _CYRILLIC.search(translated):
            raise PromptTranslationError("Local RU→EN translation was incomplete")
        return translated
    except asyncio.CancelledError:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        raise
    except (OSError, TimeoutError, UnicodeDecodeError) as exc:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        raise PromptTranslationError("Local RU→EN translation failed") from exc
