"""Bot creates synthetic characters and reviews each derived account photo."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageDraw

from bot.handlers import comfy_identity as menu
from services.comfyui.identity import IdentityPhotoResult, IdentityRecord


class State:
    def __init__(self):
        self.data = {}
        self.current = None

    async def clear(self):
        self.data = {}
        self.current = None

    async def update_data(self, **values):
        self.data.update(values)

    async def get_data(self):
        return dict(self.data)

    async def set_state(self, value):
        self.current = value


class Status:
    def __init__(self):
        self.edits = []

    async def edit_text(self, text):
        self.edits.append(text)


class Message:
    def __init__(self, text=""):
        self.from_user = SimpleNamespace(id=1)
        self.text = text
        self.status = Status()
        self.answers = []
        self.photos = []
        self.photo = None
        self.document = None
        self.bot = None

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))
        return self.status

    async def answer_photo(self, photo, **kwargs):
        self.photos.append((photo, kwargs))


class Callback:
    def __init__(self, data):
        self.from_user = SimpleNamespace(id=1)
        self.data = data
        self.message = Message()
        self.acks = []

    async def answer(self, text="", **kwargs):
        self.acks.append((text, kwargs))


def test_create_identity_shows_saved_reference(tmp_path, monkeypatch):
    identity_id = "a" * 32
    reference = tmp_path / "reference.png"
    reference.write_bytes(b"portrait")
    record = IdentityRecord(identity_id, reference, 42, "fictional adult", "sdxl")
    prompts = []

    async def create(prompt):
        prompts.append(prompt)
        return record

    monkeypatch.setattr(menu, "is_authorized_user", lambda uid: uid == 1)
    monkeypatch.setattr(menu, "create_identity", create)
    state = State()
    message = Message("Adult person with curly hair")
    asyncio.run(menu.receive_appearance(message, state))
    assert prompts == [message.text]
    assert len(message.photos) == 1
    assert identity_id in message.photos[0][1]["reply_markup"].inline_keyboard[0][0].callback_data
    assert state.data == {}


def test_variant_needs_approval_before_photo_pool(tmp_path, monkeypatch):
    identity_id = "b" * 32
    identity_dir = tmp_path / "comfy_identities" / identity_id
    identity_dir.mkdir(parents=True)
    reference = identity_dir / "reference.png"
    reference.write_bytes(b"portrait")
    variant = identity_dir / ("c" * 12 + "d" * 20 + ".png")
    variant.write_bytes(b"photo")
    record = IdentityRecord(identity_id, reference, 42, "fictional adult", "sdxl")
    pool = []

    async def generate(*args, **kwargs):
        assert args == (identity_id, "garden scene")
        assert kwargs == {"scene_template_path": None, "person_mask_path": None}
        return IdentityPhotoResult(identity_id, variant, "prompt-id", 11)

    @asynccontextmanager
    async def session():
        yield object()

    async def add(_session, values, *, category):
        pool.append((values, category))

    monkeypatch.setattr(menu, "_IDENTITY_DIR", tmp_path / "comfy_identities")
    monkeypatch.setattr(menu, "get_identity", lambda _id: record)
    monkeypatch.setattr(menu, "generate_identity_photo", generate)
    monkeypatch.setattr(menu, "is_authorized_user", lambda uid: uid == 1)
    monkeypatch.setattr(menu, "_save_photo", lambda _path: "approved.png")
    monkeypatch.setattr(menu, "session_scope", session)
    monkeypatch.setattr(menu, "add_pool_batch", add)
    state = State()
    state.data = {"identity_id": identity_id, "scene_prompt": "garden scene"}
    message = Message()
    asyncio.run(menu._generate(message, state))
    assert pool == []
    assert len(message.photos) == 1

    invalid = Callback("ai_person_save_f_" + "f" * 12)
    asyncio.run(menu.save_person_photo(invalid, state))
    assert pool == []

    valid = Callback("ai_person_save_f_" + "c" * 12)
    asyncio.run(menu.save_person_photo(valid, state))
    assert pool == [({"photo": ["approved.png"]}, "f")]
    assert state.data == {}


def test_template_and_mask_receive_identical_crop(tmp_path, monkeypatch):
    monkeypatch.setattr(menu, "_TEMP_DIR", tmp_path)
    source = tmp_path / "source.jpg"
    mask = tmp_path / "mask.png"
    Image.new("RGB", (1200, 800), "blue").save(source)
    marking = Image.new("L", (1200, 800), 0)
    ImageDraw.Draw(marking).rectangle((450, 100, 750, 700), fill=255)
    marking.save(mask)
    prepared_image, prepared_mask = menu._prepare_template(source, mask)
    try:
        with Image.open(prepared_image) as photo, Image.open(prepared_mask) as person:
            assert photo.size == person.size == (1024, 1024)
            assert person.getpixel((512, 512)) == 255
            assert person.getpixel((0, 0)) == 0
    finally:
        prepared_image.unlink()
        prepared_mask.unlink()


def test_empty_person_mask_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(menu, "_TEMP_DIR", tmp_path)
    source = tmp_path / "source.png"
    mask = tmp_path / "mask.png"
    Image.new("RGB", (1024, 1024), "blue").save(source)
    Image.new("L", (1024, 1024), 0).save(mask)
    try:
        menu._prepare_template(source, mask)
    except ValueError as exc:
        assert "person mask" in str(exc)
    else:
        raise AssertionError("empty mask must not reach ComfyUI")


def test_template_auto_mask_is_reviewed_before_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(menu, "_TEMP_DIR", tmp_path)
    monkeypatch.setattr(menu, "is_authorized_user", lambda uid: uid == 1)
    source = tmp_path / "fixture.jpg"
    Image.new("RGB", (1024, 1024), "blue").save(source)

    async def download(_source, destination):
        destination.write_bytes(source.read_bytes())

    async def detect(_source, output):
        marking = Image.new("L", (1024, 1024), 0)
        ImageDraw.Draw(marking).rectangle((300, 100, 700, 900), fill=255)
        marking.save(output)

    generated = []

    async def generate(_message, _state, *, template, mask):
        generated.append((template, mask))
        assert template.is_file() and mask.is_file()

    monkeypatch.setattr(menu, "generate_person_mask", detect)
    monkeypatch.setattr(menu, "_generate", generate)
    state = State()
    state.data = {"identity_id": "a" * 32, "scene_prompt": "cafe scene"}
    message = Message()
    message.photo = [SimpleNamespace(file_size=source.stat().st_size)]
    message.bot = SimpleNamespace(download=download)

    asyncio.run(menu.receive_template(message, state))
    assert state.current == menu.PersonFlow.waiting_mask
    assert len(message.photos) == 1
    assert "ai_person_mask_accept" in str(message.photos[0][1]["reply_markup"])
    assert generated == []
    assert state.data["auto_mask_path"]
    assert state.data["mask_preview_path"]

    callback = Callback("ai_person_mask_accept")
    asyncio.run(menu.accept_auto_mask(callback, state))
    assert len(generated) == 1
    assert not (tmp_path / Path(state.data["template_path"]).name).exists()
    assert not Path(state.data["auto_mask_path"]).exists()


def test_auto_detection_failure_offers_manual_mask(tmp_path, monkeypatch):
    monkeypatch.setattr(menu, "_TEMP_DIR", tmp_path)
    monkeypatch.setattr(menu, "is_authorized_user", lambda uid: uid == 1)
    source = tmp_path / "fixture.jpg"
    Image.new("RGB", (1024, 1024), "blue").save(source)

    async def download(_source, destination):
        destination.write_bytes(source.read_bytes())

    async def detect(_source, _output):
        raise menu.PersonMaskError("no person")

    monkeypatch.setattr(menu, "generate_person_mask", detect)
    state = State()
    state.data = {"identity_id": "a" * 32, "scene_prompt": "cafe scene"}
    message = Message()
    message.photo = [SimpleNamespace(file_size=source.stat().st_size)]
    message.bot = SimpleNamespace(download=download)

    asyncio.run(menu.receive_template(message, state))
    assert state.current == menu.PersonFlow.waiting_mask
    assert not message.photos
    assert "Пришлите маску PNG" in message.status.edits[-1]
    assert Path(state.data["template_path"]).exists()
