"""Загрузка system-промпта из data/neuro/mailings/{id}/system.txt."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from bot.config import DEFAULT_NEURO_SYSTEM_PROMPT, NEURO_MAILING_PROMPTS_DIR

_COMMAND_TAGS = ("[SEND_LINK]", "[STOP]", "[ACCEPT]", "[DECLINE]", "[HATER]")
_COMMANDS_APPENDIX = (
    "\n\n# Служебные команды\n"
    "[SEND_LINK] — отправить ссылку, только если пользователь явно просит.\n"
    "[STOP] — остановить диалог; в CRM клиенту выставляется класс stop "
    "(дальнейшие ответы не идут).\n"
    "[ACCEPT] — явное согласие/готовность.\n"
    "[DECLINE] — явный отказ.\n"
    "[HATER] — токсичный/агрессивный отказ.\n\n"
    "# Плейсхолдеры в этом тексте (подставляются при ответе)\n"
    "{first_name}, {last_name}, {username}, {phone}, {account_id} — профиль аккаунта-отправителя; "
    "{mailing} — название кампании; {link} — ссылка из настроек; "
    "{peer_first_name}, {peer_last_name}, {peer_username} — собеседник в ЛС.\n\n"
    "# Примеры\n"
    "«Как вступить?» -> ответ + [SEND_LINK]\n"
    "«Не пиши больше» -> ответ + [STOP]\n"
    "«Ок, мне подходит» -> ответ + [ACCEPT]\n"
    "«Нет, спасибо» -> ответ + [DECLINE]\n"
    "«Отстань...» -> ответ + [HATER]\n"
)


def _txt(val: Any) -> str:
    if val is None:
        return ""
    return str(val).strip()


def apply_neuro_prompt_placeholders(
    text: str,
    *,
    link: str = "",
    account: Any | None = None,
    mailing: Any | None = None,
    client: Any | None = None,
    peer_sender: Any | None = None,
) -> str:
    """
    Подстановка плейсхолдеров в system-промпт перед отправкой в LLM.
    Аккаунт — отправитель (userbot); peer — собеседник в ЛС (Telethon User + CRM client).
    """
    acc_un = _txt(getattr(account, "username", None))
    acc_un_at = f"@{acc_un.lstrip('@')}" if acc_un else ""
    peer_un = ""
    if peer_sender and getattr(peer_sender, "username", None):
        peer_un = f"@{str(peer_sender.username).lstrip('@')}"
    elif client and getattr(client, "username", None):
        peer_un = f"@{str(client.username).lstrip('@')}"

    peer_fn = _txt(getattr(peer_sender, "first_name", None)) if peer_sender else ""
    peer_ln = _txt(getattr(peer_sender, "last_name", None)) if peer_sender else ""

    mid = int(getattr(mailing, "id", 0) or 0)
    mname = _txt(getattr(mailing, "name", None)) or (f"Рассылка #{mid}" if mid else "")

    out = text or ""
    mapping = {
        "{link}": _txt(link),
        "{first_name}": _txt(getattr(account, "first_name", None)),
        "{last_name}": _txt(getattr(account, "last_name", None)),
        "{username}": acc_un_at,
        "{phone}": _txt(getattr(account, "phone", None)),
        "{account_id}": str(int(getattr(account, "id", 0) or 0)),
        "{mailing}": mname,
        "{mailing_id}": str(mid),
        "{peer_first_name}": peer_fn,
        "{peer_last_name}": peer_ln,
        "{peer_username}": peer_un,
    }
    for key, val in mapping.items():
        out = out.replace(key, val)
    return out


def neuro_prompt_file_path(mailing_id: int) -> Path:
    return NEURO_MAILING_PROMPTS_DIR / str(mailing_id) / "system.txt"


def _ensure_commands_reference(text: str) -> str:
    current = text or ""
    if all(tag in current for tag in _COMMAND_TAGS):
        return current
    return f"{current.rstrip()}{_COMMANDS_APPENDIX}"


def load_system_prompt(mailing_id: int) -> str:
    path = neuro_prompt_file_path(mailing_id)
    if path.exists():
        try:
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return _ensure_commands_reference(text)
        except OSError:
            pass
    return _ensure_commands_reference(DEFAULT_NEURO_SYSTEM_PROMPT)


def prompt_file_exists(mailing_id: int) -> bool:
    return neuro_prompt_file_path(mailing_id).is_file()
