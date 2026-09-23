"""

Сохранение полной переписки для accept: от начала удачной рассылки до успешного конца.

"""

from __future__ import annotations




import json

from datetime import datetime, timezone as tz

from typing import Any, List



from telethon.tl.types import PeerUser



from database.crm_repositories import (

    ClientAcceptTranscriptRepository,

    ClientMailSessionRepository,

)

from database.repositories import ClientRepository

from database.session import session_scope

from utils.background_tasks import background_tasks
from utils.logger import log





async def save_accept_transcript_json(

    mail_session_id: int,

    messages: List[dict[str, Any]],

) -> None:

    """Сохранить или обновить JSON массива сообщений для сессии рассылки."""

    raw = json.dumps(messages, ensure_ascii=False)

    async with session_scope() as session:

        await ClientAcceptTranscriptRepository.upsert(session, mail_session_id, raw)





def schedule_fetch_accept_transcript(mail_session_id: int) -> None:

    """Фоновая выгрузка истории через Telethon (не блокирует ответ нейрочата)."""



    async def _run() -> None:

        try:

            await fetch_and_store_accept_transcript_from_telethon(mail_session_id)

        except Exception as e:

            log.exception(f"accept transcript fetch failed ms={mail_session_id}: {e}")



    background_tasks.create(
        _run(),
        name=f"accept-transcript-{mail_session_id}",
    )





def _naive_utc(dt: datetime) -> datetime:

    if dt.tzinfo is not None:

        return dt.astimezone(tz.utc).replace(tzinfo=None)

    return dt





async def fetch_and_store_accept_transcript_from_telethon(

    mail_session_id: int,

) -> None:

    """

    По mail_session: peer = telegram_user_id клиента, аккаунт = worker Telethon,

    интервал [first_outbound_at, success_end_at].

    """

    from workers.manager import worker_manager



    async with session_scope() as session:

        ms = await ClientMailSessionRepository.get_by_id(session, mail_session_id)

        if not ms or not ms.first_outbound_at or not ms.success_end_at:

            log.warning(f"accept transcript: incomplete mail_session id={mail_session_id}")

            return

        client = await ClientRepository.get_by_id(session, ms.client_id)

        if not client or not client.telegram_user_id:

            log.warning(

                f"accept transcript: no telegram_user_id client_id={ms.client_id}"

            )

            return

        peer_uid = int(client.telegram_user_id)

        start = _naive_utc(ms.first_outbound_at)

        end = _naive_utc(ms.success_end_at)

        account_id = ms.account_id



    worker = worker_manager.workers.get(account_id)

    if not worker or not worker.client or not worker.is_connected:

        log.warning(

            f"accept transcript: worker offline account_id={account_id} ms={mail_session_id}"

        )

        return



    out: list[dict[str, Any]] = []

    try:

        async for m in worker.client.iter_messages(PeerUser(peer_uid), limit=500):

            if not m.date:

                continue

            md = m.date

            if md.tzinfo is not None:

                md = md.astimezone(tz.utc).replace(tzinfo=None)

            if md > end:

                continue

            if md < start:

                break

            out.append(

                {

                    "id": m.id,

                    "date": m.date.isoformat() if m.date else "",

                    "out": bool(m.out),

                    "text": (m.message or "")[:8000],

                }

            )

    except Exception as e:

        log.exception(f"accept transcript iter_messages: {e}")

        return



    out.reverse()

    await save_accept_transcript_json(mail_session_id, out)

    log.info(f"accept transcript stored ms={mail_session_id} messages={len(out)}")


