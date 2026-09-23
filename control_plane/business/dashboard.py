"""
Бизнес-дашборд веб-панели: KPI, тайм-серия активности, топ-аккаунты,
распределение классов, активные рассылки. Все данные читаются sync-движком
из corebot.db (тот же файл, что и у бота, см. control_plane/business/db.py).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from utils.time import utcnow_naive

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, desc, func, select
from sqlalchemy.orm import Session

from control_plane.business.db import get_bot_db
from control_plane.business.schemas import (
    DashboardClassDistributionItem,
    DashboardMailingItem,
    DashboardRecentMessage,
    DashboardSummary,
    DashboardTimePoint,
    DashboardTopAccount,
)
from control_plane.deps import get_current_user
from control_plane.models import User
from database.models import (
    Account,
    Client,
    ClientClassCounter,
    Mailing,
    MailingLog,
    NeuroChatMessage,
    OutboundQueue,
)

router = APIRouter(prefix="/business/dashboard", tags=["business-dashboard"])


# ---------------------------- Summary -----------------------------------


def _utc_window(hours: int) -> datetime:
    return utcnow_naive() - timedelta(hours=int(hours))


@router.get("/summary", response_model=DashboardSummary)
def dashboard_summary(
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    now = utcnow_naive()
    since_24h = now - timedelta(hours=24)

    accounts_total = int(db.execute(select(func.count(Account.id))).scalar_one() or 0)
    accounts_authorized = int(
        db.execute(
            select(func.count(Account.id)).where(Account.status == "ACTIVE")
        ).scalar_one()
        or 0
    )
    accounts_manual = int(
        db.execute(
            select(func.count(Account.id)).where(Account.ai_mode == "MANUAL")
        ).scalar_one()
        or 0
    )
    accounts_ai = max(0, accounts_total - accounts_manual)

    mailings_running = int(
        db.execute(
            select(func.count(Mailing.id)).where(Mailing.status == "RUNNING")
        ).scalar_one()
        or 0
    )
    mailings_paused = int(
        db.execute(
            select(func.count(Mailing.id)).where(Mailing.status == "PAUSED")
        ).scalar_one()
        or 0
    )
    mailings_completed_24h = int(
        db.execute(
            select(func.count(Mailing.id)).where(
                Mailing.status == "COMPLETED",
                Mailing.completed_at.is_not(None),
                Mailing.completed_at >= since_24h,
            )
        ).scalar_one()
        or 0
    )

    # сообщения за 24 часа
    msgs_in_24h = int(
        db.execute(
            select(func.count(NeuroChatMessage.id)).where(
                NeuroChatMessage.role == "user",
                NeuroChatMessage.created_at >= since_24h,
            )
        ).scalar_one()
        or 0
    )
    msgs_out_24h = int(
        db.execute(
            select(func.count(NeuroChatMessage.id)).where(
                NeuroChatMessage.role == "assistant",
                NeuroChatMessage.created_at >= since_24h,
            )
        ).scalar_one()
        or 0
    )

    # ручные отправки за 24 ч
    manual_sent_24h = int(
        db.execute(
            select(func.count(OutboundQueue.id)).where(
                OutboundQueue.status == "sent",
                OutboundQueue.sent_at >= since_24h,
            )
        ).scalar_one()
        or 0
    )
    manual_pending = int(
        db.execute(
            select(func.count(OutboundQueue.id)).where(
                OutboundQueue.status == "pending"
            )
        ).scalar_one()
        or 0
    )
    manual_failed = int(
        db.execute(
            select(func.count(OutboundQueue.id)).where(
                OutboundQueue.status == "failed"
            )
        ).scalar_one()
        or 0
    )

    # рассылочные логи за 24 ч (контроль рассылок)
    mailing_sent_24h = int(
        db.execute(
            select(func.count(MailingLog.id)).where(
                MailingLog.success.is_(True),
                MailingLog.sent_at >= since_24h,
            )
        ).scalar_one()
        or 0
    )
    mailing_failed_24h = int(
        db.execute(
            select(func.count(MailingLog.id)).where(
                MailingLog.success.is_(False),
                MailingLog.sent_at >= since_24h,
            )
        ).scalar_one()
        or 0
    )

    clients_total = int(db.execute(select(func.count(Client.id))).scalar_one() or 0)
    dialogs_total = int(
        db.execute(
            select(
                func.count(
                    func.distinct(
                        NeuroChatMessage.account_id * 100000000
                        + NeuroChatMessage.peer_user_id
                    )
                )
            )
        ).scalar_one()
        or 0
    )
    dialogs_24h = int(
        db.execute(
            select(
                func.count(
                    func.distinct(
                        NeuroChatMessage.account_id * 100000000
                        + NeuroChatMessage.peer_user_id
                    )
                )
            ).where(NeuroChatMessage.created_at >= since_24h)
        ).scalar_one()
        or 0
    )

    return DashboardSummary(
        generated_at=now,
        accounts_total=accounts_total,
        accounts_authorized=accounts_authorized,
        accounts_ai=accounts_ai,
        accounts_manual=accounts_manual,
        mailings_running=mailings_running,
        mailings_paused=mailings_paused,
        mailings_completed_24h=mailings_completed_24h,
        messages_in_24h=msgs_in_24h,
        messages_out_24h=msgs_out_24h,
        manual_sent_24h=manual_sent_24h,
        manual_pending=manual_pending,
        manual_failed=manual_failed,
        mailing_sent_24h=mailing_sent_24h,
        mailing_failed_24h=mailing_failed_24h,
        clients_total=clients_total,
        dialogs_total=dialogs_total,
        dialogs_24h=dialogs_24h,
    )


# ---------------------------- Time-series ----------------------------------


@router.get("/timeseries", response_model=list[DashboardTimePoint])
def dashboard_timeseries(
    hours: int = Query(default=24, ge=1, le=24 * 14),
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    """
    Помесячно/посуточно/почасово сложно: используем bucket по часу (UTC).
    Возвращаем массив точек: ts (час), in/out/manual_sent.
    """
    since = _utc_window(hours)
    # SQLite-friendly bucket: strftime('%Y-%m-%d %H:00', created_at)
    bucket_neuro = func.strftime("%Y-%m-%d %H:00", NeuroChatMessage.created_at)
    bucket_outq = func.strftime("%Y-%m-%d %H:00", OutboundQueue.sent_at)

    in_rows = dict(
        db.execute(
            select(bucket_neuro, func.count(NeuroChatMessage.id))
            .where(
                NeuroChatMessage.role == "user",
                NeuroChatMessage.created_at >= since,
            )
            .group_by(bucket_neuro)
        ).all()
    )
    out_rows = dict(
        db.execute(
            select(bucket_neuro, func.count(NeuroChatMessage.id))
            .where(
                NeuroChatMessage.role == "assistant",
                NeuroChatMessage.created_at >= since,
            )
            .group_by(bucket_neuro)
        ).all()
    )
    manual_rows = dict(
        db.execute(
            select(bucket_outq, func.count(OutboundQueue.id))
            .where(
                OutboundQueue.status == "sent",
                OutboundQueue.sent_at >= since,
            )
            .group_by(bucket_outq)
        ).all()
    )

    keys = set(in_rows) | set(out_rows) | set(manual_rows)
    # дополним пустые часы (чтобы фронт мог рисовать ровный график)
    fill: dict[str, int] = {}
    cursor = since.replace(minute=0, second=0, microsecond=0)
    end = utcnow_naive().replace(minute=0, second=0, microsecond=0)
    while cursor <= end:
        fill[cursor.strftime("%Y-%m-%d %H:00")] = 0
        cursor += timedelta(hours=1)
    keys |= set(fill)

    points: list[DashboardTimePoint] = []
    for k in sorted(keys):
        points.append(
            DashboardTimePoint(
                ts=k,
                messages_in=int(in_rows.get(k, 0)),
                messages_out=int(out_rows.get(k, 0)),
                manual_sent=int(manual_rows.get(k, 0)),
            )
        )
    return points


# ---------------------------- Top accounts ---------------------------------


@router.get("/top_accounts", response_model=list[DashboardTopAccount])
def dashboard_top_accounts(
    hours: int = Query(default=24, ge=1, le=24 * 30),
    limit: int = Query(default=10, ge=1, le=50),
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    since = _utc_window(hours)
    in_count = func.sum(case((NeuroChatMessage.role == "user", 1), else_=0))
    out_count = func.sum(case((NeuroChatMessage.role == "assistant", 1), else_=0))
    rows = db.execute(
        select(
            NeuroChatMessage.account_id,
            in_count.label("in_cnt"),
            out_count.label("out_cnt"),
            func.count(NeuroChatMessage.id).label("total"),
        )
        .where(NeuroChatMessage.created_at >= since)
        .group_by(NeuroChatMessage.account_id)
        .order_by(desc("total"))
        .limit(limit)
    ).all()
    if not rows:
        return []
    account_ids = [int(r[0]) for r in rows]
    accounts = {
        a.id: a
        for a in db.execute(select(Account).where(Account.id.in_(account_ids)))
        .scalars()
        .all()
    }
    items: list[DashboardTopAccount] = []
    for account_id, in_cnt, out_cnt, total in rows:
        a = accounts.get(int(account_id))
        items.append(
            DashboardTopAccount(
                account_id=int(account_id),
                title=(a.list_label or a.username or a.phone or f"#{account_id}") if a else f"#{account_id}",
                ai_mode=(a.ai_mode if a else "AI_ACTIVE") or "AI_ACTIVE",
                messages_in=int(in_cnt or 0),
                messages_out=int(out_cnt or 0),
                total=int(total or 0),
            )
        )
    return items


# ---------------------------- Recent messages -------------------------------


@router.get("/recent_messages", response_model=list[DashboardRecentMessage])
def dashboard_recent_messages(
    limit: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    rows = db.execute(
        select(
            NeuroChatMessage.id,
            NeuroChatMessage.account_id,
            NeuroChatMessage.peer_user_id,
            NeuroChatMessage.role,
            NeuroChatMessage.content,
            NeuroChatMessage.created_at,
            Account.list_label,
            Account.username,
            Account.phone,
            Client.username,
        )
        .join(Account, Account.id == NeuroChatMessage.account_id, isouter=True)
        .join(Client, Client.telegram_user_id == NeuroChatMessage.peer_user_id, isouter=True)
        .order_by(desc(NeuroChatMessage.id))
        .limit(limit)
    ).all()

    items: list[DashboardRecentMessage] = []
    for (
        mid,
        acc_id,
        peer,
        role,
        content,
        created_at,
        a_label,
        a_username,
        a_phone,
        c_username,
    ) in rows:
        title = a_label or a_username or a_phone or f"#{acc_id}"
        peer_title = ("@" + c_username) if c_username else str(peer)
        items.append(
            DashboardRecentMessage(
                id=int(mid),
                account_id=int(acc_id),
                account_title=title,
                peer_user_id=int(peer),
                peer_title=peer_title,
                role=role,
                content=(content or "")[:240],
                created_at=created_at,
            )
        )
    return items


# ---------------------------- Class distribution ----------------------------


# Канонический набор классов в порядке отображения. Всё, что не в списке,
# попадает «как есть» под общим хвостом.
DEFAULT_CLASS_ORDER = [
    "accept",
    "send_link",
    "alive",
    "pulse",
    "decline",
    "dead",
    "stop",
    "bl",
]


@router.get("/class_distribution", response_model=list[DashboardClassDistributionItem])
def dashboard_class_distribution(
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    rows = db.execute(
        select(
            ClientClassCounter.class_key,
            func.count(func.distinct(ClientClassCounter.client_id)).label("clients"),
            func.coalesce(func.sum(ClientClassCounter.count), 0).label("events"),
        )
        .where(ClientClassCounter.count > 0)
        .group_by(ClientClassCounter.class_key)
    ).all()
    raw = {str(k): (int(c), int(e)) for (k, c, e) in rows}
    items: list[DashboardClassDistributionItem] = []
    seen: set[str] = set()
    for key in DEFAULT_CLASS_ORDER:
        c, e = raw.get(key, (0, 0))
        items.append(
            DashboardClassDistributionItem(class_key=key, clients=c, events=e)
        )
        seen.add(key)
    for key, (c, e) in raw.items():
        if key in seen:
            continue
        items.append(
            DashboardClassDistributionItem(class_key=key, clients=c, events=e)
        )
    return items


# ---------------------------- Active mailings -----------------------------


@router.get("/mailings_active", response_model=list[DashboardMailingItem])
def dashboard_mailings_active(
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    rows = (
        db.execute(
            select(Mailing).where(Mailing.status.in_(["RUNNING", "PAUSED"])).order_by(
                desc(Mailing.started_at)
            )
        )
        .scalars()
        .all()
    )
    items: list[DashboardMailingItem] = []
    for m in rows:
        items.append(
            DashboardMailingItem(
                id=int(m.id),
                name=m.name or f"#{m.id}",
                status=getattr(m.status, "value", str(m.status)),
                total=int(m.total_messages or 0),
                sent=int(m.messages_sent or 0),
                failed=int(m.messages_failed or 0),
                started_at=m.started_at,
            )
        )
    return items
