from __future__ import annotations

import json
import re
from dataclasses import dataclass

from bot.config import DEFAULT_NEURO_MODEL
from database.crm_repositories import ClientInteractionRepository
from database.repositories import AccountRepository, ClientRepository, InstanceSettingsRepository
from services.neurochat.class_bridge import client_has_positive_class, increment_client_class_for_mailing
from services.neurochat.config_service import get_global_config
from services.neurochat.dialog_service import get_history_for_llm
from services.neurochat.engagement_service import track_incoming_engagement
from services.neurochat.filters import check_client_filters
from utils.links import normalize_public_link
from utils.neuro_lang import classify_ru_en
from utils.neuro_prompts import apply_neuro_prompt_placeholders, load_system_prompt
from utils.neuro_sampling import merge_sampling_for_request, parse_sampling_mailing_column

_VALID_HTTP_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)


@dataclass(slots=True)
class PreparedIncomingContext:
    client: object
    mailing: object
    api_key: str
    model: str
    link_for_prompt: str
    link_for_send: str
    messages: list[dict[str, str]]
    generation: dict[str, object]
    use_typing_neuro: bool


async def can_process_incoming(session) -> bool:
    cfg = await get_global_config(session)
    return bool(cfg.enabled)


async def check_incoming_allowed(
    session, mailing, *, worker_connected: bool, client_id: int, account_id: int | None = None
) -> tuple[bool, str]:
    if not worker_connected:
        return False, "worker_disconnected"
    if not await can_process_incoming(session):
        return False, "global_disabled"
    if not bool(getattr(mailing, "neurochat_enabled", False)):
        return False, "mailing_local_disabled"
    if account_id is not None:
        acct = await AccountRepository.get_by_id(session, int(account_id))
        if acct is not None:
            mode = (getattr(acct, "ai_mode", None) or "AI_ACTIVE").upper()
            if mode == "MANUAL":
                return False, "account_manual_mode"
    client_allowed, client_reason = await check_client_filters(session, client_id)
    if not client_allowed:
        return False, client_reason
    return True, "ok"


async def prepare_incoming_context(
    session,
    *,
    worker,
    sender,
    client,
    mailing,
    text: str,
    peer_uid: int,
) -> tuple[PreparedIncomingContext | None, str]:
    allowed, deny_reason = await check_incoming_allowed(
        session,
        mailing,
        worker_connected=bool(worker.is_connected),
        client_id=client.id,
        account_id=worker.account.id,
    )
    if not allowed:
        return None, deny_reason

    api_key = await InstanceSettingsRepository.get_effective_openrouter_key(session)
    if not api_key:
        return None, "missing_openrouter_key"

    if await client_has_positive_class(session, mailing, client.id, "stop"):
        return None, "client_class_stop"

    await track_incoming_engagement(
        session,
        mailing=mailing,
        account_id=worker.account.id,
        client_id=client.id,
        body=text,
        telegram_message_id=None,
    )

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

    history = await get_history_for_llm(session, worker.account.id, int(peer_uid))
    if not history:
        lang = classify_ru_en(text)
        if lang in ("ru", "en"):
            await increment_client_class_for_mailing(session, mailing, client.id, lang, 1)
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

    messages = [{"role": "system", "content": system}, *history, {"role": "user", "content": text}]
    sampling_mailing = parse_sampling_mailing_column(
        getattr(mailing, "neuro_sampling_json", None)
    )
    generation = merge_sampling_for_request(sampling_mailing)

    return (
        PreparedIncomingContext(
            client=client,
            mailing=mailing,
            api_key=api_key,
            model=model,
            link_for_prompt=link_for_prompt,
            link_for_send=link_for_send,
            messages=messages,
            generation=generation,
            use_typing_neuro=bool(getattr(mailing, "use_typing", True)),
        ),
        "ok",
    )

