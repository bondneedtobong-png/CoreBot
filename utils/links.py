"""
Утилиты нормализации ссылок.
"""
from __future__ import annotations

import html as html_module
import re
from urllib.parse import urlparse

# http(s) в plain-тексте; хвостовые знаки препинания не входят в URL.
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s<]+", re.IGNORECASE)


def normalize_public_link(raw: str) -> str:
    """
    Нормализует ссылку для отправки в мессенджер.

    Особенность:
    `https://t.me/+ABC` -> `https://t.me/joinchat/ABC`
    У некоторых клиентов plain-текст с `+` определяется хуже.
    """
    link = (raw or "").strip()
    if not link:
        return ""
    try:
        p = urlparse(link)
        host = (p.netloc or "").lower()
        path = p.path or ""
        if host in ("t.me", "telegram.me") and path.startswith("/+"):
            code = path[2:]
            if code:
                return f"https://t.me/joinchat/{code}"
    except Exception:
        return link
    return link


def linkify_plain_text_for_telegram_html(text: str) -> str:
    """
    Plain-текст → HTML для Telethon с parse_mode='html': оборачивает http(s) в <a href>.

    Без этого MTProto шлёт строку без MessageEntity*, и клиент не подсвечивает ссылки.
    """
    if not text:
        return ""
    out: list[str] = []
    pos = 0
    for m in _URL_IN_TEXT_RE.finditer(text):
        raw = m.group(0)
        url = raw
        trail = ".,;:!?"
        while len(url) > len("https://") + 2 and url[-1] in trail:
            url = url[:-1]
        url = normalize_public_link(url)
        out.append(html_module.escape(text[pos : m.start()]))
        eu = html_module.escape(url, quote=True)
        out.append(f'<a href="{eu}">{eu}</a>')
        pos = m.start() + len(raw)
    out.append(html_module.escape(text[pos:]))
    return "".join(out)


def plain_text_to_telegram_link_message(text: str) -> tuple[str, list]:
    """
    Plain-текст → (строка + entities) для send_message(..., formatting_entities=...).

    Надёжнее, чем parse_mode='html': те же MessageEntity*, без повторного разбора в клиенте
    и без «тихого» plain fallback при кривом HTML в ответе LLM.
    """
    from telethon.extensions import html as tg_html

    html_str = linkify_plain_text_for_telegram_html(text)
    if not html_str:
        return "", []
    try:
        plain, entities = tg_html.parse(html_str)
        return plain, list(entities or [])
    except Exception:
        import html as html_module

        return html_module.escape(text or ""), []

