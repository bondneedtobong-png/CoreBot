"""
Выгрузки для меню «База данных»: списки клиентов по классам, снимок аккаунтов,
переписка нейрочата за период. Чистое чтение, без побочных эффектов.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import (
    Account,
    Client,
    ClientClassCounter,
    ClientStatus,
    NeuroChatMessage,
)

# Классы, которые считаем «негативом» (исключаем из свежака/живых).
_NEGATIVE = ("bl", "stop")
# Классы «живого» отклика.
_ALIVE = ("pulse", "alive")


def client_handle(c: Client) -> str:
    """Строка для txt-списка: @username, либо tg:<id>, либо id:<pk>."""
    if c.username:
        return f"@{c.username}"
    if c.telegram_user_id:
        return f"tg:{c.telegram_user_id}"
    return f"id:{c.id}"


def _ids_with_classes(class_keys) -> select:
    return select(ClientClassCounter.client_id).where(
        ClientClassCounter.class_key.in_(list(class_keys)),
        ClientClassCounter.count > 0,
    )


async def export_fresh(session: AsyncSession, *, limit: int = 200_000) -> List[Client]:
    """Свежак: статус NEW и нет негативных классов (bl/stop)."""
    q = (
        select(Client)
        .where(
            Client.status == ClientStatus.NEW,
            ~Client.id.in_(_ids_with_classes(_NEGATIVE)),
        )
        .order_by(Client.id)
        .limit(limit)
    )
    return list((await session.execute(q)).scalars().all())


async def export_blacklist(session: AsyncSession, *, limit: int = 200_000) -> List[Client]:
    """Чёрный список: есть класс bl или stop."""
    q = (
        select(Client)
        .where(Client.id.in_(_ids_with_classes(_NEGATIVE)))
        .order_by(Client.id)
        .limit(limit)
    )
    return list((await session.execute(q)).scalars().all())


async def export_alive(session: AsyncSession, *, limit: int = 200_000) -> List[Client]:
    """Живые люди: ответившие (pulse/alive), но не в негативе."""
    q = (
        select(Client)
        .where(
            Client.id.in_(_ids_with_classes(_ALIVE)),
            ~Client.id.in_(_ids_with_classes(_NEGATIVE)),
        )
        .order_by(Client.id)
        .limit(limit)
    )
    return list((await session.execute(q)).scalars().all())


def render_client_list_txt(clients: List[Client], title: str) -> str:
    lines = [client_handle(c) for c in clients]
    body = "\n".join(lines)
    return f"# {title}: {len(lines)}\n{body}\n" if lines else f"# {title}: 0\n"


async def export_accounts_report(session: AsyncSession) -> str:
    """Снимок аккаунтов в текст (id, метка, телефон, @username, статус, статистика)."""
    rows = list(
        (await session.execute(select(Account).order_by(Account.id))).scalars().all()
    )
    out: List[str] = [
        f"# Аккаунты: {len(rows)}",
        "# id | список | телефон | @username | статус | sent/fail/today | spam | proxy | last_activity",
        "",
    ]
    for a in rows:
        last = a.last_activity.strftime("%Y-%m-%d %H:%M") if a.last_activity else "-"
        out.append(
            f"#{a.id} | {a.list_label or '-'} | {a.phone or '-'} | "
            f"@{a.username or '-'} | {a.status.value} | "
            f"{a.messages_sent or 0}/{a.messages_failed or 0}/{a.messages_today or 0} | "
            f"{'spam' if a.is_spam_blocked else 'ok'} | "
            f"{'proxy' if a.proxy_id else 'no-proxy'} | {last}"
        )
    return "\n".join(out) + "\n"


async def export_chats(
    session: AsyncSession,
    *,
    since: Optional[datetime] = None,
    limit: int = 20_000,
) -> str:
    """Переписка нейрочата за период (или вся), хронологически."""
    q = select(NeuroChatMessage).order_by(NeuroChatMessage.created_at.desc())
    if since is not None:
        q = q.where(NeuroChatMessage.created_at >= since)
    q = q.limit(limit)
    rows = list((await session.execute(q)).scalars().all())
    rows.reverse()  # к хронологическому порядку
    out: List[str] = [f"# Переписка нейрочата: {len(rows)} сообщений"]
    if since is not None:
        out.append(f"# С момента: {since.strftime('%Y-%m-%d %H:%M')} UTC")
    out.append("")
    for m in rows:
        ts = m.created_at.strftime("%Y-%m-%d %H:%M:%S") if m.created_at else "-"
        body = (m.content or "").replace("\n", " ⏎ ")
        out.append(f"[{ts}] acc{m.account_id} peer{m.peer_user_id} {m.role}: {body}")
    return "\n".join(out) + "\n"


def window_since(code: str) -> Optional[datetime]:
    """Маппинг кнопок периода переписки → начало окна (UTC, naive). None = всё."""
    deltas = {
        "db_chats_1d": timedelta(days=1),
        "db_chats_7d": timedelta(days=7),
        "db_chats_30d": timedelta(days=30),
    }
    if code == "db_chats_all":
        return None
    d = deltas.get(code)
    return (datetime.utcnow() - d) if d else None
