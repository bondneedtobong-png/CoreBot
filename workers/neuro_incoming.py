"""Telethon hook for neuro incoming pipeline."""
from __future__ import annotations

from typing import TYPE_CHECKING

from telethon import events

from services.neurochat.incoming_service import handle_incoming
from utils.logger import log

if TYPE_CHECKING:
    from workers.manager import Worker


def register_neuro_handler_on_worker(worker: Worker) -> None:
    """Один раз на (Worker, client) после подключения Telethon.

    Флаг привязан к id(client): пересоздание client (reconnect) требует
    новой привязки, повторный вызов на том же client — no-op без дубля.
    """
    client = getattr(worker, "client", None)
    if client is None:
        return
    client_id = id(client)
    if getattr(worker, "_neuro_handler_client_id", None) == client_id:
        return
    worker._neuro_handler_client_id = client_id
    worker._neuro_handler_registered = True

    @worker.client.on(events.NewMessage(incoming=True))
    async def _on_neuro_private(event: events.NewMessage.Event) -> None:
        try:
            await handle_incoming(worker, event)
        except Exception as e:
            log.exception(f"Neuro incoming: {e}")
