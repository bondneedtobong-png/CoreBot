"""
Массовая чистка «флота»: прокси и аккаунты. Используется из меню бота для
удобного удаления старых/неактуальных прокси и аккаунтов.

FK-каскады в SQLite выключены, поэтому зависимые строки чистим явно.
Удаление .session файлов и отключение воркеров делает вызывающий хендлер
(нужны SESSIONS_DIR и worker_manager) — здесь только БД.
"""
from __future__ import annotations

from typing import List

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import (
    Account,
    ClientAcceptTranscript,
    ClientAliveWindow,
    ClientInteraction,
    ClientMailSession,
    MailingAccountState,
    MailingLog,
    NeuroChatMessage,
    OutboundQueue,
    ParsingTask,
    ParsingTaskLog,
    Proxy,
    WarmupLog,
    account_groups,
)


# ==================== Прокси ====================


async def count_proxies(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count(Proxy.id))) or 0)


async def count_free_proxies(session: AsyncSession) -> int:
    """Прокси, не назначенные ни одному аккаунту."""
    used = select(Account.proxy_id).where(Account.proxy_id.isnot(None))
    return int(
        await session.scalar(
            select(func.count(Proxy.id)).where(~Proxy.id.in_(used))
        )
        or 0
    )


async def delete_all_proxies(session: AsyncSession) -> int:
    """Отвязать прокси у всех аккаунтов и удалить все прокси."""
    n = await count_proxies(session)
    await session.execute(update(Account).values(proxy_id=None))
    await session.execute(delete(Proxy))
    await session.commit()
    return n


async def delete_free_proxies(session: AsyncSession) -> int:
    """Удалить только прокси без аккаунтов."""
    used = select(Account.proxy_id).where(Account.proxy_id.isnot(None))
    n = await count_free_proxies(session)
    await session.execute(delete(Proxy).where(~Proxy.id.in_(used)))
    await session.commit()
    return n


# ==================== Аккаунты ====================


async def count_accounts(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count(Account.id))) or 0)


async def count_accounts_with_proxy(session: AsyncSession) -> int:
    return int(
        await session.scalar(
            select(func.count(Account.id)).where(Account.proxy_id.isnot(None))
        )
        or 0
    )


async def detach_all_account_proxies(session: AsyncSession) -> int:
    """Отвязать прокси у всех аккаунтов (proxy_id = NULL). Не удаляет прокси."""
    n = await count_accounts_with_proxy(session)
    await session.execute(update(Account).values(proxy_id=None))
    await session.commit()
    return n


async def list_account_session_names(session: AsyncSession) -> List[str]:
    rows = await session.execute(select(Account.session_name))
    return [str(r[0]) for r in rows.all() if r[0]]


async def delete_all_accounts(session: AsyncSession) -> int:
    """
    Полное удаление ВСЕХ аккаунтов и их зависимых строк (диалоги нейрочата,
    логи отправок, mail-сессии, прогрев, очередь). CRM-клиенты/классы/рассылки
    сохраняются; в client_interactions обнуляется account_id.
    .session файлы удаляет вызывающий хендлер.
    """
    n = await count_accounts(session)
    # Полностью account-scoped таблицы — чистим целиком.
    await session.execute(delete(ClientAcceptTranscript))
    await session.execute(delete(ClientMailSession))
    await session.execute(delete(NeuroChatMessage))
    await session.execute(delete(MailingAccountState))
    await session.execute(delete(WarmupLog))
    await session.execute(delete(ClientAliveWindow))
    await session.execute(delete(OutboundQueue))
    await session.execute(delete(MailingLog))
    await session.execute(account_groups.delete())
    # Общие таблицы — обнуляем ссылку на аккаунт (данные сохраняем).
    await session.execute(update(ClientInteraction).values(account_id=None))
    await session.execute(update(ParsingTask).values(current_account_id=None))
    await session.execute(update(ParsingTaskLog).values(account_id=None))
    # Сами аккаунты.
    await session.execute(delete(Account))
    await session.commit()
    return n
