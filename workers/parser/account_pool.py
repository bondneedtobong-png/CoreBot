"""Ротация аккаунтов и Telethon-клиентов для parser-worker."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from telethon import TelegramClient

from database.models import Account, Proxy
from workers.manager import create_telethon_client, get_telethon_proxy_dict


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

    def __init__(self, snaps: list[AccountSnap]):
        if not snaps:
            raise ValueError("empty account list")
        self._snaps = snaps
        self._i = 0
        self._clients: dict[int, TelegramClient] = {}

    @classmethod
    def from_accounts(cls, accounts: list[Account]) -> "RotatingClients":
        snaps = [
            AccountSnap(id=a.id, session_name=a.session_name, proxy=a.proxy)
            for a in accounts
        ]
        return cls(snaps)

    def _session_path(self, snap: AccountSnap) -> Path:
        return Path("data/sessions") / f"{snap.session_name}.session"

    async def next_client(self) -> tuple[int, TelegramClient]:
        snap = self._snaps[self._i % len(self._snaps)]
        self._i += 1
        aid = snap.id
        client = self._clients.get(aid)
        if client is None:
            path = self._session_path(snap)
            if not path.exists():
                raise FileNotFoundError(f"session missing: {path}")
            proxy_cfg = get_telethon_proxy_dict(snap.proxy) if snap.proxy else None
            client = create_telethon_client(path, proxy_cfg)
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                raise RuntimeError(f"account {aid} not authorized")
            self._clients[aid] = client
        return aid, client

    async def disconnect_all(self) -> None:
        for cl in list(self._clients.values()):
            try:
                await cl.disconnect()
            except Exception:
                pass
        self._clients.clear()
