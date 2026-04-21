"""
Параметры сэмплирования OpenRouter для нейрочата (на уровне рассылки).
"""
from __future__ import annotations

import json
import re
from typing import Any

from bot.config import (
    NEURO_DEFAULT_FREQUENCY_PENALTY,
    NEURO_DEFAULT_MIN_P,
    NEURO_DEFAULT_PRESENCE_PENALTY,
    NEURO_DEFAULT_REPETITION_PENALTY,
    NEURO_DEFAULT_TEMPERATURE,
    NEURO_DEFAULT_TOP_A,
    NEURO_DEFAULT_TOP_P,
    NEURO_DEFAULT_TOP_K,
    NEURO_MAX_TOKENS,
)

SAMPLING_KEYS = frozenset(
    {
        "temperature",
        "top_p",
        "top_k",
        "min_p",
        "top_a",
        "frequency_penalty",
        "presence_penalty",
        "repetition_penalty",
        "max_tokens",
        "seed",
    }
)

_INT_KEYS = frozenset({"top_k", "max_tokens", "seed"})

# Порядок как в UI OpenRouter; код кнопки → ключ API
NEURO_PARAM_BUTTONS: list[tuple[str, str, str]] = [
    ("mt", "max_tokens", "📏 Max tokens"),
    ("te", "temperature", "🌡 Temperature"),
    ("tp", "top_p", "📐 Top P"),
    ("tk", "top_k", "🔢 Top K"),
    ("fp", "frequency_penalty", "📉 Freq. penalty"),
    ("pp", "presence_penalty", "📉 Pres. penalty"),
    ("rp", "repetition_penalty", "🔁 Rep. penalty"),
    ("mp", "min_p", "📊 Min P"),
    ("ta", "top_a", "📊 Top A"),
]

CODE_TO_KEY = {code: key for code, key, _ in NEURO_PARAM_BUTTONS}


def default_sampling_from_config() -> dict[str, Any]:
    """Глобальные дефолты (см. bot.config / .env)."""
    return {
        "max_tokens": NEURO_MAX_TOKENS,
        "temperature": NEURO_DEFAULT_TEMPERATURE,
        "top_p": NEURO_DEFAULT_TOP_P,
        "top_k": NEURO_DEFAULT_TOP_K,
        "frequency_penalty": NEURO_DEFAULT_FREQUENCY_PENALTY,
        "presence_penalty": NEURO_DEFAULT_PRESENCE_PENALTY,
        "repetition_penalty": NEURO_DEFAULT_REPETITION_PENALTY,
        "min_p": NEURO_DEFAULT_MIN_P,
        "top_a": NEURO_DEFAULT_TOP_A,
    }


def parse_sampling_mailing_column(raw: str | None) -> dict[str, Any]:
    if not raw or not str(raw).strip():
        return {}
    try:
        d = json.loads(raw)
        if isinstance(d, dict):
            return filter_sampling_dict(d)
    except Exception:
        return {}
    return {}


def filter_sampling_dict(d: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        lk = str(k).strip().lower()
        if lk not in SAMPLING_KEYS or v is None:
            continue
        try:
            if lk in _INT_KEYS:
                out[lk] = int(float(v))
            else:
                out[lk] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def parse_sampling_user_input(text: str) -> dict[str, Any]:
    """Совместимость / тесты: разбор key=value или JSON."""
    t = (text or "").strip()
    if not t:
        return {}
    if t.startswith("{"):
        try:
            d = json.loads(t)
            if isinstance(d, dict):
                return filter_sampling_dict(d)
        except Exception:
            return {}
        return {}
    out: dict[str, Any] = {}
    for line in t.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*(.+)$", line)
        if not m:
            continue
        key = m.group(1).strip().lower()
        val = m.group(2).strip().strip('"').strip("'")
        if key in SAMPLING_KEYS:
            out[key] = val
    return filter_sampling_dict(out)


def merge_sampling_for_request(mailing_overrides: dict[str, Any]) -> dict[str, Any]:
    """Дефолты из конфига + переопределения из рассылки → тело chat/completions."""
    base = default_sampling_from_config()
    for k, v in mailing_overrides.items():
        if k in SAMPLING_KEYS and v is not None:
            base[k] = v
    return {k: v for k, v in base.items() if v is not None}


def format_sampling_human(d: dict[str, Any]) -> str:
    """Одна строка для карточек."""
    if not d:
        return "по умолчанию (см. config / .env)"
    parts = [f"{k}={v}" for k, v in sorted(d.items())]
    return "; ".join(parts)


def format_sampling_menu_block(effective: dict[str, Any]) -> str:
    """Многострочный блок для экрана настройки."""
    lines = []
    order = [key for _, key, _ in NEURO_PARAM_BUTTONS]
    for k in order:
        if k in effective:
            lines.append(f"• <b>{k}</b>: <code>{effective[k]}</code>")
    for k, v in sorted(effective.items()):
        if k not in order:
            lines.append(f"• <b>{k}</b>: <code>{v}</code>")
    return "\n".join(lines) if lines else "<i>(пусто)</i>"


def coerce_param_value(param_key: str, raw: str) -> int | float:
    s = (raw or "").strip().replace(",", ".")
    if not s:
        raise ValueError("пусто")
    if param_key in _INT_KEYS:
        return int(float(s))
    return float(s)
