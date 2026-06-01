"""
Удаление клиентов из меню «База данных».

FK-каскады в SQLite в этом проекте выключены (PRAGMA foreign_keys не
включается), поэтому зависимые строки удаляем явно, чтобы не плодить
«сирот». Все операции батчатся под лимит переменных SQLite (999).
"""
from __future__ import annotations

from typing import List, Sequence

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import (
    Client,
    ClientAcceptTranscript,
    ClientAliveWindow,
    ClientClassCounter,
    ClientInteraction,
    ClientMailSession,
    ClientStatus,
    ClientTag,
    MailingLocalClassCounter,
    MailingLog,
    MailingTestRecipient,
    OutboundQueue,
)

_CHUNK = 500


def _chunks(seq: Sequence[int], size: int = _CHUNK):
    for i in range(0, len(seq), size):
        yield list(seq[i : i + size])


async def find_client_ids_by_usernames(
    session: AsyncSession, usernames: List[str]
) -> List[int]:
    norm = sorted({u.strip().lstrip("@").lower() for u in usernames if u.strip()})
    if not norm:
        return []
    found: List[int] = []
    for batch in _chunks(norm):
        rows = await session.execute(
            select(Client.id).where(func.lower(Client.username).in_(batch))
        )
        found.extend(rows.scalars().all())
    return found


async def find_client_ids_by_class(session: AsyncSession, class_key: str) -> List[int]:
    rows = await session.execute(
        select(ClientClassCounter.client_id).where(
            ClientClassCounter.class_key == class_key.strip().lower(),
            ClientClassCounter.count > 0,
        )
    )
    return list(rows.scalars().all())


async def find_client_ids_by_status(
    session: AsyncSession, status: ClientStatus
) -> List[int]:
    rows = await session.execute(select(Client.id).where(Client.status == status))
    return list(rows.scalars().all())


async def delete_clients(session: AsyncSession, ids: Sequence[int]) -> int:
    """
    Полное удаление клиентов и их зависимых строк. Возвращает число
    удалённых клиентов. outbound_queue не удаляем — обнуляем client_id
    (история ручных отправок сохраняется).
    """
    ids = [int(x) for x in ids]
    if not ids:
        return 0

    for batch in _chunks(ids):
        # accept-транскрипты привязаны к mail_session, не к client напрямую.
        ms_ids = list(
            (
                await session.execute(
                    select(ClientMailSession.id).where(
                        ClientMailSession.client_id.in_(batch)
                    )
                )
            ).scalars().all()
        )
        for ms_batch in _chunks(ms_ids):
            await session.execute(
                delete(ClientAcceptTranscript).where(
                    ClientAcceptTranscript.mail_session_id.in_(ms_batch)
                )
            )

        for model in (
            ClientClassCounter,
            ClientTag,
            ClientInteraction,
            ClientMailSession,
            ClientAliveWindow,
            MailingTestRecipient,
            MailingLocalClassCounter,
            MailingLog,
        ):
            await session.execute(delete(model).where(model.client_id.in_(batch)))

        await session.execute(
            update(OutboundQueue)
            .where(OutboundQueue.client_id.in_(batch))
            .values(client_id=None)
        )
        await session.execute(delete(Client).where(Client.id.in_(batch)))

    await session.commit()
    return len(ids)
