"""Exclusive ownership of a Telethon SQLite session across processes.

The lock file is deliberately persistent: deleting it while held would allow a
second process to lock a different inode. The OS releases the lock on crash.
"""
from __future__ import annotations

from pathlib import Path
from typing import BinaryIO


class SessionBusyError(RuntimeError):
    """Another process already owns this Telegram session."""


class SessionLease:
    def __init__(self, session_path: str | Path):
        self.path = Path(f"{Path(session_path).resolve()}.lock")
        self._file: BinaryIO | None = None

    def acquire(self) -> "SessionLease":
        if self._file is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0)
            if __import__("os").name == "nt":
                import msvcrt

                # msvcrt.locking requires a byte to exist on Windows.
                if handle.seek(0, 2) == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise SessionBusyError(f"session is already in use: {self.path}") from exc
        self._file = handle
        return self

    def release(self) -> None:
        handle, self._file = self._file, None
        if handle is None:
            return
        try:
            handle.seek(0)
            if __import__("os").name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "SessionLease":
        return self.acquire()

    def __exit__(self, *_args: object) -> None:
        self.release()


def proxy_pool_import_lease(sessions_dir: str | Path, group_id: int) -> SessionLease:
    """Serialize proxy selection through account persistence for one pool."""
    return SessionLease(Path(sessions_dir) / f"proxy_pool_import_{group_id}")
