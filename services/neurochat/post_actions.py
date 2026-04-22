from __future__ import annotations

import random

from database.crm_repositories import ClientInteractionRepository
from database.repositories import MailingRepository, NeuroActionRepository
from database.session import session_scope
from services.neurochat.class_bridge import increment_client_class_for_mailing
from services.neurochat.dialog_service import append_assistant_message, append_user_message
from utils.links import plain_text_to_telegram_link_message
from utils.logger import log
from utils.telemetry import telemetry_emitter


async def persist_dialog_turn(account_id: int, peer_uid: int, user_text: str, assistant_text: str | None) -> None:
    async with session_scope() as session:
        await append_user_message(session, account_id, peer_uid, user_text)
    if assistant_text:
        async with session_scope() as session:
            await append_assistant_message(session, account_id, peer_uid, assistant_text)


async def send_text_reply(
    worker,
    *,
    peer_uid: int,
    reply: str,
    use_typing_neuro: bool,
    account_id: int,
    client_id: int,
) -> bool:
    base = random.uniform(5.0, 10.0) if use_typing_neuro else 0.0
    extra = min(len(reply) / 30.0, 55.0) if use_typing_neuro else 0.0
    typing_delay = base + extra
    out_plain, out_entities = plain_text_to_telegram_link_message(reply)
    ok, _mid, send_err, _peer = await worker.send_message_with_typing(
        int(peer_uid),
        out_plain,
        typing_delay=typing_delay,
        use_typing=use_typing_neuro,
        parse_mode=None,
        formatting_entities=out_entities or None,
    )
    if ok:
        return True
    log.warning(f"Neuro send failed: {send_err}")
    await telemetry_emitter.emit_event(
        "error",
        "neuro_send_error",
        "Neuro text send failed",
        payload={"account_id": account_id, "client_id": client_id, "error": send_err or ""},
    )
    return False


async def process_send_link_command(
    worker,
    *,
    cmd_send_link: bool,
    link_for_send: str,
    mailing_id: int,
    account_id: int,
    client_id: int,
    peer_uid: int,
) -> None:
    if cmd_send_link and link_for_send:
        log.info(
            f"Neuro command [SEND_LINK]: mailing={mailing_id} account={account_id} client={client_id}"
        )
        async with session_scope() as session:
            await NeuroActionRepository.create(
                session,
                mailing_id=mailing_id,
                account_id=account_id,
                client_id=client_id,
                action="SEND_LINK",
            )
        link_plain, link_entities = plain_text_to_telegram_link_message(link_for_send.strip())
        ok_link, _mid2, send_err2, _peer2 = await worker.send_message_with_typing(
            int(peer_uid),
            link_plain,
            typing_delay=0.0,
            use_typing=False,
            parse_mode=None,
            formatting_entities=link_entities or None,
        )
        if not ok_link:
            log.warning(f"Neuro SEND_LINK failed: {send_err2}")
            await telemetry_emitter.emit_event(
                "error",
                "neuro_send_link_error",
                "Neuro send link failed",
                payload={"account_id": account_id, "client_id": client_id, "error": send_err2 or ""},
            )
        return
    if cmd_send_link and not link_for_send:
        log.warning(
            f"Neuro command [SEND_LINK] skipped: empty/invalid community_link for mailing={mailing_id}"
        )


async def process_stop_command(
    *,
    cmd_stop: bool,
    mailing_id: int,
    account_id: int,
    client_id: int,
) -> None:
    if not cmd_stop:
        return
    log.info(
        f"Neuro command [STOP]: mailing={mailing_id} account={account_id} client={client_id}"
    )
    async with session_scope() as session:
        m2 = await MailingRepository.get_by_id(session, mailing_id)
        if m2:
            await increment_client_class_for_mailing(session, m2, client_id, "stop", 1)
        await NeuroActionRepository.create(
            session,
            mailing_id=mailing_id,
            account_id=account_id,
            client_id=client_id,
            action="STOP",
        )
        await ClientInteractionRepository.add(
            session,
            client_id=client_id,
            account_id=account_id,
            mailing_id=mailing_id,
            direction="in",
            kind="stop",
        )

