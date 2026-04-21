"""
Клиент OpenRouter (OpenAI-совместимый chat completions).
"""
from __future__ import annotations

import json
from typing import Any, List, Optional, Tuple

import aiohttp

from bot.config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL, OPENROUTER_HTTP_REFERER
from utils.logger import log

DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=120, connect=30)


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
    return await chat_completion_verbose(
        messages,
        model,
        api_key=api_key,
        generation=generation
        or _default_generation(max_tokens=max_tokens, temperature=temperature),
    )[:2]


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

    url = f"{OPENROUTER_BASE_URL.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if OPENROUTER_HTTP_REFERER:
        headers["HTTP-Referer"] = OPENROUTER_HTTP_REFERER
    headers["X-Title"] = "CoreBot"

    gen = dict(generation) if generation is not None else _default_generation(
        max_tokens=max_tokens, temperature=temperature
    )
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        **gen,
    }

    try:
        async with aiohttp.ClientSession(timeout=DEFAULT_TIMEOUT) as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                text = await resp.text()
                if resp.status != 200:
                    log.warning(f"OpenRouter HTTP {resp.status}: {text[:500]}")
                    try:
                        err = json.loads(text)
                        msg = err.get("error", {}).get("message") or text[:300]
                    except Exception:
                        msg = text[:300]
                    return None, msg, resp.status

                data = json.loads(text)
                choices = data.get("choices") or []
                if not choices:
                    return None, "Пустой ответ модели", 200
                content = (choices[0].get("message") or {}).get("content")
                if content is None:
                    return None, "Нет content в ответе", 200
                return str(content).strip(), None, 200
    except aiohttp.ClientError as e:
        log.error(f"OpenRouter сеть: {e}")
        return None, str(e), None
    except Exception as e:
        log.error(f"OpenRouter: {e}")
        return None, str(e), None
