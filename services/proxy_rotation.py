"""Pick and claim replacement runtime proxies without sharing one across accounts."""
from __future__ import annotations

import asyncio
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Account, Proxy, ProxyGroup, ProxyType
from database.sqlite_pragmas import commit_with_busy_retry, run_with_busy_retry
from utils.time import utcnow_naive


MAX_CANDIDATES_PER_ATTEMPT = 12
FAILED_PROXY_RECHECK_AFTER = timedelta(minutes=10)


def is_transport_error(error: BaseException | str | None) -> bool:
    """Only connection failures justify a proxy change, not Telegram limits."""
    if error is None:
        return False
    if isinstance(error, (ConnectionError, OSError, asyncio.TimeoutError)):
        return True
    label = f"{type(error).__name__}: {error}".lower()
    return any(
        marker in label
        for marker in (
            "proxyerror",
            "host unreachable",
            "network is unreachable",
            "connection refused",
            "connection reset",
            "connection closed",
            "not connected",
            "timed out",
            "timeout",
            "socks error",
        )
    )


async def rotation_candidates(
    session: AsyncSession,
    *,
    group_id: int,
    limit: int = MAX_CANDIDATES_PER_ATTEMPT,
) -> list[Proxy]:
    """Take a bounded, round-robin slice of unassigned proxies in one runtime list."""
    group = await session.get(ProxyGroup, group_id)
    if group is None or group.purpose != "ACCOUNT_RUNTIME":
        return []
    occupied = select(Account.id).where(Account.proxy_id == Proxy.id).exists()
    retry_before = utcnow_naive() - FAILED_PROXY_RECHECK_AFTER
    rows = list(
        (
            await session.execute(
                select(Proxy)
                .where(
                    Proxy.group_id == group_id,
                    Proxy.proxy_type == ProxyType.SOCKS5,
                    Proxy.is_active.is_(True),
                    ~occupied,
                )
                .order_by(Proxy.id)
            )
        ).scalars()
    )
    rows = [
        p
        for p in rows
        if p.is_working or p.last_checked is None or p.last_checked <= retry_before
    ]
    if not rows:
        return []
    take = min(max(1, int(limit)), len(rows))
    next_cursor = await run_with_busy_retry(
        lambda: session.scalar(
            update(ProxyGroup)
            .where(ProxyGroup.id == group_id)
            .values(rr_cursor=func.coalesce(ProxyGroup.rr_cursor, 0) + take)
            .returning(ProxyGroup.rr_cursor)
        ),
        op_name="proxy-rotation-cursor",
    )
    await commit_with_busy_retry(session, op_name="proxy-rotation-cursor")
    start = (int(next_cursor or take) - take) % len(rows)
    return (rows[start:] + rows[:start])[:take]


async def claim_proxy(
    session: AsyncSession,
    *,
    account_id: int,
    expected_proxy_id: int,
    candidate_id: int,
    group_id: int,
) -> bool:
    """Compare-and-swap assignment; fail if another account claimed the proxy."""
    occupied = select(Account.id).where(
        Account.proxy_id == candidate_id,
        Account.id != account_id,
    ).exists()
    eligible = (
        select(Proxy.id)
        .join(ProxyGroup, ProxyGroup.id == Proxy.group_id)
        .where(
            Proxy.id == candidate_id,
            Proxy.group_id == group_id,
            Proxy.proxy_type == ProxyType.SOCKS5,
            Proxy.is_active.is_(True),
            ProxyGroup.purpose == "ACCOUNT_RUNTIME",
        )
        .exists()
    )
    result = await run_with_busy_retry(
        lambda: session.execute(
            update(Account)
            .where(
                Account.id == account_id,
                Account.proxy_id == expected_proxy_id,
                ~occupied,
                eligible,
            )
            .values(proxy_id=candidate_id, updated_at=utcnow_naive())
            .execution_options(synchronize_session=False)
        ),
        op_name="proxy-rotation-claim",
    )
    await commit_with_busy_retry(session, op_name="proxy-rotation-claim")
    return bool(result.rowcount == 1)
