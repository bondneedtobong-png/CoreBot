"""
Агрегаты для экранов мониторинга (по всей базе или по группе аккаунтов).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database.models import (
    Account,
    AccountStatus,
    Client,
    ClientStatus,
    Mailing,
    MailingLog,
    MailingStatus,
    account_groups,
)


def _accounts_query(group_id: Optional[int]):
    q = select(Account).options(selectinload(Account.proxy))
    if group_id is not None:
        q = q.join(account_groups, Account.id == account_groups.c.account_id).where(
            account_groups.c.group_id == group_id
        )
    return q


async def load_accounts_for_scope(session: AsyncSession, group_id: Optional[int]) -> List[Account]:
    q = _accounts_query(group_id)
    if group_id is not None:
        q = q.distinct()
    result = await session.execute(q)
    return list(result.scalars().all())


async def build_scope_stats(session: AsyncSession, group_id: Optional[int]) -> Dict[str, Any]:
    """
    Статистика для рассылок: аккаунты в разрезе, клиенты (глобально), логи отправок по аккаунтам scope.
    """
    accounts = await load_accounts_for_scope(session, group_id)
    n = len(accounts)
    if group_id is not None and n == 0:
        return {"empty": True, "scope_label": "группа", "accounts": []}

    by_status: Dict[str, int] = {}
    for s in AccountStatus:
        by_status[s.value] = 0
    for a in accounts:
        by_status[a.status.value] = by_status.get(a.status.value, 0) + 1

    sum_sent = sum(a.messages_sent or 0 for a in accounts)
    sum_fail_acc = sum(a.messages_failed or 0 for a in accounts)
    sum_today = sum(a.messages_today or 0 for a in accounts)

    with_proxy = sum(1 for a in accounts if a.proxy_id is not None)
    without_proxy = n - with_proxy
    proxy_ok = sum(1 for a in accounts if a.proxy and a.proxy.is_working)
    proxy_bad = sum(
        1 for a in accounts if a.proxy_id is not None and a.proxy and not a.proxy.is_working
    )
    spam_blocked = sum(1 for a in accounts if a.is_spam_blocked)

    ids = [a.id for a in accounts]
    log_ok = log_fail = 0
    if ids:
        r_ok = await session.execute(
            select(func.count(MailingLog.id)).where(
                MailingLog.account_id.in_(ids), MailingLog.success == True
            )
        )
        log_ok = r_ok.scalar() or 0
        r_f = await session.execute(
            select(func.count(MailingLog.id)).where(
                MailingLog.account_id.in_(ids), MailingLog.success == False
            )
        )
        log_fail = r_f.scalar() or 0

    # Клиенты — глобально по базе (для контекста рассылки)
    cl_new = await session.scalar(
        select(func.count(Client.id)).where(Client.status == ClientStatus.NEW)
    )
    cl_contacted = await session.scalar(
        select(func.count(Client.id)).where(Client.status == ClientStatus.CONTACTED)
    )
    cl_invalid = await session.scalar(
        select(func.count(Client.id)).where(Client.status == ClientStatus.INVALID)
    )
    cl_blocked = await session.scalar(
        select(func.count(Client.id)).where(Client.status == ClientStatus.BLOCKED)
    )

    running = (
        await session.execute(select(Mailing).where(Mailing.status == MailingStatus.RUNNING).limit(1))
    ).scalars().first()

    return {
        "empty": False,
        "account_count": n,
        "by_status": by_status,
        "sum_sent": sum_sent,
        "sum_fail_acc": sum_fail_acc,
        "sum_today": sum_today,
        "with_proxy": with_proxy,
        "without_proxy": without_proxy,
        "proxy_ok": proxy_ok,
        "proxy_bad": proxy_bad,
        "spam_blocked": spam_blocked,
        "mailing_log_ok": log_ok,
        "mailing_log_fail": log_fail,
        "clients_new": cl_new or 0,
        "clients_contacted": cl_contacted or 0,
        "clients_invalid": cl_invalid or 0,
        "clients_blocked": cl_blocked or 0,
        "running_mailing": running,
        "accounts": accounts,
    }


async def build_global_accounts_dashboard(session: AsyncSession) -> Dict[str, Any]:
    """Только аккаунты (вся база): статусы + телефоны для ГЕО."""
    result = await session.execute(select(Account).options(selectinload(Account.proxy)))
    accounts = list(result.scalars().all())
    n = len(accounts)
    by_status: Dict[str, int] = {s.value: 0 for s in AccountStatus}
    for a in accounts:
        by_status[a.status.value] = by_status.get(a.status.value, 0) + 1

    sum_sent = sum(a.messages_sent or 0 for a in accounts)
    sum_today = sum(a.messages_today or 0 for a in accounts)
    with_proxy = sum(1 for a in accounts if a.proxy_id is not None)
    spam_blocked = sum(1 for a in accounts if a.is_spam_blocked)
    phones = [a.phone for a in accounts]

    return {
        "account_count": n,
        "by_status": by_status,
        "sum_sent": sum_sent,
        "sum_today": sum_today,
        "with_proxy": with_proxy,
        "without_proxy": n - with_proxy,
        "spam_blocked": spam_blocked,
        "phones": phones,
    }
