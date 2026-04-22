from __future__ import annotations

from database.crm_repositories import (
    ClientClassCounterRepository,
    ClientInteractionRepository,
    ClientMailSessionRepository,
    MailingLocalClassCounterRepository,
)
from database.repositories import MailingRepository, MailingTestRecipientRepository, NeuroActionRepository
from database.session import session_scope


async def increment_client_class_for_mailing(
    session,
    mailing,
    client_id: int,
    class_key: str,
    delta: int = 1,
) -> None:
    mode = (getattr(mailing, "audience_mode", None) or "classes").strip().lower()
    if mode == "test" and await MailingTestRecipientRepository.client_in_test_list(
        session, mailing.id, client_id
    ):
        await MailingLocalClassCounterRepository.increment(
            session, mailing.id, client_id, class_key, delta
        )
    else:
        await ClientClassCounterRepository.increment(session, client_id, class_key, delta)


async def client_has_positive_class(session, mailing, client_id: int, class_key: str) -> bool:
    k = (class_key or "").strip().lower()
    if not k:
        return False
    mode = (getattr(mailing, "audience_mode", None) or "classes").strip().lower()
    if mode == "test" and await MailingTestRecipientRepository.client_in_test_list(
        session, mailing.id, client_id
    ):
        n = await MailingLocalClassCounterRepository.get_count(
            session, mailing.id, client_id, k
        )
        return n > 0
    counts = await ClientClassCounterRepository.get_counts(session, client_id)
    return counts.get(k, 0) > 0


async def apply_neuro_class_commands(
    account_id: int,
    client_id: int,
    mailing_id: int,
    cmd_accept: bool,
    cmd_decline: bool,
    cmd_hater: bool,
) -> None:
    if not (cmd_accept or cmd_decline or cmd_hater):
        return
    async with session_scope() as session:
        mailing = await MailingRepository.get_by_id(session, mailing_id)
        if not mailing:
            return
        ms = await ClientMailSessionRepository.get_or_create(
            session, client_id, account_id, mailing_id
        )
        if cmd_accept:
            await increment_client_class_for_mailing(session, mailing, client_id, "accept", 1)
            await ClientMailSessionRepository.set_success_end(session, ms.id)
            await NeuroActionRepository.create(
                session, mailing_id, account_id, client_id, "ACCEPT"
            )
            await ClientInteractionRepository.add(
                session,
                client_id=client_id,
                account_id=account_id,
                mailing_id=mailing_id,
                direction="in",
                kind="accept",
            )
            from services.database.accept_transcript import schedule_fetch_accept_transcript

            schedule_fetch_accept_transcript(ms.id)
        if cmd_decline:
            await increment_client_class_for_mailing(session, mailing, client_id, "decline", 1)
            await NeuroActionRepository.create(
                session, mailing_id, account_id, client_id, "DECLINE"
            )
            await ClientInteractionRepository.add(
                session,
                client_id=client_id,
                account_id=account_id,
                mailing_id=mailing_id,
                direction="in",
                kind="decline",
            )
        if cmd_hater:
            await increment_client_class_for_mailing(session, mailing, client_id, "hater", 1)
            await NeuroActionRepository.create(
                session, mailing_id, account_id, client_id, "HATER"
            )
            await ClientInteractionRepository.add(
                session,
                client_id=client_id,
                account_id=account_id,
                mailing_id=mailing_id,
                direction="in",
                kind="hater",
            )

