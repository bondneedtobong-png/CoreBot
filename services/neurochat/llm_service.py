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
) -> tuple[str | None, str | None]:
    models: list[str] = []
    if primary_model:
        models.append(primary_model)
    for m in NEURO_FALLBACK_MODELS:
        if m not in models:
            models.append(m)
    if not models:
        models = [DEFAULT_NEURO_MODEL]

    last_err: str | None = None
    for model in models:
        log.info(f"Neuro LLM: try model={model}")
        for attempt in range(max(1, NEURO_OPENROUTER_MAX_RETRIES)):
            log.info(
                f"Neuro LLM request: model={model} attempt={attempt + 1}/{max(1, NEURO_OPENROUTER_MAX_RETRIES)}"
            )
            reply, err, status = await chat_completion_verbose(
                messages,
                model,
                api_key=api_key,
                generation=generation,
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

