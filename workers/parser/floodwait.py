"""Обработка FloodWait: пауза, опционально смена аккаунта."""
from __future__ import annotations

import asyncio
import random
from typing import Awaitable, Callable, TypeVar

from telethon.errors import FloodWaitError
from utils.logger import log

T = TypeVar("T")


async def run_with_floodwait(
    op: Callable[[], Awaitable[T]],
    *,
    on_flood_seconds: Callable[[int], Awaitable[None]] | None = None,
    max_retries: int = 8,
) -> T:
    """
    Повтор op() при FloodWaitError с ожиданием seconds (+ jitter).
    on_flood_seconds(seconds) — залогировать / переключить аккаунт снаружи.
    """
    attempt = 0
    while True:
        try:
            return await op()
        except FloodWaitError as e:
            attempt += 1
            if attempt > max_retries:
                raise
            sec = int(getattr(e, "seconds", 0) or 0) + 1
            if on_flood_seconds:
                await on_flood_seconds(sec)
            jitter = random.uniform(0.2, 1.5)
            log.warning(f"FloodWait {sec}s (attempt {attempt}/{max_retries}), sleep {sec + jitter:.1f}s")
            await asyncio.sleep(sec + jitter)