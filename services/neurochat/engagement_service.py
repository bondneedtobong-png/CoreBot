from __future__ import annotations

from datetime import datetime, timezone

from database.crm_repositories import ClientAliveWindowRepository, ClientInteractionRepository
from services.neurochat.class_bridge import increment_client_class_for_mailing


def build_alive_window_key(now_utc: datetime | None = None) -> int:
    ts = now_utc or datetime.now(timezone.utc)
    return int(ts.timestamp() // 3600)


async def track_incoming_engagement(
    session,
    *,
    mailing,
    account_id: int,
    client_id: int,
    body: str | None,
    telegram_message_id: int | None,
) -> None:
    # Pulse: каждое входящее в контексте нейрочата.
    await increment_client_class_for_mailing(session, mailing, client_id, "pulse", 1)
    await ClientInteractionRepository.add(
        session,
        client_id=client_id,
        account_id=account_id,
        mailing_id=mailing.id,
        direction="in",
        kind="pulse",
        body=(body[:4000] if body else None),
        telegram_message_id=telegram_message_id,
    )

    # Alive: не чаще 1 раза в 60 минут на связку (mailing, account, client).
    window_key = build_alive_window_key()
    created = await ClientAliveWindowRepository.create_if_absent(
        session,
        mailing_id=mailing.id,
        account_id=account_id,
        client_id=client_id,
        window_key=window_key,
    )
    if not created:
        return
    await increment_client_class_for_mailing(session, mailing, client_id, "alive", 1)
    await ClientInteractionRepository.add(
        session,
        client_id=client_id,
        account_id=account_id,
        mailing_id=mailing.id,
        direction="in",
        kind="alive",
        body=None,
        payload_json=f'{{"window_key": {window_key}}}',
    )

