"""Synthetic identity and fixed PhotoMaker workflow contracts."""

from __future__ import annotations

import asyncio
from io import BytesIO
import json
import struct
import uuid
import zlib

import pytest
from PIL import Image, ImageDraw

from services.comfyui import identity
from services.comfyui.preview import PreviewResult


def _png() -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))

    width = height = 1024
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress((b"\x00" + b"\x00\x00\x00" * width) * height))
            + chunk(b"IEND", b""))


class _Body:
    def __init__(self, body):
        self.body = body
        self.offset = 0

    async def read(self, size):
        result = self.body[self.offset:self.offset + size]
        self.offset += len(result)
        return result


class _Response:
    def __init__(self, body, *, content_type="application/json", status=200):
        body = json.dumps(body).encode() if isinstance(body, dict) else body
        self.content = _Body(body)
        self.content_length = len(body)
        self.headers = {"Content-Type": content_type}
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Session:
    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        assert kwargs["allow_redirects"] is False
        reply = self.replies.pop(0)
        return reply(self.calls) if callable(reply) else reply


def _create_record(monkeypatch, tmp_path):
    monkeypatch.setattr(identity, "DATA_DIR", tmp_path)
    source = tmp_path / "source.png"
    source.write_bytes(_png())

    async def fake_generate(_self, request):
        assert "adult person" in request.prompt
        assert request.model.startswith("Juggernaut")
        return PreviewResult(str(uuid.uuid4()), source, 42, request.model, 1024, 1024)

    monkeypatch.setattr(identity.preview.ComfyPreviewClient, "generate", fake_generate)
    return asyncio.run(identity.create_identity("freckles, dark hair"))


def test_create_list_and_get_identity(monkeypatch, tmp_path):
    record = _create_record(monkeypatch, tmp_path)
    assert record.reference_path.read_bytes() == _png()
    assert identity.get_identity(record.identity_id) == record
    assert identity.list_identities() == [record]
    assert identity.get_identity(uuid.uuid4().hex) is None
    with pytest.raises(ValueError, match="identity ID"):
        identity.get_identity("../escape")
    with pytest.raises(ValueError, match="prompt"):
        asyncio.run(identity.create_identity(""))


def test_create_identity_translates_russian_appearance(monkeypatch, tmp_path):
    monkeypatch.setattr(identity, "DATA_DIR", tmp_path)
    source = tmp_path / "source.png"
    source.write_bytes(_png())

    async def translate(prompt):
        assert prompt == "тёмные волосы"
        return "dark hair"

    async def generate(_self, request):
        assert "dark hair" in request.prompt
        assert "тёмные" not in request.prompt
        return PreviewResult(str(uuid.uuid4()), source, 42, request.model, 1024, 1024)

    monkeypatch.setattr(identity.prompt_translation, "translate_if_needed", translate)
    monkeypatch.setattr(identity.preview.ComfyPreviewClient, "generate", generate)
    record = asyncio.run(identity.create_identity("тёмные волосы"))
    assert record.prompt.endswith("dark hair")


def test_graph_is_fixed_and_template_requires_mask():
    args = dict(scene_prompt="standing in a garden", model="sd_xl_base_1.0.safetensors",
                seed=1, upload_name="corebot_identity_" + "a" * 24 + ".png",
                filename_prefix="corebot_identity_" + "b" * 24)
    graph = identity.build_identity_workflow(**args)
    assert graph["6"]["class_type"] == "PhotoMakerEncode"
    assert graph["3"]["inputs"]["denoise"] == 1.0
    assert graph["3"]["inputs"]["latent_image"] == ["5", 0]
    assert graph["5"]["class_type"] == "EmptyLatentImage"
    assert graph["5"]["inputs"] == {"width": 1024, "height": 1024, "batch_size": 1}
    assert graph["10"]["class_type"] == "PhotoMakerLoader"
    with pytest.raises(ValueError, match="mask"):
        identity.build_identity_workflow(**args, template_name=args["upload_name"])
    masked = identity.build_identity_workflow(
        **args, template_name=args["upload_name"], mask_name=args["upload_name"],
    )
    assert masked["3"]["inputs"]["positive"] == ["14", 0]
    assert masked["3"]["inputs"]["negative"] == ["14", 1]
    assert masked["3"]["inputs"]["latent_image"] == ["14", 2]
    assert masked["3"]["inputs"]["denoise"] == 0.55
    assert masked["13"]["inputs"]["channel"] == "red"
    assert masked["14"]["class_type"] == "InpaintModelConditioning"
    assert masked["14"]["inputs"]["pixels"] == ["12", 0]
    assert masked["14"]["inputs"]["mask"] == ["13", 0]
    assert "5" not in masked


def test_template_background_is_preserved_pixel_for_pixel():
    source = Image.new("RGB", (1024, 1024), "red")
    generated = Image.new("RGB", (1024, 1024), "blue")
    mask = Image.new("L", (1024, 1024), 0)
    ImageDraw.Draw(mask).rectangle((0, 0, 511, 1023), fill=255)

    def png(image):
        output = BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()

    combined = identity._composite_scene(png(source), png(mask), png(generated))
    with Image.open(BytesIO(combined)) as result:
        assert result.getpixel((100, 500)) == (0, 0, 255)
        assert result.getpixel((900, 500)) == (255, 0, 0)


def test_model_absence_and_template_pair_rejected_before_network(monkeypatch, tmp_path):
    record = _create_record(monkeypatch, tmp_path)
    monkeypatch.setattr(identity, "PHOTOMAKER_MODEL_DIR", tmp_path / "missing")
    with pytest.raises(ValueError, match="mask"):
        asyncio.run(identity.generate_identity_photo(
            record.identity_id, "garden", scene_template_path=record.reference_path,
        ))
    with pytest.raises(identity.ComfyIdentityModelMissingError, match="PhotoMaker model"):
        asyncio.run(identity.generate_identity_photo(record.identity_id, "garden"))


@pytest.mark.parametrize("oversized", [False, True])
@pytest.mark.parametrize("scene, expected", [
    ("standing in a garden", "standing in a garden"),
    ("в кафе", "in a cafe"),
])
def test_generate_uploads_identity_and_masked_scene(monkeypatch, tmp_path, oversized, scene, expected):
    record = _create_record(monkeypatch, tmp_path)
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / identity.PHOTOMAKER_MODEL_NAME).write_bytes(b"mock")
    monkeypatch.setattr(identity, "PHOTOMAKER_MODEL_DIR", model_dir)
    starts = []
    checked = []

    async def translate(prompt):
        return "in a cafe" if prompt == "в кафе" else prompt

    monkeypatch.setattr(identity.prompt_translation, "translate_if_needed", translate)

    async def no_oversized_face(source, generated):
        checked.append((source, generated))
        return oversized

    monkeypatch.setattr(identity.scene_quality, "has_oversized_face", no_oversized_face)

    async def ready(url):
        starts.append(url)

    monkeypatch.setattr(identity.lifecycle, "ensure_running", ready)
    template = tmp_path / "template.png"
    mask = tmp_path / "mask.png"
    template.write_bytes(_png())
    mask.write_bytes(_png())
    prompt_id = str(uuid.uuid4())

    def upload(calls):
        assert calls[-1][1].endswith("/upload/image")
        name = calls[-1][2]["data"]._fields[0][0]["filename"]
        return _Response({"name": name, "subfolder": "", "type": "input"})

    def accept(calls):
        graph = calls[-1][2]["json"]["prompt"]
        assert graph["14"]["class_type"] == "InpaintModelConditioning"
        assert graph["6"]["inputs"]["text"].startswith("a photograph of an adult person photomaker")
        assert "freckles, dark hair" in graph["6"]["inputs"]["text"]
        assert expected in graph["6"]["inputs"]["text"]
        return _Response({"prompt_id": prompt_id, "node_errors": {}})

    def history(calls):
        prefix = calls[3][2]["json"]["prompt"]["9"]["inputs"]["filename_prefix"]
        return _Response({prompt_id: {"status": {"completed": True}, "outputs": {"9": {"images": [
            {"filename": prefix + "_00001_.png", "type": "output"}
        ]}}}})

    fake = _Session([upload, upload, upload, accept, history,
                     _Response(_png(), content_type="image/png")])
    monkeypatch.setattr(identity.aiohttp, "ClientSession", lambda **_kwargs: fake)
    request = identity.generate_identity_photo(
        record.identity_id, scene, seed=9,
        scene_template_path=template, person_mask_path=mask,
    )
    if oversized:
        with pytest.raises(identity.ComfyIdentityQualityError, match="larger"):
            asyncio.run(request)
        assert [path.name for path in record.reference_path.parent.glob("*.png")] == ["reference.png"]
        return
    result = asyncio.run(request)
    assert result.seed == 9
    assert starts == ["http://127.0.0.1:8188"]
    with Image.open(result.path) as generated:
        assert generated.size == (1024, 1024)
        assert generated.getpixel((900, 500)) == (0, 0, 0)
    assert result.path.parent == record.reference_path.parent
    assert checked == [(template, result.path)]
    assert [call[1].rsplit("/", 1)[-1] for call in fake.calls[:4]] == [
        "image", "image", "image", "prompt",
    ]
