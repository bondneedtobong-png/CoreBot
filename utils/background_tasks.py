"""Lifecycle management for one-shot background asyncio tasks."""
from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Coroutine
from typing import Any

from utils.logger import log


class BackgroundTaskSupervisor:
    """Keep strong references, observe failures, and drain tasks on shutdown."""

    def __init__(self, *, failure_history: int = 20) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()
        self._failures: deque[dict[str, str]] = deque(maxlen=max(1, failure_history))
        self._closed = False

    def create(self, coroutine: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine, name=name)
        if self._closed:
            task.cancel()
            return task
        self._tasks.add(task)
        task.add_done_callback(self._on_done)
        return task

    def _on_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is None:
            return
        task_name = task.get_name()
        self._failures.append(
            {"name": task_name, "error_type": type(exc).__name__}
        )
        log.opt(exception=(type(exc), exc, exc.__traceback__)).error(
            f"Background task failed: {task_name}"
        )

    def snapshot(self) -> dict[str, Any]:
        active_names = sorted(task.get_name() for task in self._tasks if not task.done())
        failures = list(self._failures)
        return {
            "active": len(active_names),
            "active_names": active_names,
            "failed": len(failures),
            "failures": failures,
        }

    async def shutdown(self) -> None:
        while True:
            pending = [task for task in self._tasks if not task.done()]
            if not pending:
                break
            # Give tasks created from another task's ``finally`` block one loop
            # turn so their own cleanup can run when they are cancelled.
            await asyncio.sleep(0)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()
        self._closed = True


background_tasks = BackgroundTaskSupervisor()

