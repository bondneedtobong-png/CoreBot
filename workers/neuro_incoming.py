"""
Входящие личные сообщения → OpenRouter → ответ с того же аккаунта.
Контекст изолирован по (account_id, peer_user_id).
"""
from __future__ import annotations

import asyncio
import json
import random
import re
from typing import TYPE_CHECKING, Any

from telethon import events

from bot.config import (
    DEFAULT_NEURO_MODEL,
    NEURO_FALLBACK_MODELS,
    NEURO_MAX_CONCURRENT,
    NEURO_OPENROUTER_MAX_RETRIES,
    NEURO_OPENROUTER_RETRY_BASE_SEC,
    NEURO_UNAVAILABLE_TEMPLATE,
)
from database.crm_repositories import (
    ClientClassCounterRepository,
    ClientInteractionRepository,
    ClientMailSessionRepository,
    MailingLocalClassCounterRepository,
)
from database.repositories import (
    AccountRepository,
    ClientRepository,
    InstanceSettingsRepository,
    MailingLogRepository,
    MailingRepository,
    MailingTestRecipientRepository,
    NeuroActionRepository,
    NeuroChatRepository,
)
from database.session import session_scope
from utils.logger import log
from utils.links import normalize_public_link, plain_text_to_telegram_link_message
from utils.neuro_lang import classify_ru_en
from utils.neuro_prompts import apply_neuro_prompt_placeholders, load_system_prompt
from utils.neuro_sampling import merge_sampling_for_request, parse_sampling_mailing_column
from utils.openrouter import chat_completion_verbose
from utils.telemetry import telemetry_emitter

if TYPE_CHECKING:
    from workers.manager import Worker

_llm_sem = asyncio.Semaphore(NEURO_MAX_CONCURRENT)
_dialog_locks: dict[str, asyncio.Lock] = {}


def _lock_key(account_id: int, peer_id: int) -> str:
    return f"{account_id}:{peer_id}"


_VALID_HTTP_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
_CMD_SEND_LINK = "[SEND_LINK]"
_CMD_STOP = "[STOP]"
_LINK_INTENT_RE = re.compile(
    r"\b(ссылка|ссылку|ссылки|линк|link|url|invite|приглас|приглаш)\b",
    re.IGNORECASE,
)


def _extract_commands(
    raw_reply: str,
) -> tuple[str, bool, bool, bool, bool, bool]:
    # Модели часто пишут «[ SEND_LINK ]» с пробелами — жёстко только [send_link] ломалось.
    send_link = bool(re.search(r"\[\s*send_link\s*\]", raw_reply, re.IGNORECASE))
    stop_chat = bool(re.search(r"\[\s*stop\s*\]", raw_reply, re.IGNORECASE))
    accept = bool(re.search(r"\[\s*accept\s*\]", raw_reply, re.IGNORECASE))
    decline = bool(re.search(r"\[\s*decline\s*\]", raw_reply, re.IGNORECASE))
    hater = bool(re.search(r"\[\s*hater\s*\]", raw_reply, re.IGNORECASE))
    cleaned = raw_reply
    for pat in (
        r"\[\s*send_link\s*\]",
        r"\[\s*stop\s*\]",
        r"\[\s*accept\s*\]",
        r"\[\s*decline\s*\]",
        r"\[\s*hater\s*\]",
    ):
        cleaned = re.sub(pat, "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip()
    return cleaned, send_link, stop_chat, accept, decline, hater


async def _try_pulse_non_neuro_incoming(
    worker: Any,
    client: Any,
    text: str,
    event: events.NewMessage.Event,
) -> None:
    """Входящее в ЛС не в ветке нейрочата, но после успешной рассылки — инкремент pulse."""
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


async def _increment_client_class_for_mailing(
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


async def _client_has_positive_class(
    session,
    mailing,
    client_id: int,
    class_key: str,
) -> bool:
    """Учитывает тестовый режим (локальные счётчики) и глобальную CRM."""
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


async def _apply_neuro_class_commands(
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
            await _increment_client_class_for_mailing(session, mailing, client_id, "accept", 1)
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
            await _increment_client_class_for_mailing(session, mailing, client_id, "decline", 1)
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
            await _increment_client_class_for_mailing(session, mailing, client_id, "hater", 1)
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


def _user_asked_for_link(user_text: str) -> bool:
    t = (user_text or "").strip().lower()
    if not t:
        return False
    if _LINK_INTENT_RE.search(t):
        return True
    # Дополнительные разговорные формулировки запроса ссылки.
    hard_phrases = (
        "дай ссыл",
        "скинь ссыл",
        "пришли ссыл",
        "хочу ссыл",
        "можно ссыл",
        "кинь ссыл",
    )
    return any(p in t for p in hard_phrases)


def register_neuro_handler_on_worker(worker: Worker) -> None:
    """Один раз на Worker после подключения Telethon."""
    if getattr(worker, "_neuro_handler_registered", False):
        return
    worker._neuro_handler_registered = True

    @worker.client.on(events.NewMessage(incoming=True))
    async def _on_neuro_private(event: events.NewMessage.Event) -> None:
        try:
            await _handle_incoming(worker, event)
        except Exception as e:
            log.exception(f"Neuro incoming: {e}")


def _is_retryable_openrouter_error(status: int | None, err: str | None) -> bool:
    if status in (408, 409, 429, 500, 502, 503, 504):
        return True
    s = (err or "").lower()
    return any(x in s for x in ("rate-limit", "rate limit", "temporarily", "timeout", "overloaded"))


async def _generate_reply_with_retries_and_fallback(
    messages: list[dict[str, str]],
    primary_model: str,
    *,
    api_key: str,
    generation: dict[str, Any],
) -> tuple[str | None, str | None]:
    models: list[str] = []
    if primary_model:
        models.append(primary_model)
    for m in NEURO_FALLBACK_MODELS:
        if m not in models:
            models.append(m)

    if not models:
        models = [DEFAULT_NEURO_MODEL]

    last_err: str | None = None
    for model in models:
        log.info(f"Neuro LLM: try model={model}")
        for attempt in range(max(1, NEURO_OPENROUTER_MAX_RETRIES)):
            log.info(
                f"Neuro LLM request: model={model} attempt={attempt + 1}/{max(1, NEURO_OPENROUTER_MAX_RETRIES)}"
            )
            reply, err, status = await chat_completion_verbose(
                messages,
                model,
                api_key=api_key,
                generation=generation,
            )
            if reply:
                log.info(f"Neuro LLM success: model={model}")
                return reply, None
            last_err = err or f"HTTP {status}" if status else (err or "unknown error")
            log.warning(
                f"Neuro LLM fail: model={model} attempt={attempt + 1} "
                f"status={status} err={last_err}"
            )
            if not _is_retryable_openrouter_error(status, err):
                log.warning(f"Neuro LLM non-retryable: model={model}")
                break
            if attempt + 1 < max(1, NEURO_OPENROUTER_MAX_RETRIES):
                delay = NEURO_OPENROUTER_RETRY_BASE_SEC * (2**attempt)
                log.info(
                    f"Neuro LLM backoff: model={model} delay={delay:.1f}s before retry"
                )
                await asyncio.sleep(delay)
        log.warning(f"Neuro LLM switch fallback from model={model}")
    return None, last_err


async def _handle_incoming(worker: Worker, event: events.NewMessage.Event) -> None:
    if not event.is_private:
        return
    if not worker.client or not worker.is_connected:
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

        api_key = await InstanceSettingsRepository.get_effective_openrouter_key(session)
        if not api_key:
            return

        if await _client_has_positive_class(session, mailing, client.id, "stop"):
            return

        if client.telegram_user_id is None and peer_uid:
            await ClientRepository.set_telegram_user_id(session, client.id, int(peer_uid))

        model = (mailing.neuro_model or "").strip() or DEFAULT_NEURO_MODEL
        raw_link = (getattr(mailing, "community_link", None) or "").strip()
        link_for_prompt = normalize_public_link(raw_link)
        if not _VALID_HTTP_URL_RE.match(link_for_prompt):
            link_for_prompt = ""
        link_for_send = raw_link if _VALID_HTTP_URL_RE.match(raw_link) else ""
        account_row = await AccountRepository.get_by_id(session, worker.account.id)
        acct = account_row or worker.account
        raw_system = load_system_prompt(mailing.id)
        system = apply_neuro_prompt_placeholders(
            raw_system,
            link=link_for_prompt,
            account=acct,
            mailing=mailing,
            client=client,
            peer_sender=sender,
        )
        history = await NeuroChatRepository.get_messages_for_llm(
            session, worker.account.id, int(peer_uid)
        )
        if not history:
            lang = classify_ru_en(text)
            if lang in ("ru", "en"):
                await _increment_client_class_for_mailing(session, mailing, client.id, lang, 1)
                await ClientInteractionRepository.add(
                    session,
                    client_id=client.id,
                    account_id=worker.account.id,
                    mailing_id=mailing.id,
                    direction="in",
                    kind="neuro_lang",
                    body=(text[:500] if text else None),
                    payload_json=json.dumps({"lang": lang}),
                )
        messages = [{"role": "system", "content": system}]
        messages.extend(history)
        messages.append({"role": "user", "content": text})

        sampling_mailing = parse_sampling_mailing_column(
            getattr(mailing, "neuro_sampling_json", None)
        )
        generation = merge_sampling_for_request(sampling_mailing)

    use_typing_neuro = bool(getattr(mailing, "use_typing", True))

    key = _lock_key(worker.account.id, int(peer_uid))
    lock = _dialog_locks.setdefault(key, asyncio.Lock())
    async with lock:
        async with _llm_sem:
            reply, err = await _generate_reply_with_retries_and_fallback(
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
        (
            reply,
            cmd_send_link,
            cmd_stop,
            cmd_accept,
            cmd_decline,
            cmd_hater,
        ) = _extract_commands(reply)
        asked_link = _user_asked_for_link(text)
        log.info(
            f"Neuro command gate: asked_link={asked_link} has_send_link={cmd_send_link}"
        )
        if cmd_send_link and not asked_link:
            log.info("Neuro command [SEND_LINK] ignored: user did not ask for link")
            cmd_send_link = False
            # Если модель вернула только команду, делаем 1 повторный запрос
            # с явным запретом команд, чтобы не молчать.
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
                retry_reply, retry_err = await _generate_reply_with_retries_and_fallback(
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
                    ) = _extract_commands(retry_reply)
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
        await _apply_neuro_class_commands(
            worker.account.id,
            client.id,
            mailing.id,
            cmd_accept,
            cmd_decline,
            cmd_hater,
        )
        # [SEND_LINK] = tool-команда: текст LLM игнорируем полностью.
        send_text_reply = bool(reply) and (not cmd_send_link)

        async with session_scope() as session:
            await NeuroChatRepository.append(
                session, worker.account.id, int(peer_uid), "user", text
            )
        if send_text_reply:
            async with session_scope() as session:
                await NeuroChatRepository.append(
                    session, worker.account.id, int(peer_uid), "assistant", reply
                )

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
            if not ok:
                log.warning(f"Neuro send failed: {send_err}")
                await telemetry_emitter.emit_event(
                    "error",
                    "neuro_send_error",
                    "Neuro text send failed",
                    payload={"account_id": worker.account.id, "client_id": client.id, "error": send_err or ""},
                )
                return

        if cmd_send_link and link_for_send:
            log.info(
                f"Neuro command [SEND_LINK]: mailing={mailing.id} account={worker.account.id} client={client.id}"
            )
            async with session_scope() as session:
                await NeuroActionRepository.create(
                    session,
                    mailing_id=mailing.id,
                    account_id=worker.account.id,
                    client_id=client.id,
                    action="SEND_LINK",
                )
            link_plain, link_entities = plain_text_to_telegram_link_message(
                link_for_send.strip()
            )
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
                    payload={"account_id": worker.account.id, "client_id": client.id, "error": send_err2 or ""},
                )
        elif cmd_send_link and not link_for_send:
            log.warning(
                f"Neuro command [SEND_LINK] skipped: empty/invalid community_link for mailing={mailing.id}"
            )

        if cmd_stop:
            log.info(
                f"Neuro command [STOP]: mailing={mailing.id} account={worker.account.id} client={client.id}"
            )
            async with session_scope() as session:
                m2 = await MailingRepository.get_by_id(session, mailing.id)
                if m2:
                    await _increment_client_class_for_mailing(
                        session, m2, client.id, "stop", 1
                    )
                await NeuroActionRepository.create(
                    session,
                    mailing_id=mailing.id,
                    account_id=worker.account.id,
                    client_id=client.id,
                    action="STOP",
                )
                await ClientInteractionRepository.add(
                    session,
                    client_id=client.id,
                    account_id=worker.account.id,
                    mailing_id=mailing.id,
                    direction="in",
                    kind="stop",
                )
