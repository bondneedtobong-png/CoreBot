"""Экспорт/импорт CRM-снимка: клиенты, счётчики классов, теги; merge по правилам спеки."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Set

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.crm_repositories import ClientClassCounterRepository
from database.models import Client, ClientTag
from database.repositories import ClientRepository


async def export_snapshot(session: AsyncSession) -> Dict[str, Any]:
    clients = list((await session.execute(select(Client).order_by(Client.id))).scalars())
    out_clients: List[Dict[str, Any]] = []
    for c in clients:
        cnt = await ClientClassCounterRepository.get_counts(session, c.id)
        tags_r = await session.execute(select(ClientTag.tag).where(ClientTag.client_id == c.id))
        tags = [str(r[0]) for r in tags_r.all()]
        out_clients.append(
            {
                "username": c.username,
                "telegram_user_id": c.telegram_user_id,
                "status": c.status.value if c.status else "new",
                "classes": cnt,
                "tags": tags,
            }
        )
    return {"version": 1, "clients": out_clients}


async def merge_snapshot(session: AsyncSession, data: Dict[str, Any]) -> Dict[str, int]:
    """
    Импорт с merge: для каждого username — max по счётчикам классов, теги union.
    """
    rows = data.get("clients") or []
    merged = 0
    for row in rows:
        un = str(row.get("username") or "").strip().lower().lstrip("@")
        if not un:
            continue
        client = await ClientRepository.get_by_username(session, un)
        if not client:
            client = await ClientRepository.create(session, un)
        classes: Dict[str, int] = row.get("classes") or {}
        for k, v in classes.items():
            key = str(k).strip().lower()
            if not key:
                continue
            cur = await ClientClassCounterRepository.get_counts(session, client.id)
            cur_v = int(cur.get(key, 0))
            new_v = max(cur_v, int(v))
            if new_v > cur_v:
                delta = new_v - cur_v
                await ClientClassCounterRepository.increment(session, client.id, key, delta)
        tags: Set[str] = set(row.get("tags") or [])
        for t in tags:
            tt = str(t).strip()
            if not tt:
                continue
            exists = await session.execute(
                select(ClientTag.id).where(
                    ClientTag.client_id == client.id,
                    ClientTag.tag == tt,
                )
            )
            if exists.scalar_one_or_none() is None:
                session.add(ClientTag(client_id=client.id, tag=tt))
        await session.commit()
        merged += 1
    return merged


def snapshot_to_json(data: Dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def snapshot_from_json(raw: str) -> Dict[str, Any]:
    return json.loads(raw)
