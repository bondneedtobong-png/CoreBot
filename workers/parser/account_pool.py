"""Ротация аккаунтов и Telethon-клиентов для parser-worker."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from utils.time import utcnow_naive
from pathlib import Path
from typing import Optional

from telethon import TelegramClient

from bot.config import SESSIONS_DIR
from database.models import Account, Proxy, ProxyType
from utils.logger import log
from workers.manager import create_telethon_client, get_telethon_proxy_dict, worker_manager
from workers.session_lease import SessionLease


@dataclass
class AccountSnap:
    id: int
    session_name: str
    proxy: Optional[Proxy]


class RotatingClients:
    """
    Round-robin по account_ids; ленивое подключение клиентов.
    В конце задачи вызвать disconnect_all().
    """

    def __init__(self, snaps: list[AccountSnap], *, runtime_workers: bool = False):
        if not snaps:
            raise ValueError("empty account list")
        self._snaps = snaps
        self._runtime_workers = runtime_workers
        self._i = 0
        self._clients: dict[int, TelegramClient] = {}
        self._leases: dict[int, SessionLease] = {}
        self._cooldown_until: dict[int, datetime] = {}

    @classmethod
    def from_accounts(
        cls, accounts: list[Account], *, runtime_workers: bool = False
    ) -> "RotatingClients":
        snaps = [
            AccountSnap(id=a.id, session_name=a.session_name, proxy=a.proxy)
            for a in accounts
        ]
        return cls(snaps, runtime_workers=runtime_workers)

    def _session_path(self, snap: AccountSnap) -> Path:
        return SESSIONS_DIR / f"{snap.session_name}.session"

    async def next_client(self) -> tuple[int, TelegramClient]:
        attempts = len(self._snaps)
        now = utcnow_naive()
        last_err: Optional[Exception] = None
        for _ in range(attempts):
            snap = self._snaps[self._i % len(self._snaps)]
            self._i += 1
            aid = snap.id
            cd = self._cooldown_until.get(aid)
            if cd and cd > now:
                continue
            if self._runtime_workers:
                worker = worker_manager.workers.get(aid)
                client = worker.client if worker and worker.is_connected else None
                if client is not None and client.is_connected():
                    return aid, client
                last_err = RuntimeError(f"account {aid} is not connected in bot runtime")
                continue
            client = self._clients.get(aid)
            if client is not None:
                return aid, client
            lease: SessionLease | None = None
            client = None
            try:
                path = self._session_path(snap)
                if not path.exists():
                    raise FileNotFoundError(f"session missing: {path}")
                if (not snap.proxy or snap.proxy.proxy_type != ProxyType.SOCKS5
                        or not snap.proxy.is_active or not snap.proxy.is_working):
                    raise RuntimeError(f"account {aid} requires an active SOCKS5 proxy")
                proxy_cfg = get_telethon_proxy_dict(snap.proxy)
                lease = SessionLease(path).acquire()
                client = create_telethon_client(path, proxy_cfg)
                await client.connect()
                if not await client.is_user_authorized():
                    await client.disconnect()
                    raise RuntimeError(f"account {aid} not authorized")
                self._clients[aid] = client
                self._leases[aid] = lease
                self._cooldown_until.pop(aid, None)
                return aid, client
            except Exception as e:
                last_err = e
                self._cooldown_until[aid] = utcnow_naive() + timedelta(seconds=90)
                log.warning(f"parser account {aid} connect failed, cooldown 90s: {e}")
                try:
                    if client:
                        await client.disconnect()
                except Exception:
                    pass
                if lease:
                    lease.release()
                continue
        raise RuntimeError(f"no connectable parser accounts right now: {last_err}")

    async def disconnect_all(self) -> None:
        for aid, cl in list(self._clients.items()):
            try:
                await cl.disconnect()
            except Exception:
                pass
            finally:
                lease = self._leases.pop(aid, None)
                if lease:
                    lease.release()
        self._clients.clear()
