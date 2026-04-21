"""Поиск клиентов: простой список классов и мини-DSL."""
from __future__ import annotations

import re
from typing import List, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Client, ClientClassCounter


def parse_simple_classes(line: str) -> Tuple[List[str], List[str]]:
    """
    Строка вида: include:ru,pulse exclude:bl
    или коротко: ru,pulse (только include), exclude через пробел и минус не используем —
    упрощённо: «include1,include2 exclude:bl,spam»
    """
    s = (line or "").strip()
    if not s:
        return [], []
    inc: List[str] = []
    exc: List[str] = []
    if "exclude:" in s.lower():
        parts = re.split(r"(?i)exclude:\s*", s, maxsplit=1)
        left = parts[0].replace("include:", "").strip()
        exc_s = parts[1].strip() if len(parts) > 1 else ""
        exc = [x.strip().lower() for x in exc_s.split(",") if x.strip()]
    else:
        left = s.replace("include:", "").strip()
    if "include:" in left.lower():
        left = re.sub(r"(?i)include:\s*", "", left).strip()
    inc = [x.strip().lower() for x in left.split(",") if x.strip()]
    return inc, exc


DSL_CLASS = re.compile(
    r"class:\s*([a-z0-9_]+)\s*(>=|<=|=)\s*(\d+)",
    re.IGNORECASE,
)


def parse_dsl(line: str) -> List[Tuple[str, str, int]]:
    """Возвращает список (class_key, op, value) для AND."""
    out: List[Tuple[str, str, int]] = []
    for m in DSL_CLASS.finditer(line or ""):
        key = m.group(1).lower()
        op = m.group(2)
        val = int(m.group(3))
        out.append((key, op, val))
    return out


async def search_clients_by_classes(
    session: AsyncSession,
    *,
    include: List[str],
    exclude: List[str],
    limit: int = 500,
) -> List[Client]:
    audience = {
        "client_status": "open",
        "include_classes": include,
        "exclude_classes": exclude or ["bl"],
    }
    from database.repositories import ClientRepository

    return (await ClientRepository.get_clients_for_mailing(session, audience))[:limit]


async def search_clients_dsl(
    session: AsyncSession,
    query: str,
    limit: int = 500,
) -> List[Client]:
    conds = parse_dsl(query)
    if not conds:
        return []
    from database.models import ClientStatus

    q = select(Client).where(
        ~Client.status.in_([ClientStatus.INVALID, ClientStatus.BLOCKED])
    )
    for class_key, op, val in conds:
        wc = [ClientClassCounter.class_key == class_key]
        if op == ">=":
            wc.append(ClientClassCounter.count >= val)
        elif op == "<=":
            wc.append(ClientClassCounter.count <= val)
        else:
            wc.append(ClientClassCounter.count == val)
        sub_ok = select(ClientClassCounter.client_id).where(*wc)
        q = q.where(Client.id.in_(sub_ok))
    q = q.order_by(Client.id).limit(limit)
    result = await session.execute(q)
    return list(result.scalars().all())
