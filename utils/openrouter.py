"""
Клиент OpenRouter (OpenAI-совместимый chat completions).
"""
from __future__ import annotations

from typing import Any, List, Optional, Tuple

from bot.config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL, OPENROUTER_HTTP_REFERER
from utils.openai_compatible import chat_completion_verbose as compatible_chat_completion_verbose


def _resolve_api_key(api_key: Optional[str]) -> str:
    return (api_key or "").strip() or OPENROUTER_API_KEY


def _default_generation(
    max_tokens: int = 1024,
    temperature: float = 0.7,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    gen: dict[str, Any] = {"max_tokens": max_tokens, "temperature": temperature}
    if extra:
        gen.update(extra)
    return gen


async def chat_completion(
    messages: List[dict[str, str]],
    model: str,
    *,
    max_tokens: int = 1024,
    temperature: float = 0.7,
    api_key: Optional[str] = None,
    generation: Optional[dict[str, Any]] = None,
) -> tuple[Optional[str], Optional[str]]:
    """
    POST /chat/completions.

    generation — полный набор параметров генерации (перекрывает max_tokens/temperature).
    """
    reply, error, _status = await chat_completion_verbose(
        messages,
        model,
        api_key=api_key,
        generation=generation
        or _default_generation(max_tokens=max_tokens, temperature=temperature),
    )
    return reply, error


async def chat_completion_verbose(
    messages: List[dict[str, str]],
    model: str,
    *,
    api_key: Optional[str] = None,
    generation: Optional[dict[str, Any]] = None,
    max_tokens: int = 1024,
    temperature: float = 0.7,
) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    """
    Версия с кодом HTTP для ретраев/фолбэка.

    generation — если передан, используется как тело параметров (temperature, max_tokens, top_p, …).
    Иначе собирается из max_tokens и temperature.

    Returns:
        (assistant_text, error_message, http_status_or_none)
    """
    key = _resolve_api_key(api_key)
    if not key:
        return None, "Ключ OpenRouter не задан (ни в боте, ни OPENROUTER_API_KEY в .env)", None

    headers = {"X-Title": "CoreBot"}
    if OPENROUTER_HTTP_REFERER:
        headers["HTTP-Referer"] = OPENROUTER_HTTP_REFERER

    gen = dict(generation) if generation is not None else _default_generation(
        max_tokens=max_tokens, temperature=temperature
    )
    return await compatible_chat_completion_verbose(
        messages, model, base_url=OPENROUTER_BASE_URL, api_key=key,
        generation=gen, extra_headers=headers, allow_loopback_http=True,
    )
