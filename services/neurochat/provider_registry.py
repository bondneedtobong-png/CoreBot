"""AI provider profiles shared by the bot and Control Plane."""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import DEFAULT_NEURO_MODEL, NEURO_MAX_TOKENS, OPENROUTER_API_KEY, OPENROUTER_BASE_URL
from database.models import AIProvider, InstanceSettings, Mailing
from database.repositories import InstanceSettingsRepository
from database.sqlite_pragmas import commit_with_busy_retry
from utils.crypto_openrouter import decrypt_openrouter_key
from utils.openai_compatible import validate_base_url
from utils.neuro_sampling import merge_sampling_for_request

_KINDS = {"openai", "deepseek", "custom"}
_DEFAULT_URLS = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com",
}
_GENERATION_KEYS = {"temperature", "top_p", "max_tokens", "presence_penalty", "frequency_penalty", "seed"}
_HEADER_KEYS = {"HTTP-Referer", "X-Title", "OpenAI-Organization", "OpenAI-Project"}


@dataclass(frozen=True, slots=True)
class ProviderRuntime:
    name: str
    base_url: str
    model: str
    api_key: str = field(repr=False)
    generation_defaults: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    is_openrouter: bool = False


def _fernet() -> Fernet:
    raw = (os.getenv("AI_PROVIDER_ENCRYPTION_KEY") or os.getenv("OPENROUTER_KEY_ENCRYPTION_KEY") or "").strip()
    if not raw:
        raise ValueError("AI_PROVIDER_ENCRYPTION_KEY is required before storing provider keys")
    try:
        return Fernet(raw.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise ValueError("AI_PROVIDER_ENCRYPTION_KEY is invalid") from exc


def encrypt_api_key(key: str) -> str:
    clean = (key or "").strip()
    if not clean or len(clean) > 4096 or any(ord(c) < 32 for c in clean):
        raise ValueError("invalid provider API key")
    return _fernet().encrypt(clean.encode("utf-8")).decode("ascii")


def decrypt_api_key(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError) as exc:
        raise ValueError("provider API key cannot be decrypted") from exc


def validate_config(config: dict[str, Any] | str | None) -> dict[str, Any]:
    if isinstance(config, str):
        if len(config) > 8192:
            raise ValueError("provider config is too large")
        try:
            config = json.loads(config)
        except ValueError as exc:
            raise ValueError("provider config must be JSON") from exc
    config = config or {}
    if not isinstance(config, dict) or set(config) - {"headers", "generation"}:
        raise ValueError("provider config supports only headers and generation")
    headers = config.get("headers", {})
    generation = config.get("generation", {})
    if not isinstance(headers, dict) or set(headers) - _HEADER_KEYS:
        raise ValueError("unsupported provider header")
    if not isinstance(generation, dict) or set(generation) - _GENERATION_KEYS:
        raise ValueError("unsupported generation parameter")
    for value in headers.values():
        if not isinstance(value, str) or len(value) > 256 or any(ord(c) < 32 for c in value):
            raise ValueError("invalid provider header value")
    for name, value in generation.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"invalid {name}")
        if not math.isfinite(value):
            raise ValueError(f"invalid {name}")
        if name == "max_tokens" and not 1 <= value <= 32768:
            raise ValueError("max_tokens out of range")
        if name == "temperature" and not 0 <= value <= 2:
            raise ValueError("temperature out of range")
        if name == "top_p" and not 0 < value <= 1:
            raise ValueError("top_p out of range")
        if name in {"presence_penalty", "frequency_penalty"} and not -2 <= value <= 2:
            raise ValueError(f"{name} out of range")
        if name == "seed" and not 0 <= value <= 2**31 - 1:
            raise ValueError("seed out of range")
    return {"headers": headers, "generation": generation}


def generation_for_provider(provider: ProviderRuntime, mailing_overrides: dict[str, Any]) -> dict[str, Any]:
    """Avoid sending OpenRouter-only defaults to other Chat Completions APIs."""
    if provider.is_openrouter:
        return merge_sampling_for_request(mailing_overrides)
    result = {"max_tokens": NEURO_MAX_TOKENS}
    result.update({key: value for key, value in mailing_overrides.items() if key in _GENERATION_KEYS})
    return result


def _validate_fields(*, name: str, kind: str, base_url: str, default_model: str) -> tuple[str, str, str, str]:
    name = name.strip()
    kind = kind.strip().lower()
    default_model = default_model.strip()
    if not name or len(name) > 80 or any(ord(c) < 32 for c in name):
        raise ValueError("provider name must have 1–80 characters")
    if kind not in _KINDS:
        raise ValueError("unsupported provider kind")
    if not default_model or len(default_model) > 255 or any(ord(c) < 32 for c in default_model):
        raise ValueError("provider model is required")
    if not base_url:
        base_url = _DEFAULT_URLS.get(kind, "")
    return name, kind, validate_base_url(base_url, allow_loopback_http=kind == "custom"), default_model


async def create_provider(
    session: AsyncSession, *, name: str, kind: str, base_url: str,
    api_key: str, default_model: str, config: dict[str, Any] | str | None = None,
) -> AIProvider:
    name, kind, base_url, default_model = _validate_fields(
        name=name, kind=kind, base_url=base_url, default_model=default_model,
    )
    item = AIProvider(
        name=name, kind=kind, base_url=base_url,
        api_key_ciphertext=encrypt_api_key(api_key), default_model=default_model,
        request_type="chat_completions", config_json=json.dumps(validate_config(config)),
    )
    session.add(item)
    await commit_with_busy_retry(session, op_name="ai-provider-create")
    await session.refresh(item)
    return item


async def list_providers(session: AsyncSession, *, page: int = 0, page_size: int = 5) -> list[AIProvider]:
    if page < 0 or page_size < 1 or page_size > 20:
        raise ValueError("invalid provider page")
    rows = await session.execute(select(AIProvider).order_by(AIProvider.id).offset(page * page_size).limit(page_size))
    return list(rows.scalars().all())


async def get_provider(session: AsyncSession, provider_id: int) -> AIProvider | None:
    return await session.get(AIProvider, provider_id)


async def update_provider(
    session: AsyncSession, provider_id: int, *, api_key: str | None = None,
    base_url: str | None = None, default_model: str | None = None,
    request_type: str | None = None, config: dict[str, Any] | str | None = None,
) -> AIProvider:
    item = await get_provider(session, provider_id)
    if item is None:
        raise ValueError("provider not found")
    if api_key is not None:
        item.api_key_ciphertext = encrypt_api_key(api_key)
    if base_url is not None:
        item.base_url = validate_base_url(base_url, allow_loopback_http=item.kind == "custom")
    if default_model is not None:
        item.default_model = _validate_fields(
            name=item.name, kind=item.kind, base_url=item.base_url, default_model=default_model,
        )[3]
    if request_type is not None:
        if request_type != "chat_completions":
            raise ValueError("only chat_completions is supported")
        item.request_type = request_type
    if config is not None:
        item.config_json = json.dumps(validate_config(config))
    await commit_with_busy_retry(session, op_name="ai-provider-update")
    return item


async def select_mailing_provider(session: AsyncSession, mailing_id: int, provider_id: int | None) -> Mailing:
    mailing = await session.get(Mailing, mailing_id)
    if mailing is None:
        raise ValueError("mailing not found")
    if provider_id is not None and await get_provider(session, provider_id) is None:
        raise ValueError("provider not found")
    mailing.neuro_provider_id = provider_id
    mailing.neuro_model = None  # Use the selected provider's default model.
    await commit_with_busy_retry(session, op_name="ai-provider-select")
    return mailing


async def select_default_provider(session: AsyncSession, provider_id: int | None) -> InstanceSettings:
    if provider_id is not None and await get_provider(session, provider_id) is None:
        raise ValueError("provider not found")
    settings = await InstanceSettingsRepository.get_row(session)
    settings.default_ai_provider_id = provider_id
    await commit_with_busy_retry(session, op_name="ai-provider-default")
    return settings


def _configured_runtime(item: AIProvider, mailing: Mailing) -> ProviderRuntime:
    if item.request_type != "chat_completions":
        raise ValueError("unsupported provider request type")
    config = validate_config(item.config_json)
    return ProviderRuntime(
        name=item.name, base_url=validate_base_url(item.base_url, allow_loopback_http=item.kind == "custom"),
        model=(mailing.neuro_model or "").strip() or item.default_model,
        api_key=decrypt_api_key(item.api_key_ciphertext),
        generation_defaults=config["generation"], headers=config["headers"],
    )


async def resolve_provider_async(session: AsyncSession, mailing: Mailing) -> ProviderRuntime:
    if mailing.neuro_provider_id is not None:
        item = await get_provider(session, mailing.neuro_provider_id)
        if item is None:
            raise ValueError("selected AI provider is missing")
        return _configured_runtime(item, mailing)
    key = await InstanceSettingsRepository.get_effective_openrouter_key(session)
    return ProviderRuntime("OpenRouter", OPENROUTER_BASE_URL,
                           (mailing.neuro_model or "").strip() or DEFAULT_NEURO_MODEL,
                           key or "", is_openrouter=True)


def resolve_provider_sync(session, mailing: Mailing) -> ProviderRuntime:
    if mailing.neuro_provider_id is not None:
        item = session.get(AIProvider, mailing.neuro_provider_id)
        if item is None:
            raise ValueError("selected AI provider is missing")
        return _configured_runtime(item, mailing)
    settings = session.get(InstanceSettings, 1)
    key = decrypt_openrouter_key(settings.openrouter_key_ciphertext) if settings else ""
    return ProviderRuntime("OpenRouter", OPENROUTER_BASE_URL,
                           (mailing.neuro_model or "").strip() or DEFAULT_NEURO_MODEL,
                           key or OPENROUTER_API_KEY or "", is_openrouter=True)


def resolve_default_provider_sync(session) -> ProviderRuntime:
    settings = session.get(InstanceSettings, 1)
    if settings and settings.default_ai_provider_id is not None:
        item = session.get(AIProvider, settings.default_ai_provider_id)
        if item is None:
            raise ValueError("default AI provider is missing")
        return _configured_runtime(item, Mailing(neuro_model=None))
    key = decrypt_openrouter_key(settings.openrouter_key_ciphertext) if settings else ""
    return ProviderRuntime("OpenRouter", OPENROUTER_BASE_URL, DEFAULT_NEURO_MODEL,
                           key or OPENROUTER_API_KEY or "", is_openrouter=True)
