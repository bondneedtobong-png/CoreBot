"""
Лёгкий in-memory мониторинг нейрочата: deny-причины, успехи и фолбэки за
скользящее окно N часов. Без БД и без парсинга логов — кольцевой буфер
событий с таймстампами. Сбрасывается при рестарте процесса (это
оперативный пульс «почему сейчас не отвечает», а не исторический отчёт).
"""
from __future__ import annotations

import time
from collections import deque
from threading import Lock
from typing import Deque, Dict, Tuple

# Стабильный порядок известных deny-причин для вывода в боте.
DENY_REASONS = (
    "global_disabled",
    "mailing_local_disabled",
    "worker_disconnected",
    "client_class_bl",
    "client_class_stop",
    "account_manual_mode",
)

_MAX_EVENTS = 5000


class NeuroMonitor:
    def __init__(self, max_events: int = _MAX_EVENTS) -> None:
        # (epoch_seconds, kind, reason); kind ∈ {deny, success, fallback}
        self._events: Deque[Tuple[float, str, str]] = deque(maxlen=max_events)
        self._lock = Lock()

    def _now(self) -> float:
        return time.time()

    def _record(self, kind: str, reason: str = "") -> None:
        with self._lock:
            self._events.append((self._now(), kind, reason))

    def record_deny(self, reason: str) -> None:
        self._record("deny", (reason or "unknown").strip())

    def record_success(self) -> None:
        self._record("success")

    def record_fallback(self) -> None:
        self._record("fallback")

    def summary(self, hours: float = 24.0) -> Dict[str, object]:
        cutoff = self._now() - float(hours) * 3600.0
        deny: Dict[str, int] = {}
        success = 0
        fallback = 0
        with self._lock:
            events = list(self._events)
        for ts, kind, reason in events:
            if ts < cutoff:
                continue
            if kind == "deny":
                deny[reason] = deny.get(reason, 0) + 1
            elif kind == "success":
                success += 1
            elif kind == "fallback":
                fallback += 1
        return {
            "hours": float(hours),
            "deny": deny,
            "deny_total": sum(deny.values()),
            "success": success,
            "fallback": fallback,
        }

    def clear(self) -> None:
        with self._lock:
            self._events.clear()


neuro_monitor = NeuroMonitor()
