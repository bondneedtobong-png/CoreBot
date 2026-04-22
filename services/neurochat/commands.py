from __future__ import annotations

import re

_LINK_INTENT_RE = re.compile(
    r"\b(ссылка|ссылку|ссылки|линк|link|url|invite|приглас|приглаш)\b",
    re.IGNORECASE,
)


def extract_commands(raw_reply: str) -> tuple[str, bool, bool, bool, bool, bool]:
    """Извлекает служебные теги из ответа LLM и возвращает очищенный текст."""
    send_link = bool(re.search(r"\[\s*send_link\s*\]", raw_reply, re.IGNORECASE))
    stop_chat = bool(re.search(r"\[\s*stop\s*\]", raw_reply, re.IGNORECASE))
    accept = bool(re.search(r"\[\s*accept\s*\]", raw_reply, re.IGNORECASE))
    decline = bool(re.search(r"\[\s*decline\s*\]", raw_reply, re.IGNORECASE))
    hater = bool(re.search(r"\[\s*hater\s*\]", raw_reply, re.IGNORECASE))
    cleaned = raw_reply
    for pat in (
        r"\[\s*send_link\s*\]",
        r"\[\s*stop\s*\]",
        r"\[\s*accept\s*\]",
        r"\[\s*decline\s*\]",
        r"\[\s*hater\s*\]",
    ):
        cleaned = re.sub(pat, "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip(), send_link, stop_chat, accept, decline, hater


def user_asked_for_link(user_text: str) -> bool:
    """Грубая эвристика: пользователь явно просит ссылку/инвайт."""
    t = (user_text or "").strip().lower()
    if not t:
        return False
    if _LINK_INTENT_RE.search(t):
        return True
    hard_phrases = (
        "дай ссыл",
        "скинь ссыл",
        "пришли ссыл",
        "хочу ссыл",
        "можно ссыл",
        "кинь ссыл",
    )
    return any(p in t for p in hard_phrases)

