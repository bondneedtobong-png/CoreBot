"""Bounded SDXL photo previews through a loopback-only ComfyUI server.

Only the fixed, reviewed standard-node graph below can be submitted. The
caller cannot supply a workflow, node type, server-side path or output path.
Generated PNG files remain in ``data/comfy_previews`` for human review.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import os
import re
import struct
import uuid
import zlib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from bot.config import DATA_DIR
from services.comfyui import lifecycle


DEFAULT_BASE_URL = "http://127.0.0.1:8188"
ALLOWED_MODELS = frozenset({
    "sd_xl_base_1.0.safetensors",
    "Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors",
})
# The profile asset pool accepts at most 10,000,000 bytes.
MAX_PNG_BYTES = 10_000_000
MAX_JSON_BYTES = 512 * 1024
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_SAFE_FILENAME = re.compile(r"^[a-zA-Z0-9_.-]+\.png$")


class ComfyPreviewError(RuntimeError):
    """A validation, transport or generation failure without provider details."""


@dataclass(frozen=True)
class PreviewRequest:
    prompt: str
    negative_prompt: str = "blurry, low quality, deformed, watermark, text"
    seed: int | None = None
    steps: int = 25
    model: str = "sd_xl_base_1.0.safetensors"
    width: int = 1024
    height: int = 1024

    def validate(self) -> None:
        if not isinstance(self.prompt, str) or not 1 <= len(self.prompt.strip()) <= 1500:
            raise ValueError("prompt must contain 1–1500 characters")
        if not isinstance(self.negative_prompt, str) or len(self.negative_prompt) > 1000:
            raise ValueError("negative_prompt is too long")
        if self.seed is not None and (
            type(self.seed) is not int or not 0 <= self.seed < 2**63
        ):
            raise ValueError("seed is out of range")
        if type(self.steps) is not int or not 1 <= self.steps <= 40:
            raise ValueError("steps must be between 1 and 40")
        if not isinstance(self.model, str) or self.model not in ALLOWED_MODELS:
            raise ValueError("model is not allowed")
        if any(type(side) is not int or side < 512 or side > 1024 or side % 64 for side in (self.width, self.height)):
            raise ValueError("width and height must be multiples of 64 between 512 and 1024")
        if self.width * self.height > 1024 * 1024:
            raise ValueError("image area is too large")


@dataclass(frozen=True)
class PreviewResult:
    prompt_id: str
    path: Path
    seed: int
    model: str
    width: int
    height: int


def build_workflow(request: PreviewRequest, *, seed: int, filename_prefix: str) -> dict:
    """Construct the reviewed SDXL API graph from literals and fixed links."""
    request.validate()
    if type(seed) is not int or not 0 <= seed < 2**63:
        raise ValueError("seed is out of range")
    if not re.fullmatch(r"corebot_preview_[0-9a-f]{24}", filename_prefix):
        raise ValueError("invalid filename prefix")
    return {
        "3": {"class_type": "KSampler", "inputs": {
            "seed": seed, "steps": request.steps, "cfg": 7.5,
            "sampler_name": "dpmpp_2m", "scheduler": "karras", "denoise": 1.0,
            "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0],
            "latent_image": ["5", 0],
        }},
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": request.model}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {
            "width": request.width, "height": request.height, "batch_size": 1,
        }},
        "6": {"class_type": "CLIPTextEncode", "inputs": {
            "text": request.prompt.strip(), "clip": ["4", 1],
        }},
        "7": {"class_type": "CLIPTextEncode", "inputs": {
            "text": request.negative_prompt, "clip": ["4", 1],
        }},
        "8": {"class_type": "VAEDecode", "inputs": {
            "samples": ["3", 0], "vae": ["4", 2],
        }},
        "9": {"class_type": "SaveImage", "inputs": {
            "filename_prefix": filename_prefix, "images": ["8", 0],
        }},
    }


def _validate_base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid ComfyUI base URL") from exc
    if (
        parsed.scheme != "http" or host is None or port is None
        or parsed.username or parsed.password or parsed.path not in ("", "/")
        or parsed.query or parsed.fragment
    ):
        raise ValueError("ComfyUI base URL must be a loopback HTTP origin")
    try:
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError("ComfyUI base URL must be a loopback HTTP origin")
    except ValueError as exc:
        raise ValueError("ComfyUI base URL must use a numeric loopback address") from exc
    return value.rstrip("/")


def _validate_png(data: bytes, *, width: int, height: int) -> None:
    if not 45 <= len(data) <= MAX_PNG_BYTES or not data.startswith(_PNG_MAGIC):
        raise ComfyPreviewError("ComfyUI returned an invalid PNG")
    offset = len(_PNG_MAGIC)
    saw_header = False
    saw_pixels = False
    saw_end = False
    while offset + 12 <= len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        end = offset + 12 + length
        if end > len(data):
            raise ComfyPreviewError("ComfyUI returned an incomplete PNG")
        kind = data[offset + 4:offset + 8]
        payload = data[offset + 8:end - 4]
        checksum = struct.unpack(">I", data[end - 4:end])[0]
        if zlib.crc32(data[offset + 4:end - 4]) != checksum:
            raise ComfyPreviewError("ComfyUI returned a corrupt PNG")
        if not saw_header:
            if kind != b"IHDR" or length != 13:
                raise ComfyPreviewError("ComfyUI returned an invalid PNG")
            if struct.unpack(">II", payload[:8]) != (width, height):
                raise ComfyPreviewError("ComfyUI returned an unexpected image size")
            saw_header = True
        elif kind == b"IDAT":
            saw_pixels = True
        elif kind == b"IEND":
            if length != 0 or not saw_pixels or end != len(data):
                raise ComfyPreviewError("ComfyUI returned an invalid PNG")
            saw_end = True
            break
        offset = end
    if not saw_header or not saw_end:
        raise ComfyPreviewError("ComfyUI returned an incomplete PNG")


class ComfyPreviewClient:
    """Generate one PNG preview in CoreBot's data directory.

    The instance is safe for local-only use; create once and call ``generate``.
    ``base_url`` may change the port for a local ComfyUI instance but cannot
    point at a hostname, remote address, URL path or redirect destination.
    """

    def __init__(
        self, *, base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 300.0, poll_seconds: float = 1.0,
    ) -> None:
        self.base_url = _validate_base_url(base_url)
        if (
            type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
            or type(poll_seconds) not in (int, float) or not math.isfinite(poll_seconds)
            or not 5 <= timeout_seconds <= 900 or not 0.1 <= poll_seconds <= 10
        ):
            raise ValueError("invalid ComfyUI timeout or polling interval")
        self.timeout_seconds = float(timeout_seconds)
        self.poll_seconds = float(poll_seconds)
        self._semaphore = asyncio.Semaphore(1)

    async def generate(self, request: PreviewRequest) -> PreviewResult:
        request.validate()
        seed = request.seed if request.seed is not None else uuid.uuid4().int % (2**63)
        prefix = f"corebot_preview_{uuid.uuid4().hex[:24]}"
        workflow = build_workflow(request, seed=seed, filename_prefix=prefix)
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        try:
            async with self._semaphore, asyncio.timeout(self.timeout_seconds):
                async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
                    prompt_body = {"prompt": workflow, "client_id": str(uuid.uuid4())}
                    try:
                        accepted = await self._json(
                            session, "POST", "/prompt", json_body=prompt_body,
                        )
                    except aiohttp.ClientConnectorError:
                        # An absent local server is the only condition that should
                        # cause us to launch the explicitly configured ComfyUI install.
                        try:
                            await lifecycle.ensure_running(self.base_url)
                        except lifecycle.ComfyUILifecycleError as exc:
                            raise ComfyPreviewError(str(exc)) from exc
                        accepted = await self._json(
                            session, "POST", "/prompt", json_body=prompt_body,
                        )
                    prompt_id = accepted.get("prompt_id") if isinstance(accepted, dict) else None
                    try:
                        prompt_id = str(uuid.UUID(prompt_id))
                    except (TypeError, ValueError, AttributeError) as exc:
                        raise ComfyPreviewError("ComfyUI did not accept the workflow") from exc
                    if accepted.get("node_errors"):
                        raise ComfyPreviewError("ComfyUI rejected the workflow")
                    image = await self._wait_for_image(session, prompt_id, prefix)
                    params = {
                        "filename": image["filename"],
                        "subfolder": "", "type": "output",
                    }
                    data = await self._png(session, params=params)
                    _validate_png(data, width=request.width, height=request.height)
                    output_dir = DATA_DIR / "comfy_previews"
                    output_dir.mkdir(parents=True, exist_ok=True)
                    target = output_dir / f"{uuid.uuid4().hex}.png"
                    temporary = target.with_suffix(".tmp")
                    try:
                        with temporary.open("xb") as handle:
                            handle.write(data)
                        os.replace(temporary, target)
                    finally:
                        temporary.unlink(missing_ok=True)
                    return PreviewResult(prompt_id, target, seed, request.model, request.width, request.height)
        except TimeoutError as exc:
            raise ComfyPreviewError("ComfyUI preview timed out") from exc
        except aiohttp.ClientError as exc:
            raise ComfyPreviewError("ComfyUI is unavailable or returned invalid data") from exc
        except OSError as exc:
            raise ComfyPreviewError("Could not save the ComfyUI preview") from exc

    async def _wait_for_image(self, session: aiohttp.ClientSession, prompt_id: str, prefix: str) -> dict:
        while True:
            history = await self._json(session, "GET", f"/history/{prompt_id}")
            entry = history.get(prompt_id) if isinstance(history, dict) else None
            if isinstance(entry, dict):
                status = entry.get("status") or {}
                if not isinstance(status, dict):
                    raise ComfyPreviewError("ComfyUI returned invalid history status")
                if status.get("status_str") == "error":
                    raise ComfyPreviewError("ComfyUI failed to generate the preview")
                if status.get("completed"):
                    outputs = entry.get("outputs")
                    output = outputs.get("9") if isinstance(outputs, dict) else None
                    images = output.get("images") if isinstance(output, dict) else None
                    if not isinstance(images, list) or len(images) != 1 or not isinstance(images[0], dict):
                        raise ComfyPreviewError("ComfyUI returned no single PNG output")
                    image = images[0]
                    filename = image.get("filename")
                    if (
                        not isinstance(filename, str) or not _SAFE_FILENAME.fullmatch(filename)
                        or not filename.startswith(prefix + "_")
                        or image.get("type") != "output" or image.get("subfolder") not in ("", None)
                    ):
                        raise ComfyPreviewError("ComfyUI returned an unsafe output reference")
                    return image
            queue = await self._json(session, "GET", "/queue")
            if not isinstance(queue, dict) or not isinstance(queue.get("queue_running"), list) or not isinstance(queue.get("queue_pending"), list):
                raise ComfyPreviewError("ComfyUI returned invalid queue state")
            await asyncio.sleep(self.poll_seconds)

    async def _json(self, session: aiohttp.ClientSession, method: str, path: str, *, json_body: dict | None = None) -> dict:
        async with session.request(method, self.base_url + path, json=json_body, allow_redirects=False) as response:
            if response.status != 200 or response.content_length is not None and response.content_length > MAX_JSON_BYTES:
                raise ComfyPreviewError("ComfyUI request failed")
            payload = await self._read_bounded(response, MAX_JSON_BYTES)
            try:
                parsed = json.loads(payload)
            except (ValueError, UnicodeDecodeError) as exc:
                raise ComfyPreviewError("ComfyUI returned invalid JSON") from exc
            if not isinstance(parsed, dict):
                raise ComfyPreviewError("ComfyUI returned invalid JSON")
            return parsed

    async def _png(self, session: aiohttp.ClientSession, *, params: dict) -> bytes:
        async with session.request("GET", self.base_url + "/view", params=params, allow_redirects=False) as response:
            if response.status != 200 or response.content_length is not None and response.content_length > MAX_PNG_BYTES:
                raise ComfyPreviewError("ComfyUI image download failed")
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "image/png":
                raise ComfyPreviewError("ComfyUI returned a non-PNG image")
            return await self._read_bounded(response, MAX_PNG_BYTES)

    @staticmethod
    async def _read_bounded(response: aiohttp.ClientResponse, limit: int) -> bytes:
        chunks = bytearray()
        while True:
            part = await response.content.read(min(64 * 1024, limit - len(chunks) + 1))
            if not part:
                return bytes(chunks)
            chunks.extend(part)
            if len(chunks) > limit:
                raise ComfyPreviewError("ComfyUI response is too large")


async def generate_profile_preview(
    prompt: str, *, negative_prompt: str = "", seed: int | None = None,
) -> PreviewResult:
    """Generate one reviewable 1024×1024 SDXL preview in ``data/comfy_previews``."""
    return await _PROFILE_PREVIEW_CLIENT.generate(PreviewRequest(
        prompt=prompt, negative_prompt=negative_prompt, seed=seed,
    ))


_PROFILE_PREVIEW_CLIENT = ComfyPreviewClient()
