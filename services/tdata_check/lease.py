"""Lease proxy для проверки: один proxy — одна одновременная проверка (задача 12).

Lease-политика одной строкой: exclusive — proxy id выдаётся не более чем
одной concurrent-проверке; повторный acquire того же id ждёт release.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Optional


class CheckProxyLeaseManager:
    """Потокобезопасный exclusive lease пула proxy id."""

    def __init__(self) -> None:
        self._leased: set[int] = set()
        self._lock = threading.Lock()

    def try_acquire(self, candidates: list[int]) -> Optional[int]:
        """Выдать первый свободный id из ``candidates`` или None."""
        with self._lock:
            for pid in candidates:
                if pid not in self._leased:
                    self._leased.add(pid)
                    return pid
            return None

    def release(self, proxy_id: int) -> None:
        with self._lock:
            self._leased.discard(int(proxy_id))

    def leased_snapshot(self) -> set[int]:
        with self._lock:
            return set(self._leased)

    async def acquire_wait(
        self,
        candidates: list[int],
        *,
        timeout_sec: float = 60.0,
        poll_sec: float = 0.05,
    ) -> Optional[int]:
        """Ждать освобождения любого id из ``candidates`` (bounded wait)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.1, timeout_sec)
        while True:
            got = self.try_acquire(candidates)
            if got is not None:
                return got
            if loop.time() >= deadline:
                return None
            await asyncio.sleep(poll_sec)


__all__ = ["CheckProxyLeaseManager"]
