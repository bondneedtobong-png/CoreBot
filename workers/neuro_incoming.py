"""Telethon hook for neuro incoming pipeline."""
from __future__ import annotations

from typing import TYPE_CHECKING

from telethon import events

from services.neurochat.incoming_service import handle_incoming
from utils.logger import log

if TYPE_CHECKING:
    from workers.manager import Worker


def register_neuro_handler_on_worker(worker: Worker) -> None:
    """Один раз на Worker после подключения Telethon."""
    if getattr(worker, "_neuro_handler_registered", False):
        return
    worker._neuro_handler_registered = True

    @worker.client.on(events.NewMessage(incoming=True))
    async def _on_neuro_private(event: events.NewMessage.Event) -> None:
        try:
            await handle_incoming(worker, event)
        except Exception as e:
            log.exception(f"Neuro incoming: {e}")
