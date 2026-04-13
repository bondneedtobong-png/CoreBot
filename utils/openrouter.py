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


async def chat_completion(
    messages: List[dict[str, str]],
    model: str,
    *,
    max_tokens: int = 1024,
    temperature: float = 0.7,
) -> tuple[Optional[str], Optional[str]]:
    """
    POST /chat/completions.

    Returns:
        (assistant_text, error_message)
    """
    if not OPENROUTER_API_KEY:
        return None, "OPENROUTER_API_KEY не задан в .env"

    url = f"{OPENROUTER_BASE_URL.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    if OPENROUTER_HTTP_REFERER:
        headers["HTTP-Referer"] = OPENROUTER_HTTP_REFERER
    headers["X-Title"] = "CoreBot"

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
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
                    return None, msg

                data = json.loads(text)
                choices = data.get("choices") or []
                if not choices:
                    return None, "Пустой ответ модели"
                content = (choices[0].get("message") or {}).get("content")
                if content is None:
                    return None, "Нет content в ответе"
                return str(content).strip(), None
    except aiohttp.ClientError as e:
        log.error(f"OpenRouter сеть: {e}")
        return None, str(e)
    except Exception as e:
        log.error(f"OpenRouter: {e}")
        return None, str(e)


async def chat_completion_verbose(
    messages: List[dict[str, str]],
    model: str,
    *,
    max_tokens: int = 1024,
    temperature: float = 0.7,
) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    """
    Версия с кодом HTTP для ретраев/фолбэка.

    Returns:
        (assistant_text, error_message, http_status_or_none)
    """
    if not OPENROUTER_API_KEY:
        return None, "OPENROUTER_API_KEY не задан в .env", None

    url = f"{OPENROUTER_BASE_URL.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    if OPENROUTER_HTTP_REFERER:
        headers["HTTP-Referer"] = OPENROUTER_HTTP_REFERER
    headers["X-Title"] = "CoreBot"

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
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
