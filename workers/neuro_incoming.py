"""
Входящие личные сообщения → OpenRouter → ответ с того же аккаунта.
Контекст изолирован по (account_id, peer_user_id).
"""
from __future__ import annotations

import asyncio
import random
import re
from typing import TYPE_CHECKING

from telethon import events

from bot.config import (
    DEFAULT_NEURO_MODEL,
    NEURO_FALLBACK_MODELS,
    NEURO_MAX_CONCURRENT,
    NEURO_MAX_TOKENS,
    NEURO_OPENROUTER_MAX_RETRIES,
    NEURO_OPENROUTER_RETRY_BASE_SEC,
    NEURO_UNAVAILABLE_TEMPLATE,
    OPENROUTER_API_KEY,
)
from database.repositories import (
    ClientRepository,
    MailingLogRepository,
    NeuroActionRepository,
    NeuroChatRepository,
    NeuroStopRepository,
)
from database.session import session_scope
from utils.logger import log
from utils.links import normalize_public_link, plain_text_to_telegram_link_message
from utils.neuro_prompts import load_system_prompt
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


def _extract_commands(raw_reply: str) -> tuple[str, bool, bool]:
    # Модели часто пишут «[ SEND_LINK ]» с пробелами — жёстко только [send_link] ломалось.
    send_link = bool(re.search(r"\[\s*send_link\s*\]", raw_reply, re.IGNORECASE))
    stop_chat = bool(re.search(r"\[\s*stop\s*\]", raw_reply, re.IGNORECASE))
    cleaned = raw_reply
    cleaned = re.sub(r"\[\s*send_link\s*\]", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\[\s*stop\s*\]", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip()
    return cleaned, send_link, stop_chat


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
                max_tokens=NEURO_MAX_TOKENS,
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

    if not OPENROUTER_API_KEY:
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
            return

        if await NeuroStopRepository.is_blocked(session, worker.account.id, client.id):
            return

        if client.telegram_user_id is None and peer_uid:
            await ClientRepository.set_telegram_user_id(session, client.id, int(peer_uid))

        model = (mailing.neuro_model or "").strip() or DEFAULT_NEURO_MODEL
        raw_link = (getattr(mailing, "community_link", None) or "").strip()
        link_for_prompt = normalize_public_link(raw_link)
        if not _VALID_HTTP_URL_RE.match(link_for_prompt):
            link_for_prompt = ""
        link_for_send = raw_link if _VALID_HTTP_URL_RE.match(raw_link) else ""
        system = load_system_prompt(mailing.id).replace("{link}", link_for_prompt)
        history = await NeuroChatRepository.get_messages_for_llm(
            session, worker.account.id, int(peer_uid)
        )
        messages = [{"role": "system", "content": system}]
        messages.extend(history)
        messages.append({"role": "user", "content": text})

    use_typing_neuro = bool(getattr(mailing, "use_typing", True))

    key = _lock_key(worker.account.id, int(peer_uid))
    lock = _dialog_locks.setdefault(key, asyncio.Lock())
    async with lock:
        async with _llm_sem:
            reply, err = await _generate_reply_with_retries_and_fallback(messages, model)
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
        reply, cmd_send_link, cmd_stop = _extract_commands(reply)
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
                    retry_messages, model
                )
                if retry_reply:
                    retry_reply = retry_reply.replace("{link}", link_for_prompt).strip()
                    retry_reply, retry_send_link, retry_stop = _extract_commands(retry_reply)
                    if retry_send_link:
                        retry_reply = ""
                    if retry_reply:
                        reply = retry_reply
                        cmd_stop = cmd_stop or retry_stop
                else:
                    log.warning(f"Neuro retry (no commands) failed: {retry_err}")
                if not reply:
                    reply = "Понял, давай продолжим."
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
                await NeuroStopRepository.add(
                    session,
                    mailing_id=mailing.id,
                    account_id=worker.account.id,
                    client_id=client.id,
                )
                await NeuroActionRepository.create(
                    session,
                    mailing_id=mailing.id,
                    account_id=worker.account.id,
                    client_id=client.id,
                    action="STOP",
                )
