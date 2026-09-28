"""Russian image prompts are translated before SDXL conditioning."""

from __future__ import annotations

import asyncio

import pytest

from services.comfyui import prompt_translation


def test_english_prompt_needs_no_worker(monkeypatch):
    async def should_not_run(*_args, **_kwargs):
        raise AssertionError("translation process was started")

    monkeypatch.setattr(prompt_translation.asyncio, "create_subprocess_exec", should_not_run)
    assert asyncio.run(prompt_translation.translate_if_needed("  sitting in a cafe  ")) == "sitting in a cafe"


def test_russian_prompt_is_sent_as_utf8_and_translated(monkeypatch, tmp_path):
    executable = tmp_path / "python.exe"
    worker = tmp_path / "translate.py"
    executable.touch()
    worker.touch()
    monkeypatch.setattr(prompt_translation, "_PYTHON", executable)
    monkeypatch.setattr(prompt_translation, "_WORKER", worker)
    received = []

    class Process:
        returncode = 0

        async def communicate(self, data):
            received.append(data)
            return b"in a cafe", b""

    async def spawn(*_args, **_kwargs):
        return Process()

    monkeypatch.setattr(prompt_translation.asyncio, "create_subprocess_exec", spawn)
    assert asyncio.run(prompt_translation.translate_if_needed("в кафе")) == "in a cafe"
    assert received == ["в кафе".encode("utf-8")]


def test_untranslated_cyrillic_is_rejected(monkeypatch, tmp_path):
    executable = tmp_path / "python.exe"
    worker = tmp_path / "translate.py"
    executable.touch()
    worker.touch()
    monkeypatch.setattr(prompt_translation, "_PYTHON", executable)
    monkeypatch.setattr(prompt_translation, "_WORKER", worker)

    class Process:
        returncode = 0

        async def communicate(self, _data):
            return "в кафе".encode("utf-8"), b""

    async def spawn(*_args, **_kwargs):
        return Process()

    monkeypatch.setattr(prompt_translation.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(prompt_translation.PromptTranslationError, match="incomplete"):
        asyncio.run(prompt_translation.translate_if_needed("в кафе"))
