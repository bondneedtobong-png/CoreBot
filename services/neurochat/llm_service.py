from __future__ import annotations

import asyncio
from typing import Any

from bot.config import (
    DEFAULT_NEURO_MODEL,
    NEURO_FALLBACK_MODELS,
    NEURO_OPENROUTER_MAX_RETRIES,
    NEURO_OPENROUTER_RETRY_BASE_SEC,
)
from utils.logger import log
from utils.openrouter import chat_completion_verbose
from utils.openai_compatible import chat_completion_verbose as compatible_chat_completion_verbose
from services.neurochat.provider_registry import ProviderRuntime


def is_retryable_openrouter_error(status: int | None, err: str | None) -> bool:
    if status in (408, 409, 429, 500, 502, 503, 504):
        return True
    s = (err or "").lower()
    return any(x in s for x in ("rate-limit", "rate limit", "temporarily", "timeout", "overloaded"))


async def generate_reply_with_retries_and_fallback(
    messages: list[dict[str, str]],
    primary_model: str,
    *,
    api_key: str,
    generation: dict[str, Any],
    provider: ProviderRuntime | None = None,
) -> tuple[str | None, str | None]:
    models: list[str] = []
    if primary_model:
        models.append(primary_model)
    if provider is None or provider.is_openrouter:
        for m in NEURO_FALLBACK_MODELS:
            if m not in models:
                models.append(m)
    if not models:
        models = [provider.model if provider else DEFAULT_NEURO_MODEL]

    request_generation = dict(provider.generation_defaults) if provider else {}
    request_generation.update(generation)

    last_err: str | None = None
    for model in models:
        log.info(f"Neuro LLM: try model={model}")
        for attempt in range(max(1, NEURO_OPENROUTER_MAX_RETRIES)):
            log.info(
                f"Neuro LLM request: model={model} attempt={attempt + 1}/{max(1, NEURO_OPENROUTER_MAX_RETRIES)}"
            )
            if provider is not None and not provider.is_openrouter:
                reply, err, status = await compatible_chat_completion_verbose(
                    messages, model, base_url=provider.base_url,
                    api_key=api_key, generation=request_generation,
                    extra_headers=provider.headers,
                    allow_loopback_http=provider.base_url.startswith(("http://localhost", "http://127.0.0.1", "http://[::1]")),
                )
            else:
                reply, err, status = await chat_completion_verbose(
                    messages, model, api_key=api_key,
                    generation=request_generation,
                )
            if reply:
                log.info(f"Neuro LLM success: model={model}")
                return reply, None
            last_err = err or f"HTTP {status}" if status else (err or "unknown error")
            log.warning(
                f"Neuro LLM fail: model={model} attempt={attempt + 1} status={status} err={last_err}"
            )
            if not is_retryable_openrouter_error(status, err):
                log.warning(f"Neuro LLM non-retryable: model={model}")
                break
            if attempt + 1 < max(1, NEURO_OPENROUTER_MAX_RETRIES):
                delay = NEURO_OPENROUTER_RETRY_BASE_SEC * (2**attempt)
                log.info(f"Neuro LLM backoff: model={model} delay={delay:.1f}s before retry")
                await asyncio.sleep(delay)
        log.warning(f"Neuro LLM switch fallback from model={model}")
    return None, last_err

