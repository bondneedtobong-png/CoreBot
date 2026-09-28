"""A generated photo is previewed before entering the profile randomization pool."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from bot.handlers import comfy_photo
from services.comfyui import PreviewResult


class _State:
    def __init__(self):
        self.data = {}
        self.state = None

    async def clear(self):
        self.data = {}
        self.state = None

    async def set_state(self, state):
        self.state = state

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def get_data(self):
        return self.data.copy()


class _Status:
    def __init__(self):
        self.edits = []

    async def edit_text(self, text):
        self.edits.append(text)


class _Message:
    def __init__(self, text=""):
        self.from_user = SimpleNamespace(id=1)
        self.text = text
        self.status = _Status()
        self.photos = []
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))
        return self.status

    async def answer_photo(self, photo, **kwargs):
        self.photos.append((photo, kwargs))


class _Callback:
    def __init__(self, data):
        self.data = data
        self.from_user = SimpleNamespace(id=1)
        self.message = _Message()
        self.notifications = []

    async def answer(self, text="", **kwargs):
        self.notifications.append((text, kwargs))


def test_preview_requires_separate_save_action(tmp_path, monkeypatch):
    preview_dir = tmp_path / "previews"
    preview_dir.mkdir()
    image = preview_dir / ("a" * 12 + "b" * 20 + ".png")
    image.write_bytes(b"test-png")
    monkeypatch.setattr(comfy_photo, "_PREVIEW_DIR", preview_dir)
    monkeypatch.setattr(comfy_photo, "is_authorized_user", lambda uid: uid == 1)
    generation = []
    pool = []

    async def fake_generate(prompt, *, negative_prompt):
        generation.append((prompt, negative_prompt))
        return PreviewResult("prompt-id", image, 42, "sdxl", 1024, 1024)

    @asynccontextmanager
    async def fake_session():
        yield object()

    async def fake_add(_session, values):
        pool.append(values)

    monkeypatch.setattr(comfy_photo, "generate_profile_preview", fake_generate)
    monkeypatch.setattr(comfy_photo, "session_scope", fake_session)
    monkeypatch.setattr(comfy_photo, "add_pool_batch", fake_add)
    monkeypatch.setattr(comfy_photo, "_save_photo", lambda path: "approved.png" if path == image else "wrong.png")
    state = _State()
    message = _Message("Adult portrait in natural daylight")
    asyncio.run(comfy_photo.receive_comfy_prompt(message, state))
    assert len(message.photos) == 1
    assert generation[0][0] == message.text
    assert pool == []
    assert state.data["preview_path"] == str(image)

    bad = _Callback("ai_comfy_save_" + "f" * 12)
    asyncio.run(comfy_photo.save_comfy_photo(bad, state))
    assert pool == []

    good = _Callback("ai_comfy_save_" + "a" * 12)
    asyncio.run(comfy_photo.save_comfy_photo(good, state))
    assert pool == [{"photo": ["approved.png"]}]
    assert state.data == {}
    assert any("не изменены" in text for text, _ in good.message.answers)


def test_preview_path_rejects_file_outside_data_dir(tmp_path, monkeypatch):
    preview_dir = tmp_path / "previews"
    preview_dir.mkdir()
    outside = tmp_path / ("a" * 12 + ".png")
    outside.write_bytes(b"png")
    monkeypatch.setattr(comfy_photo, "_PREVIEW_DIR", preview_dir)
    assert comfy_photo._safe_preview_path(str(outside), "a" * 12) is None
