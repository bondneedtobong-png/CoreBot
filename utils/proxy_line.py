# -*- coding: utf-8 -*-
"""Единый парсер строк прокси для бота и веб-панели.

Поддерживаемые форматы (пробелы по краям игнорируются):
    host:port                       — без авторизации
    host:port:user:pass             — классика продавцов
    user:pass@host:port             — формат бота (bulk/single)
    host:port@user:pass             — формат прайсов пользователя
    [socks5://|http://] + любое из выше — явный тип, иначе socks5

Неоднозначность ``a:b@c:d`` (подходит и под user:pass@host:port, и под
host:port@user:pass) разрешается эвристикой: если часть до ``@`` — это
IP-адрес или hostname с точкой, считается форматом ``host:port@user:pass``,
иначе классическим ``user:pass@host:port``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class ParsedProxy:
    host: str
    port: int
    username: str | None = None
    password: str | None = None
    proxy_type: str = "socks5"  # "socks5" | "http"


_SCHEME_RE = re.compile(r"^(socks5|http)://(.+)$", re.IGNORECASE)
_HOST_PORT_RE = re.compile(r"^([^:\s]+):(\d{1,5})$")
_IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def _valid_port(raw: str) -> int | None:
    if not raw.isdigit():
        return None
    port = int(raw)
    return port if 1 <= port <= 65535 else None


def _valid_host(raw: str) -> str | None:
    s = raw.strip()
    if not s or any(c.isspace() for c in s) or ":" in s or "@" in s:
        return None
    return s


def _looks_like_host(host: str) -> bool:
    return bool(_IP_RE.match(host)) or ("." in host)


def parse_proxy_line(line: str) -> ParsedProxy | None:
    s = (line or "").strip()
    if not s:
        return None
    proxy_type = "socks5"
    m = _SCHEME_RE.match(s)
    if m:
        proxy_type = m.group(1).lower()
        s = m.group(2).strip()
        if not s:
            return None

    # host:port@user:pass  /  user:pass@host:port
    if "@" in s:
        if s.count("@") != 1:
            return None
        left, right = s.split("@", 1)
        # Сначала пробуем host:port@user:pass (формат прайсов).
        hm = _HOST_PORT_RE.match(left.strip())
        host = _valid_host(hm.group(1)) if hm else None
        port = _valid_port(hm.group(2)) if hm else None
        creds = right.strip().split(":", 1)
        if (
            host is not None
            and port is not None
            and len(creds) == 2
            and creds[0].strip()
            and _looks_like_host(host)
        ):
            return ParsedProxy(
                host=host,
                port=port,
                username=creds[0].strip(),
                password=creds[1].strip() or None,
                proxy_type=proxy_type,
            )
        # Иначе классика user:pass@host:port.
        m_auth = re.match(r"^([^:\s]+):([^@\s]+)@([^:\s]+):(\d{1,5})$", s)
        if m_auth:
            user, pwd, host_raw, port_raw = m_auth.groups()
            host = _valid_host(host_raw)
            port = _valid_port(port_raw)
            if host is not None and port is not None:
                return ParsedProxy(
                    host=host,
                    port=port,
                    username=user,
                    password=pwd,
                    proxy_type=proxy_type,
                )
        return None

    # host:port[:user[:pass]]
    parts = [p.strip() for p in s.split(":")]
    if len(parts) not in (2, 3, 4) or not parts[0] or not parts[1]:
        return None
    host = _valid_host(parts[0])
    port = _valid_port(parts[1])
    if host is None or port is None:
        return None
    username = parts[2] if len(parts) >= 3 and parts[2] else None
    password = parts[3] if len(parts) == 4 and parts[3] else None
    return ParsedProxy(
        host=host,
        port=port,
        username=username,
        password=password,
        proxy_type=proxy_type,
    )


def dedup_key(
    host: str, port: int, username: str | None, password: str | None
) -> tuple:
    """Ключ дедупликации импорта (как в bulk-импорте бота)."""
    return (host, int(port), username or "", password or "")
