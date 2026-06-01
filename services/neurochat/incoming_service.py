from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from telethon import events

from bot.config import NEURO_MAX_CONCURRENT, NEURO_UNAVAILABLE_TEMPLATE
from database.crm_repositories import ClientClassCounterRepository, ClientInteractionRepository
from database.repositories import (
    ClientRepository,
    MailingLogRepository,
    NeuroChatRepository,
)
from database.session import session_scope
from services.neurochat.class_bridge import apply_neuro_class_commands
from services.neurochat.commands import extract_commands, user_asked_for_link
from services.neurochat.llm_service import generate_reply_with_retries_and_fallback
from services.neurochat.manager import prepare_incoming_context
from services.neurochat.post_actions import (
    persist_dialog_turn,
    process_send_link_command,
    process_stop_command,
    send_text_reply,
)
from utils.logger import log
from utils.telemetry import telemetry_emitter

_llm_sem = asyncio.Semaphore(NEURO_MAX_CONCURRENT)
# Per-dialog локи (account:peer) — сериализация обработки одного диалога.
# Словарь самоочищается через refcount: лок удаляется, как только его отпустил
# последний пользователь (никто не держит и не ждёт). Это убирает исторический
# рост _dialog_locks на каждый новый account:peer (утечка памяти на длинной
# дистанции).
_dialog_locks: dict[str, asyncio.Lock] = {}
_dialog_lock_refs: dict[str, int] = {}


def _lock_key(account_id: int, peer_id: int) -> str:
    return f"{account_id}:{peer_id}"


@asynccontextmanager
async def _dialog_lock(account_id: int, peer_id: int) -> AsyncIterator[None]:
    """
    Контекст-менеджер per-dialog лока с авто-очисткой.

    Лок создаётся лениво и удаляется, как только его отпустил последний
    пользователь. `_dialog_lock_refs` считает «держит + ждёт»: пока счётчик
    > 0, лок жив (его кто-то использует); как только дошёл до нуля — запись
    удаляется из обоих словарей. Безопасно в однопоточном asyncio: инкремент
    и финальная проверка выполняются без точек переключения (await) внутри.
    """
    key = _lock_key(account_id, peer_id)
    lock = _dialog_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _dialog_locks[key] = lock
    _dialog_lock_refs[key] = _dialog_lock_refs.get(key, 0) + 1
    try:
        async with lock:
            yield
    finally:
        remaining = _dialog_lock_refs.get(key, 1) - 1
        if remaining <= 0:
            _dialog_lock_refs.pop(key, None)
            # Удаляем лок, только если это тот же объект и он уже отпущен —
            # защита от гонки, если параллельно создали новый лок под тем же key.
            if _dialog_locks.get(key) is lock and not lock.locked():
                _dialog_locks.pop(key, None)
        else:
            _dialog_lock_refs[key] = remaining


async def _try_pulse_non_neuro_incoming(
    worker: Any,
    client: Any,
    text: str,
    event: events.NewMessage.Event,
) -> None:
    async with session_scope() as session:
        if not await MailingLogRepository.has_successful_outbound_to_client(
            session, worker.account.id, client.id
        ):
            return
        await ClientClassCounterRepository.increment(session, client.id, "pulse", 1)
        await ClientInteractionRepository.add(
            session,
            client_id=client.id,
            kind="pulse",
            direction="in",
            account_id=worker.account.id,
            body=(text[:4000] if text else None),
            telegram_message_id=getattr(event.message, "id", None),
        )


async def handle_incoming(worker: Any, event: events.NewMessage.Event) -> None:
    if not event.is_private:
        return
    if not worker.client:
        return

    text = (event.message.message or "").strip()
    if not text:
        return

    peer_uid = event.sender_id
    if peer_uid is None or peer_uid <= 0:
        return

    sender = await event.get_sender()
    if sender and getattr(sender, "bot", False):
        return

    async with session_scope() as session:
        client = await ClientRepository.get_by_telegram_user_id(session, int(peer_uid))
        if not client and sender and getattr(sender, "username", None):
            un = str(sender.username).strip().lstrip("@").lower()
            client = await ClientRepository.get_by_username(session, un)
        if not client:
            return

        mailing = await MailingLogRepository.get_mailing_for_neuro_reply(
            session, worker.account.id, client.id
        )
        if not mailing:
            await _try_pulse_non_neuro_incoming(worker, client, text, event)
            return

        prepared, prep_reason = await prepare_incoming_context(
            session,
            worker=worker,
            sender=sender,
            client=client,
            mailing=mailing,
            text=text,
            peer_uid=int(peer_uid),
        )
        if not prepared:
            log.info(
                f"Neuro incoming denied: reason={prep_reason} mailing={mailing.id} "
                f"account={worker.account.id} client={client.id}"
            )
            # Когда отказ из-за MANUAL-режима аккаунта — всё равно фиксируем
            # входящее сообщение для веб-панели: чтобы оператор видел поток в
            # реальном времени и мог ответить вручную.
            if prep_reason == "account_manual_mode":
                try:
                    await NeuroChatRepository.append(
                        session,
                        worker.account.id,
                        int(peer_uid),
                        "user",
                        text,
                    )
                    await ClientInteractionRepository.add(
                        session,
                        client_id=client.id,
                        account_id=worker.account.id,
                        mailing_id=mailing.id,
                        direction="in",
                        kind="manual_inbound",
                        body=(text[:4000] if text else None),
                        telegram_message_id=getattr(event.message, "id", None),
                    )
                    if not await MailingLogRepository.has_successful_outbound_to_client(
                        session, worker.account.id, client.id
                    ):
                        return
                    await ClientClassCounterRepository.increment(
                        session, client.id, "pulse", 1
                    )
                except Exception as exc:
                    log.warning(f"Manual-mode persist failed: {exc}")
            return

    assert prepared is not None
    client = prepared.client
    mailing = prepared.mailing
    model = prepared.model
    api_key = prepared.api_key
    link_for_prompt = prepared.link_for_prompt
    link_for_send = prepared.link_for_send
    messages = prepared.messages
    generation = prepared.generation
    use_typing_neuro = prepared.use_typing_neuro
    async with _dialog_lock(worker.account.id, int(peer_uid)):
        async with _llm_sem:
            reply, err = await generate_reply_with_retries_and_fallback(
                messages,
                model,
                api_key=api_key,
                generation=generation,
            )
        if not reply:
            log.warning(f"Neuro LLM: {err}; fallback to template")
            await telemetry_emitter.emit_event(
                "warning",
                "neuro_llm_fallback",
                "Neuro fallback template used",
                payload={"account_id": worker.account.id, "peer_id": int(peer_uid), "error": err or ""},
            )
            reply = NEURO_UNAVAILABLE_TEMPLATE

        reply = reply.replace("{link}", link_for_prompt).strip()
        reply, cmd_send_link, cmd_stop, cmd_accept, cmd_decline, cmd_hater = extract_commands(reply)
        asked_link = user_asked_for_link(text)
        log.info(f"Neuro command gate: asked_link={asked_link} has_send_link={cmd_send_link}")
        if cmd_send_link and not asked_link:
            log.info("Neuro command [SEND_LINK] ignored: user did not ask for link")
            cmd_send_link = False
            if not reply:
                retry_messages = list(messages)
                retry_messages.insert(
                    1,
                    {
                        "role": "system",
                        "content": (
                            "Пользователь НЕ просил ссылку. "
                            "Ответь обычным кратким сообщением по контексту. "
                            "Не используй служебные метки [SEND_LINK] и [STOP] "
                            "(ровно так, без пробелов внутри скобок)."
                        ),
                    },
                )
                retry_reply, retry_err = await generate_reply_with_retries_and_fallback(
                    retry_messages,
                    model,
                    api_key=api_key,
                    generation=generation,
                )
                if retry_reply:
                    retry_reply = retry_reply.replace("{link}", link_for_prompt).strip()
                    (
                        retry_reply,
                        retry_send_link,
                        retry_stop,
                        retry_accept,
                        retry_decline,
                        retry_hater,
                    ) = extract_commands(retry_reply)
                    if retry_send_link:
                        retry_reply = ""
                    if retry_reply:
                        reply = retry_reply
                        cmd_send_link = retry_send_link
                        cmd_stop = retry_stop
                        cmd_accept = retry_accept
                        cmd_decline = retry_decline
                        cmd_hater = retry_hater
                    else:
                        cmd_stop = cmd_stop or retry_stop
                        cmd_accept = cmd_accept or retry_accept
                        cmd_decline = cmd_decline or retry_decline
                        cmd_hater = cmd_hater or retry_hater
                else:
                    log.warning(f"Neuro retry (no commands) failed: {retry_err}")
                if not reply:
                    reply = "Понял, давай продолжим."

        await apply_neuro_class_commands(
            worker.account.id,
            client.id,
            mailing.id,
            cmd_accept,
            cmd_decline,
            cmd_hater,
        )
        should_send_text_reply = bool(reply) and (not cmd_send_link)

        await persist_dialog_turn(
            worker.account.id,
            int(peer_uid),
            text,
            reply if should_send_text_reply else None,
        )
        if should_send_text_reply:
            ok = await send_text_reply(
                worker,
                peer_uid=int(peer_uid),
                reply=reply,
                use_typing_neuro=use_typing_neuro,
                account_id=worker.account.id,
                client_id=client.id,
            )
            if not ok:
                return

        await process_send_link_command(
            worker,
            cmd_send_link=cmd_send_link,
            link_for_send=link_for_send,
            mailing_id=mailing.id,
            account_id=worker.account.id,
            client_id=client.id,
            peer_uid=int(peer_uid),
        )
        await process_stop_command(
            cmd_stop=cmd_stop,
            mailing_id=mailing.id,
            account_id=worker.account.id,
            client_id=client.id,
        )

