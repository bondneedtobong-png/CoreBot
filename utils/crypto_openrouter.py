"""
Хранение ключа OpenRouter: опциональное шифрование Fernet (OPENROUTER_KEY_ENCRYPTION_KEY).
Без ключа шифрования значение помечается префиксом «p:» (открытый текст на диске).
"""
from __future__ import annotations

import os

from utils.logger import log

_PLAIN_PREFIX = "p:"
_ENC_PREFIX = "e:"


def _fernet():
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        log.warning("cryptography не установлен — ключ OpenRouter только в открытом виде в БД")
        return None
    raw = os.getenv("OPENROUTER_KEY_ENCRYPTION_KEY", "").strip()
    if not raw:
        return None
    try:
        return Fernet(raw.encode("utf-8"))
    except Exception as e:
        log.error(f"OPENROUTER_KEY_ENCRYPTION_KEY невалиден для Fernet: {e}")
        return None


def encrypt_openrouter_key(plain: str) -> str:
    if not plain:
        return ""
    f = _fernet()
    if f is None:
        return _PLAIN_PREFIX + plain
    token = f.encrypt(plain.encode("utf-8")).decode("ascii")
    return _ENC_PREFIX + token


def decrypt_openrouter_key(stored: str | None) -> str:
    if not stored:
        return ""
    s = stored.strip()
    if s.startswith(_PLAIN_PREFIX):
        return s[len(_PLAIN_PREFIX) :]
    if s.startswith(_ENC_PREFIX):
        f = _fernet()
        if f is None:
            log.error("Ключ OpenRouter зашифрован, но OPENROUTER_KEY_ENCRYPTION_KEY не задан")
            return ""
        try:
            return f.decrypt(s[len(_ENC_PREFIX) :].encode("ascii")).decode("utf-8")
        except Exception as e:
            log.error(f"Не удалось расшифровать ключ OpenRouter: {e}")
            return ""
    # без префикса — старый формат (открытый текст)
    return s


def mask_api_key(key: str) -> str:
    k = (key or "").strip()
    if len(k) <= 8:
        return "•••" if k else "не задан"
    return f"{k[:4]}…{k[-4:]}"
