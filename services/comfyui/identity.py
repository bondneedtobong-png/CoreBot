"""Local synthetic identity references and PhotoMaker scene variants.

The first portrait is an ordinary SDXL preview. Later images condition on that
portrait through ComfyUI's built-in PhotoMaker nodes. A single generated face
is a weak reference; visual identity consistency is best-effort, not guaranteed.
"""

from __future__ import annotations

import asyncio
from io import BytesIO
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

import aiohttp
from PIL import Image

from bot.config import DATA_DIR
from services.comfyui import preview
from services.comfyui import lifecycle
from services.comfyui import prompt_translation
from services.comfyui import scene_quality
from services.comfyui.lifecycle import DEFAULT_INSTALL_DIR


PHOTOMAKER_MODEL_NAME = "photomaker-v1.bin"
PHOTOMAKER_MODEL_DIR = DEFAULT_INSTALL_DIR / "models" / "photomaker"
MAX_IDENTITY_IMAGE_BYTES = preview.MAX_PNG_BYTES
_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class ComfyIdentityError(RuntimeError):
    """Identity creation or PhotoMaker generation failed."""


class ComfyIdentityModelMissingError(ComfyIdentityError):
    """The local PhotoMaker model must be installed before scene generation."""


class ComfyIdentityQualityError(ComfyIdentityError):
    """The scene was generated but failed a conservative composition check."""


class ComfyIdentityTranslationError(ComfyIdentityError):
    """A non-English scene prompt could not be translated locally."""


@dataclass(frozen=True)
class IdentityRecord:
    identity_id: str
    reference_path: Path
    seed: int
    prompt: str
    model: str


@dataclass(frozen=True)
class IdentityPhotoResult:
    identity_id: str
    path: Path
    prompt_id: str
    seed: int


def _identity_dir(identity_id: str) -> Path:
    if not isinstance(identity_id, str) or not _ID_RE.fullmatch(identity_id):
        raise ValueError("invalid identity ID")
    root = DATA_DIR / "comfy_identities"
    directory = root / identity_id
    if root.exists() and not directory.resolve().is_relative_to(root.resolve()):
        raise ValueError("identity directory escapes CoreBot data")
    return directory


def _write_identity(identity: IdentityRecord, png: bytes) -> None:
    directory = _identity_dir(identity.identity_id)
    directory.mkdir(parents=True, exist_ok=False)
    reference = directory / "reference.png"
    metadata = directory / "metadata.json"
    try:
        reference.write_bytes(png)
        metadata.write_text(json.dumps({
            "identity_id": identity.identity_id,
            "seed": identity.seed,
            "prompt": identity.prompt,
            "model": identity.model,
        }, ensure_ascii=False), encoding="utf-8")
    except OSError:
        reference.unlink(missing_ok=True)
        metadata.unlink(missing_ok=True)
        directory.rmdir()
        raise


def _read_identity(identity_id: str) -> tuple[IdentityRecord, bytes]:
    directory = _identity_dir(identity_id)
    try:
        raw = (directory / "metadata.json").read_bytes()
        if len(raw) > 4096:
            raise ValueError("identity metadata is too large")
        metadata = json.loads(raw)
        if not isinstance(metadata, dict) or metadata.get("identity_id") != identity_id:
            raise ValueError("identity metadata is invalid")
        prompt = metadata["prompt"]
        model = metadata["model"]
        seed = metadata["seed"]
        preview.PreviewRequest(prompt=prompt, model=model, seed=seed).validate()
        reference = directory / "reference.png"
        with reference.open("rb") as handle:
            png = handle.read(MAX_IDENTITY_IMAGE_BYTES + 1)
        preview._validate_png(png, width=1024, height=1024)
    except (OSError, KeyError, TypeError, ValueError, preview.ComfyPreviewError) as exc:
        raise ComfyIdentityError("Identity reference is missing or invalid") from exc
    return IdentityRecord(identity_id, reference, seed, prompt, model), png


def get_identity(identity_id: str) -> IdentityRecord | None:
    """Return a validated saved identity, or None if it is unavailable."""
    _identity_dir(identity_id)
    try:
        return _read_identity(identity_id)[0]
    except ComfyIdentityError:
        return None


def list_identities(limit: int = 20) -> list[IdentityRecord]:
    """List recent valid references from CoreBot's identity directory."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    root = DATA_DIR / "comfy_identities"
    if not root.exists():
        return []
    candidates = sorted(
        (item for item in root.iterdir() if item.is_dir() and not item.is_symlink()
         and _ID_RE.fullmatch(item.name)),
        key=lambda item: item.stat().st_mtime, reverse=True,
    )
    records = []
    for item in candidates:
        record = get_identity(item.name)
        if record is not None:
            records.append(record)
            if len(records) == limit:
                break
    return records


def _read_scene_png(path: Path) -> bytes:
    if not isinstance(path, Path):
        raise ValueError("scene image must be a local Path")
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(DATA_DIR.resolve()) or not resolved.is_file():
        raise ValueError("scene image must be within CoreBot data")
    with resolved.open("rb") as handle:
        image = handle.read(MAX_IDENTITY_IMAGE_BYTES + 1)
    preview._validate_png(image, width=1024, height=1024)
    return image


def _composite_scene(template_png: bytes, mask_png: bytes, generated_png: bytes) -> bytes:
    """Keep unmasked scene pixels exactly as uploaded by the user."""
    for data in (template_png, mask_png, generated_png):
        preview._validate_png(data, width=1024, height=1024)
    with (Image.open(BytesIO(template_png)) as template_file,
          Image.open(BytesIO(mask_png)) as mask_file,
          Image.open(BytesIO(generated_png)) as generated_file):
        combined = Image.composite(
            generated_file.convert("RGB"), template_file.convert("RGB"),
            mask_file.convert("L"),
        )
        output = BytesIO()
        combined.save(output, format="PNG")
        data = output.getvalue()
    preview._validate_png(data, width=1024, height=1024)
    return data


def build_identity_workflow(
    *, scene_prompt: str, model: str, seed: int, upload_name: str,
    filename_prefix: str, width: int = 1024, height: int = 1024,
    template_name: str | None = None, mask_name: str | None = None,
    identity_description: str = "",
) -> dict:
    """A fixed SDXL graph using one uploaded identity image and PhotoMaker."""
    preview.PreviewRequest(scene_prompt, seed=seed, model=model, width=width, height=height).validate()
    if not re.fullmatch(r"corebot_identity_[0-9a-f]{24}\.png", upload_name):
        raise ValueError("invalid uploaded image name")
    if not re.fullmatch(r"corebot_identity_[0-9a-f]{24}", filename_prefix):
        raise ValueError("invalid output prefix")
    if (template_name is None) != (mask_name is None):
        raise ValueError("scene template requires a person mask")
    for name in (template_name, mask_name):
        if name is not None and not re.fullmatch(r"corebot_identity_[0-9a-f]{24}\.png", name):
            raise ValueError("invalid scene upload name")
    # PhotoMaker replaces the class token immediately before its special token.
    traits = identity_description.removeprefix(
        "photorealistic head and shoulders portrait of an adult person, "
    ).strip()[:500]
    class_word = "woman" if re.search(r"\b(woman|female)\b", traits, re.I) else (
        "man" if re.search(r"\b(man|male)\b", traits, re.I) else "person"
    )
    conditioned = (
        f"a photograph of an adult {class_word} photomaker, "
        f"same fictional identity, {traits}, {scene_prompt.strip()}"
    )
    graph = {
        "3": {"class_type": "KSampler", "inputs": {
            "seed": seed, "steps": 28, "cfg": 7.0,
            "sampler_name": "dpmpp_2m", "scheduler": "karras",
            "denoise": 0.55 if template_name is not None else 1.0,
            "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0],
            "latent_image": ["5", 0],
        }},
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": model}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {
            "width": width, "height": height, "batch_size": 1,
        }},
        "6": {"class_type": "PhotoMakerEncode", "inputs": {
            "photomaker": ["10", 0], "image": ["11", 0],
            "clip": ["4", 1], "text": conditioned,
        }},
        "7": {"class_type": "CLIPTextEncode", "inputs": {
            "text": "child, minor, blurry, low quality, deformed, watermark, text",
            "clip": ["4", 1],
        }},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {
            "filename_prefix": filename_prefix, "images": ["8", 0],
        }},
        "10": {"class_type": "PhotoMakerLoader", "inputs": {
            "photomaker_model_name": PHOTOMAKER_MODEL_NAME,
        }},
        "11": {"class_type": "LoadImage", "inputs": {"image": upload_name}},
    }
    if template_name is not None and mask_name is not None:
        graph["3"]["inputs"].update({
            "positive": ["14", 0], "negative": ["14", 1],
            "latent_image": ["14", 2],
        })
        graph["12"] = {"class_type": "LoadImage", "inputs": {"image": template_name}}
        graph["13"] = {"class_type": "LoadImageMask", "inputs": {
            "image": mask_name, "channel": "red",
        }}
        graph["14"] = {"class_type": "InpaintModelConditioning", "inputs": {
            "positive": ["6", 0], "negative": ["7", 0],
            "pixels": ["12", 0], "vae": ["4", 2], "mask": ["13", 0],
            "noise_mask": True,
        }}
        del graph["5"]
    return graph


class ComfyIdentityClient:
    """Manage identities and generate variants through a loopback ComfyUI server."""

    def __init__(self, *, base_url: str = preview.DEFAULT_BASE_URL,
                 timeout_seconds: float = 300.0, poll_seconds: float = 1.0) -> None:
        self._preview = preview.ComfyPreviewClient(
            base_url=base_url, timeout_seconds=timeout_seconds, poll_seconds=poll_seconds,
        )
        self._semaphore = asyncio.Semaphore(1)

    async def create_identity(self, prompt: str, *, seed: int | None = None) -> IdentityRecord:
        """Generate and save a new adult person's portrait and metadata."""
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 1300:
            raise ValueError("prompt must contain 1–1300 characters")
        try:
            english_prompt = await prompt_translation.translate_if_needed(prompt)
        except prompt_translation.PromptTranslationError as exc:
            raise ComfyIdentityTranslationError("Cannot translate the appearance description") from exc
        full_prompt = "photorealistic head and shoulders portrait of an adult person, " + english_prompt
        request = preview.PreviewRequest(
            prompt=full_prompt, seed=seed,
            model="Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors",
        )
        request.validate()
        try:
            result = await self._preview.generate(request)
            png = await asyncio.to_thread(result.path.read_bytes)
            preview._validate_png(png, width=1024, height=1024)
            identity_id = uuid.uuid4().hex
            record = IdentityRecord(
                identity_id, _identity_dir(identity_id) / "reference.png",
                result.seed, full_prompt, result.model,
            )
            await asyncio.to_thread(_write_identity, record, png)
            return record
        except (preview.ComfyPreviewError, OSError) as exc:
            raise ComfyIdentityError("Could not create the identity portrait") from exc

    async def generate_photo(
        self, identity_id: str, scene_prompt: str, *, seed: int | None = None,
        scene_template_path: Path | None = None, person_mask_path: Path | None = None,
    ) -> IdentityPhotoResult:
        """Generate one fresh scene using the saved portrait as PhotoMaker input."""
        if not isinstance(scene_prompt, str) or not 1 <= len(scene_prompt.strip()) <= 1200:
            raise ValueError("scene_prompt must contain 1–1200 characters")
        if seed is not None and (type(seed) is not int or not 0 <= seed < 2**63):
            raise ValueError("seed is out of range")
        if (scene_template_path is None) != (person_mask_path is None):
            raise ValueError("scene template requires a person mask")
        if not (PHOTOMAKER_MODEL_DIR / PHOTOMAKER_MODEL_NAME).is_file():
            raise ComfyIdentityModelMissingError(
                f"PhotoMaker model is missing: {PHOTOMAKER_MODEL_DIR / PHOTOMAKER_MODEL_NAME}"
            )
        record, png = await asyncio.to_thread(_read_identity, identity_id)
        try:
            english_scene = await prompt_translation.translate_if_needed(scene_prompt)
        except prompt_translation.PromptTranslationError as exc:
            raise ComfyIdentityTranslationError("Cannot translate the scene description") from exc
        try:
            template_png = (
                await asyncio.to_thread(_read_scene_png, scene_template_path)
                if scene_template_path is not None else None
            )
            mask_png = (
                await asyncio.to_thread(_read_scene_png, person_mask_path)
                if person_mask_path is not None else None
            )
        except (OSError, preview.ComfyPreviewError) as exc:
            raise ComfyIdentityError("Scene template or person mask is invalid") from exc
        actual_seed = seed if seed is not None else uuid.uuid4().int % (2**63)
        token = uuid.uuid4().hex[:24]
        upload_name = f"corebot_identity_{token}.png"
        prefix = f"corebot_identity_{token}"
        template_name = f"corebot_identity_{uuid.uuid4().hex[:24]}.png" if template_png is not None else None
        mask_name = f"corebot_identity_{uuid.uuid4().hex[:24]}.png" if mask_png is not None else None
        graph = build_identity_workflow(
            scene_prompt=english_scene, model=record.model, seed=actual_seed,
            upload_name=upload_name, filename_prefix=prefix,
            template_name=template_name, mask_name=mask_name,
            identity_description=record.prompt,
        )
        timeout = aiohttp.ClientTimeout(total=self._preview.timeout_seconds)
        try:
            async with self._semaphore, asyncio.timeout(self._preview.timeout_seconds):
                try:
                    await lifecycle.ensure_running(self._preview.base_url)
                except lifecycle.ComfyUILifecycleError as exc:
                    raise ComfyIdentityError("Local ComfyUI could not be started") from exc
                async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
                    await self._upload_png(session, upload_name, png)
                    if template_png is not None and template_name is not None:
                        await self._upload_png(session, template_name, template_png)
                    if mask_png is not None and mask_name is not None:
                        await self._upload_png(session, mask_name, mask_png)
                    accepted = await self._preview._json(
                        session, "POST", "/prompt",
                        json_body={"prompt": graph, "client_id": str(uuid.uuid4())},
                    )
                    try:
                        prompt_id = str(uuid.UUID(accepted["prompt_id"]))
                    except (KeyError, TypeError, ValueError, AttributeError) as exc:
                        raise ComfyIdentityError("ComfyUI did not accept the identity workflow") from exc
                    if accepted.get("node_errors"):
                        raise ComfyIdentityError("ComfyUI rejected the identity workflow")
                    image = await self._preview._wait_for_image(session, prompt_id, prefix)
                    data = await self._preview._png(session, params={
                        "filename": image["filename"], "subfolder": "", "type": "output",
                    })
                    preview._validate_png(data, width=1024, height=1024)
                    if template_png is not None and mask_png is not None:
                        data = await asyncio.to_thread(
                            _composite_scene, template_png, mask_png, data,
                        )
                    path = _identity_dir(identity_id) / f"{uuid.uuid4().hex}.png"
                    temporary = path.with_suffix(".tmp")
                    try:
                        with temporary.open("xb") as handle:
                            handle.write(data)
                        os.replace(temporary, path)
                    finally:
                        temporary.unlink(missing_ok=True)
                    if scene_template_path is not None and await scene_quality.has_oversized_face(
                        scene_template_path, path,
                    ):
                        path.unlink(missing_ok=True)
                        raise ComfyIdentityQualityError(
                            "Generated face is much larger than in the template"
                        )
                    return IdentityPhotoResult(identity_id, path, prompt_id, actual_seed)
        except TimeoutError as exc:
            raise ComfyIdentityError("PhotoMaker generation timed out") from exc
        except (aiohttp.ClientError, preview.ComfyPreviewError, OSError) as exc:
            raise ComfyIdentityError("PhotoMaker generation failed") from exc

    async def _upload_png(self, session: aiohttp.ClientSession, name: str, png: bytes) -> None:
        form = aiohttp.FormData()
        form.add_field("image", png, filename=name, content_type="image/png")
        form.add_field("overwrite", "false")
        async with session.request(
            "POST", self._preview.base_url + "/upload/image", data=form,
            allow_redirects=False,
        ) as response:
            if response.status != 200 or (
                response.content_length is not None
                and response.content_length > preview.MAX_JSON_BYTES
            ):
                raise ComfyIdentityError("ComfyUI rejected the image upload")
            payload = await preview.ComfyPreviewClient._read_bounded(
                response, preview.MAX_JSON_BYTES,
            )
        try:
            uploaded = json.loads(payload)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ComfyIdentityError("ComfyUI returned invalid upload data") from exc
        if (not isinstance(uploaded, dict) or uploaded.get("name") != name
                or uploaded.get("subfolder") not in ("", None)
                or uploaded.get("type") not in ("input", None)):
            raise ComfyIdentityError("ComfyUI rejected the image upload")


_CLIENT = ComfyIdentityClient()


async def create_identity(prompt: str, *, seed: int | None = None) -> IdentityRecord:
    return await _CLIENT.create_identity(prompt, seed=seed)


async def generate_identity_photo(
    identity_id: str, scene_prompt: str, *, seed: int | None = None,
    scene_template_path: Path | None = None, person_mask_path: Path | None = None,
) -> IdentityPhotoResult:
    return await _CLIENT.generate_photo(
        identity_id, scene_prompt, seed=seed,
        scene_template_path=scene_template_path, person_mask_path=person_mask_path,
    )
