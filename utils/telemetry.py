"""
Telemetry emitter for distributed agent mode.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from utils.time import utcnow_aware
from pathlib import Path
from typing import Any

import aiohttp

from utils.logger import log


_PHONE_RE = re.compile(r"\+?\d[\d\-\(\) ]{7,}\d")
_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_\-]{24,}\b")
_INVITE_RE = re.compile(r"https?://t\.me/\+\S+", re.IGNORECASE)


def _redact_text(text: str) -> str:
    s = text or ""
    s = _PHONE_RE.sub("[PHONE]", s)
    s = _INVITE_RE.sub("[INVITE_LINK]", s)
    s = _TOKEN_RE.sub("[TOKEN]", s)
    return s


def _redact_payload(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not payload:
        return payload
    out = {}
    for k, v in payload.items():
        if isinstance(v, str):
            out[k] = _redact_text(v)
        else:
            out[k] = v
    return out


@dataclass
class TelemetryEmitter:
    enabled: bool = field(default_factory=lambda: os.getenv("CP_AGENT_ENABLED", "0").lower() in ("1", "true", "yes", "on"))
    endpoint: str = field(default_factory=lambda: os.getenv("CP_INGEST_URL", "http://127.0.0.1:8081/ingest/batch"))
    token: str = field(default_factory=lambda: os.getenv("CP_AGENT_TOKEN", "").strip())
    agent_name: str = field(default_factory=lambda: os.getenv("CP_AGENT_NAME", "corebot-agent"))
    version: str = field(default_factory=lambda: os.getenv("CP_AGENT_VERSION", "dev"))
    schema_version: str = "1"
    flush_interval_sec: float = field(default_factory=lambda: float(os.getenv("CP_AGENT_FLUSH_SEC", "4")))
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=10000))
    _task: asyncio.Task | None = None
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _buffer_file: Path = field(default_factory=lambda: Path("data/telemetry_buffer.jsonl"))

    async def start(self) -> None:
        if not self.enabled or not self.token:
            return
        self._stop.clear()
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="telemetry-emitter")
        log.info("TelemetryEmitter started")

    async def stop(self) -> None:
        self._stop.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("TelemetryEmitter stopped")

    async def emit_event(self, level: str, category: str, message: str, payload: dict[str, Any] | None = None, code: str | None = None) -> None:
        if not self.enabled:
            return
        item = {
            "type": "event",
            "event": {
                "level": level,
                "category": category,
                "code": code,
                "message": _redact_text(message),
                "payload": _redact_payload(payload),
                "schema_version": self.schema_version,
                "ts": utcnow_aware().isoformat(),
            },
        }
        await self._enqueue(item)

    async def emit_metric(self, name: str, value: float, tags: dict[str, Any] | None = None) -> None:
        if not self.enabled:
            return
        item = {"type": "metric", "metric": {"name": name, "value": float(value), "tags": tags or {}}}
        await self._enqueue(item)

    async def _enqueue(self, item: dict[str, Any]) -> None:
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            self._buffer_file.parent.mkdir(parents=True, exist_ok=True)
            with self._buffer_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")

    async def _drain_buffer_file(self) -> list[dict[str, Any]]:
        if not self._buffer_file.exists():
            return []
        lines = self._buffer_file.read_text(encoding="utf-8").splitlines()
        self._buffer_file.unlink(missing_ok=True)
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
        return out

    async def _run(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self.flush_interval_sec)
            await self.flush_once()

    async def flush_once(self) -> None:
        if not self.enabled or not self.token:
            return
        items = await self._drain_buffer_file()
        while len(items) < 250 and not self.queue.empty():
            try:
                items.append(self.queue.get_nowait())
            except Exception:
                break
        if not items:
            return
        events = [x["event"] for x in items if x.get("type") == "event"]
        metrics = [x["metric"] for x in items if x.get("type") == "metric"]
        payload = {
            "agent_name": self.agent_name,
            "version": self.version,
            "events": events,
            "metrics": metrics,
        }
        headers = {"X-Agent-Token": self.token}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                async with s.post(self.endpoint, json=payload, headers=headers) as resp:
                    if resp.status >= 300:
                        raise RuntimeError(f"HTTP {resp.status}")
        except Exception as e:
            log.warning(f"Telemetry flush failed: {e}")
            self._buffer_file.parent.mkdir(parents=True, exist_ok=True)
            with self._buffer_file.open("a", encoding="utf-8") as f:
                for item in items:
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")


telemetry_emitter = TelemetryEmitter()
