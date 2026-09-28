"""ComfyUI preview adapter contracts with a mocked HTTP transport."""

from __future__ import annotations

import asyncio
import json
import struct
import uuid
import zlib

import pytest

from services.comfyui import (
    ComfyPreviewClient, ComfyPreviewError, PreviewRequest, PreviewResult,
    build_workflow, generate_profile_preview,
)
from services.comfyui import preview


def _png(width: int = 512, height: int = 512) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload)) + kind + payload
            + struct.pack(">I", zlib.crc32(kind + payload))
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw_row = b"\x00" + b"\x00\x00\x00" * width
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw_row * height))
        + chunk(b"IEND", b"")
    )


class _Content:
    def __init__(self, body: bytes):
        self.body = body
        self.offset = 0

    async def read(self, size: int) -> bytes:
        part = self.body[self.offset:self.offset + size]
        self.offset += len(part)
        return part


class _Response:
    def __init__(self, body: dict | bytes, *, status: int = 200, content_type: str = "application/json"):
        self.body = json.dumps(body).encode() if isinstance(body, dict) else body
        self.content = _Content(self.body)
        self.content_length = len(self.body)
        self.status = status
        self.headers = {"Content-Type": content_type}

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


def test_fixed_workflow_and_restricted_inputs():
    prefix = "corebot_preview_" + "a" * 24
    graph = build_workflow(
        PreviewRequest("portrait", model="sd_xl_base_1.0.safetensors", width=512, height=768),
        seed=123, filename_prefix=prefix,
    )
    assert {node["class_type"] for node in graph.values()} == {
        "KSampler", "CheckpointLoaderSimple", "EmptyLatentImage", "CLIPTextEncode", "VAEDecode", "SaveImage",
    }
    assert graph["3"]["inputs"]["seed"] == 123
    assert graph["4"]["inputs"]["ckpt_name"] == "sd_xl_base_1.0.safetensors"
    assert graph["5"]["inputs"]["height"] == 768
    assert graph["9"]["inputs"]["filename_prefix"] == prefix
    with pytest.raises(ValueError, match="model"):
        PreviewRequest("portrait", model="../evil.safetensors").validate()
    with pytest.raises(ValueError, match="model"):
        PreviewRequest("portrait", model=["sd_xl_base_1.0.safetensors"]).validate()
    with pytest.raises(ValueError, match="width"):
        PreviewRequest("portrait", width=4096).validate()
    with pytest.raises(ValueError, match="steps"):
        PreviewRequest("portrait", steps=True).validate()
    with pytest.raises(ValueError, match="prompt"):
        PreviewRequest(" ").validate()
    with pytest.raises(ValueError, match="filename"):
        build_workflow(PreviewRequest("portrait"), seed=1, filename_prefix="../x")


@pytest.mark.parametrize("url", [
    "https://127.0.0.1:8188", "http://localhost:8188", "http://192.168.1.2:8188",
    "http://127.0.0.1:8188/path", "http://127.0.0.1:8188?x=1",
    "http://user:pass@127.0.0.1:8188", "http://127.0.0.1:8188@evil.test:8188",
])
def test_non_loopback_or_ambiguous_endpoint_rejected(url):
    with pytest.raises(ValueError, match="loopback"):
        ComfyPreviewClient(base_url=url)


def test_profile_preview_convenience_api(monkeypatch, tmp_path):
    captured = []

    async def fake_generate(self, request):
        captured.append(request)
        return PreviewResult(str(uuid.uuid4()), tmp_path / "example.png", 11, request.model, request.width, request.height)

    monkeypatch.setattr(ComfyPreviewClient, "generate", fake_generate)
    result = asyncio.run(generate_profile_preview("portrait", negative_prompt="watermark", seed=11))
    assert captured == [PreviewRequest("portrait", negative_prompt="watermark", seed=11)]
    assert result.path == tmp_path / "example.png"
    with pytest.raises(ValueError, match="timeout"):
        ComfyPreviewClient(timeout_seconds=float("nan"))


def test_generate_saves_bounded_png_to_corebot_data(monkeypatch, tmp_path):
    prompt_id = str(uuid.uuid4())
    prefix = None

    def accept(calls):
        nonlocal prefix
        graph = calls[-1][2]["json"]["prompt"]
        prefix = graph["9"]["inputs"]["filename_prefix"]
        assert graph["5"]["inputs"]["batch_size"] == 1
        return _Response({"prompt_id": prompt_id, "node_errors": {}})

    def history(calls):
        assert calls[-1][1].endswith("/history/" + prompt_id)
        return _Response({prompt_id: {
            "status": {"completed": True, "status_str": "success"},
            "outputs": {"9": {"images": [{
                "filename": prefix + "_00001_.png", "subfolder": "", "type": "output",
            }]}}},
        })

    fake = _Session([accept, history, _Response(_png(), content_type="image/png")])
    monkeypatch.setattr(preview.aiohttp, "ClientSession", lambda **_kwargs: fake)
    monkeypatch.setattr(preview, "DATA_DIR", tmp_path / "data")
    result = asyncio.run(ComfyPreviewClient().generate(PreviewRequest("a person", seed=7, width=512, height=512)))
    assert result.prompt_id == prompt_id
    assert result.seed == 7
    assert result.path.parent == tmp_path / "data" / "comfy_previews"
    assert result.path.read_bytes() == _png()
    assert fake.calls[0][1] == "http://127.0.0.1:8188/prompt"
    assert fake.calls[2][1] == "http://127.0.0.1:8188/view"
    assert fake.calls[2][2]["params"] == {
        "filename": prefix + "_00001_.png", "subfolder": "", "type": "output",
    }


def test_queue_polling_then_history_success(monkeypatch, tmp_path):
    prompt_id = str(uuid.uuid4())

    def history(calls):
        prefix = calls[0][2]["json"]["prompt"]["9"]["inputs"]["filename_prefix"]
        return _Response({prompt_id: {
            "status": {"completed": True, "status_str": "success"},
            "outputs": {"9": {"images": [{"filename": prefix + "_00001_.png", "type": "output"}]}}},
        })

    fake = _Session([
        _Response({"prompt_id": prompt_id, "node_errors": {}}),
        _Response({}),
        _Response({"queue_running": [], "queue_pending": []}),
        history,
        _Response(_png(), content_type="image/png"),
    ])
    monkeypatch.setattr(preview.aiohttp, "ClientSession", lambda **_kwargs: fake)
    monkeypatch.setattr(preview, "DATA_DIR", tmp_path / "data")
    result = asyncio.run(ComfyPreviewClient(poll_seconds=0.1).generate(PreviewRequest("portrait", width=512, height=512)))
    assert result.path.exists()
    assert [call[1].split("/")[-1] for call in fake.calls] == ["prompt", prompt_id, "queue", prompt_id, "view"]


def test_error_status_precedes_completed_and_unsafe_output_rejected(monkeypatch, tmp_path):
    prompt_id = str(uuid.uuid4())
    monkeypatch.setattr(preview, "DATA_DIR", tmp_path / "data")
    fake = _Session([
        _Response({"prompt_id": prompt_id, "node_errors": {}}),
        _Response({prompt_id: {"status": {"completed": True, "status_str": "error"}}}),
    ])
    monkeypatch.setattr(preview.aiohttp, "ClientSession", lambda **_kwargs: fake)
    with pytest.raises(ComfyPreviewError, match="failed"):
        asyncio.run(ComfyPreviewClient().generate(PreviewRequest("portrait")))
    assert not (tmp_path / "data").exists()

    fake = _Session([
        _Response({"prompt_id": prompt_id, "node_errors": {}}),
        _Response({prompt_id: {
            "status": {"completed": True},
            "outputs": {"9": {"images": [{
                "filename": "../../private.png", "subfolder": "", "type": "output",
            }]}}},
        }),
    ])
    with pytest.raises(ComfyPreviewError, match="unsafe"):
        asyncio.run(ComfyPreviewClient().generate(PreviewRequest("portrait")))
    assert len(fake.calls) == 2


def test_redirect_and_non_png_rejected_without_saving(monkeypatch, tmp_path):
    prompt_id = str(uuid.uuid4())

    def history(calls):
        prefix = calls[0][2]["json"]["prompt"]["9"]["inputs"]["filename_prefix"]
        return _Response({prompt_id: {
            "status": {"completed": True},
            "outputs": {"9": {"images": [{
                "filename": prefix + "_00001_.png", "type": "output",
            }]}}},
        })

    monkeypatch.setattr(preview, "DATA_DIR", tmp_path / "data")
    for image_response in [
        _Response(b"", status=302),
        _Response(b"not a png", content_type="text/html"),
        _Response(_png(768, 512), content_type="image/png"),
    ]:
        fake = _Session([_Response({"prompt_id": prompt_id}), history, image_response])
        monkeypatch.setattr(preview.aiohttp, "ClientSession", lambda **_kwargs: fake)
        with pytest.raises(ComfyPreviewError):
            asyncio.run(ComfyPreviewClient().generate(PreviewRequest("portrait", width=512, height=512)))
        assert not (tmp_path / "data" / "comfy_previews").exists()
