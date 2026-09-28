"""Проверка TData через обязательный TDATA_CHECK proxy (задача 12).

Инварианты (покрыты тестами ``tests/test_tdata_check.py``):

* ``client_factory`` вызывается ТОЛЬКО с непустым ``proxy``-dict из
  переданного TDATA_CHECK pool; пустой pool → ``proxy_required`` без
  единого вызова factory (прямой коннект запрещён);
* ``Account``/``Account.proxy_id``/``data/sessions`` не затрагиваются —
  у движка вообще нет доступа к БД, сессии живут в системном tmp;
* error details обеззаражены через ``sanitize`` (без токенов,
  proxy-паролей, session-путей, полного TG-response).
"""

from __future__ import annotations

import asyncio
import re
import shutil
import sqlite3
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from control_plane.services.sanitize import sanitize
from utils.time import utcnow_naive

from services.tdata_check import limits as check_limits
from services.tdata_check.lease import CheckProxyLeaseManager
from services.tdata_check.models import TDataCheckItem, TDataCheckRun
from services.tdata_check.zip_safety import (
    ZipSafetyError,
    find_check_roots,
    is_tdata_root,
    safe_extract_zip,
)


@dataclass
class CheckProxy:
    """Прокси из TDATA_CHECK pool (без права светить password наружу)."""

    id: int
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None
    proxy_type: str = "socks5"
    name: Optional[str] = None

    @property
    def label(self) -> str:
        base = self.name or f"{self.host}:{self.port}"
        return f"{base}"

    def to_telethon_dict(self) -> dict[str, Any]:
        ptype = (self.proxy_type or "socks5").lower()
        if ptype not in ("socks5", "http", "mtproto"):
            ptype = "socks5"
        out: dict[str, Any] = {
            "proxy_type": ptype,
            "addr": self.host,
            "port": int(self.port),
            "rdns": True,
        }
        if self.username and self.password:
            out["username"] = self.username
            out["password"] = self.password
        return out


def check_proxy_from_orm(proxy: Any, *, name: Optional[str] = None) -> CheckProxy:
    """Адаптер ORM Proxy → CheckProxy (password остаётся только в dict)."""
    ptype = getattr(proxy, "proxy_type", "socks5")
    ptype_value = getattr(ptype, "value", ptype) or "socks5"
    return CheckProxy(
        id=int(proxy.id),
        host=str(proxy.host),
        port=int(proxy.port),
        username=getattr(proxy, "username", None),
        password=getattr(proxy, "password", None),
        proxy_type=str(ptype_value),
        name=name or getattr(proxy, "name", None),
    )


#: Эвристика страны по телефонному коду (longest-prefix match).
_COUNTRY_PREFIXES = (
    ("380", "UA"),
    ("375", "BY"),
    ("374", "AM"),
    ("994", "AZ"),
    ("995", "GE"),
    ("996", "KG"),
    ("998", "UZ"),
    ("373", "MD"),
    ("77", "KZ"),
    ("7", "RU"),
    ("1", "US"),
    ("44", "GB"),
    ("49", "DE"),
    ("33", "FR"),
    ("34", "ES"),
    ("39", "IT"),
    ("31", "NL"),
    ("48", "PL"),
    ("90", "TR"),
    ("972", "IL"),
    ("971", "AE"),
    ("91", "IN"),
    ("86", "CN"),
    ("81", "JP"),
    ("82", "KR"),
    ("55", "BR"),
)


def guess_country(phone: Optional[str]) -> Optional[str]:
    if not phone:
        return None
    digits = "".join(c for c in phone if c.isdigit())
    for prefix, country in _COUNTRY_PREFIXES:
        if digits.startswith(prefix):
            return country
    return None


def safe_detail(
    text: Any, *, tmp_dir: Optional[Path] = None, secrets: tuple[str, ...] = ()
) -> str:
    """Безопасный error detail: секреты пула + tmp-пути + sanitize, кап длины."""
    out = str(text or "")
    for secret in secrets:
        if secret:
            out = out.replace(str(secret), "***")
    if tmp_dir:
        out = out.replace(str(tmp_dir), "<tmp>")
    # Имена session-файлов — тоже PII-пути: item-01.session → <session>.
    out = re.sub(r"[\w\-.]+\.session\b", "<session>", out)
    cleaned = sanitize({"detail": out})["detail"]
    result = str(cleaned)
    return result[:400]


def _pool_secrets(proxies: list["CheckProxy"]) -> tuple[str, ...]:
    """Пароли/логины check-пула — никогда не попадают в error details."""
    secrets: list[str] = []
    for proxy in proxies or []:
        for value in (proxy.password, proxy.username):
            if value:
                secrets.append(str(value))
    return tuple(secrets)


def map_exception(
    exc: BaseException, *, tmp_dir: Optional[Path] = None, secrets: tuple[str, ...] = ()
) -> tuple[str, str, str, Optional[int]]:
    """Mapping Telegram/RPC-исключений → (status, error_code, detail, retry_after).

    Импорты telethon — ленивые: домен не падает без telethon в окружении.
    """
    try:
        from telethon.errors import (
            AuthKeyUnregisteredError,
            FloodWaitError,
            InputUserDeactivatedError,
            SessionPasswordNeededError,
            SessionRevokedError,
            UserDeactivatedBanError,
            UserDeactivatedError,
        )
    except Exception:
        return (
            "unknown",
            "rpc_error",
            safe_detail(exc, tmp_dir=tmp_dir, secrets=secrets),
            None,
        )

    if isinstance(exc, FloodWaitError):
        seconds = int(getattr(exc, "seconds", 0) or 0)
        return (
            "flood_wait",
            "flood_wait",
            f"FloodWait: retry after {seconds}s",
            seconds,
        )
    if isinstance(exc, AuthKeyUnregisteredError):
        return (
            "session_revoked",
            "auth_key_unregistered",
            "session is not registered",
            None,
        )
    if isinstance(exc, SessionRevokedError):
        return ("session_revoked", "session_revoked", "session was revoked", None)
    if isinstance(
        exc, (UserDeactivatedBanError, UserDeactivatedError, InputUserDeactivatedError)
    ):
        return (
            "account_deactivated",
            "account_deactivated",
            "account is deactivated",
            None,
        )
    if isinstance(exc, SessionPasswordNeededError):
        return ("unauthorized", "password_needed", "2FA password required", None)
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return ("proxy_failed", "connect_timeout", "proxy connect/auth timed out", None)
    if isinstance(exc, (OSError, ConnectionError)):
        return (
            "proxy_failed",
            "proxy_connect_error",
            safe_detail(exc, tmp_dir=tmp_dir, secrets=secrets),
            None,
        )
    return (
        "unknown",
        "rpc_error",
        safe_detail(exc, tmp_dir=tmp_dir, secrets=secrets),
        None,
    )


ConvertFn = Callable[[Path, Path], Awaitable[tuple[bool, str]]]
ClientFactory = Callable[[str, dict[str, Any]], Any]
SpamFn = Callable[[Any], Awaitable[Optional[bool]]]


async def default_offline_convert(
    tdata_root: Path, session_path: Path, *, timeout_sec: float = 120.0
) -> tuple[bool, str]:
    """Офлайн-конвертация TData → .session БЕЗ сети (TGConvertor, thread+timeout)."""
    try:
        from TGConvertor import SessionManager
    except Exception as exc:
        return (False, f"tdata converter unavailable: {exc}")
    try:
        manager = await asyncio.wait_for(
            asyncio.to_thread(SessionManager.from_tdata_folder, str(tdata_root)),
            timeout=timeout_sec,
        )
        await asyncio.wait_for(
            manager.to_telethon_file(str(session_path)), timeout=timeout_sec
        )
        return (True, "")
    except Exception as exc:
        return (False, str(exc))


def default_client_factory(session_path: str, proxy: dict[str, Any]):
    """Production factory через workers.manager (proxy обязателен)."""
    from workers.manager import create_telethon_client

    return create_telethon_client(session_path, proxy=proxy)


async def _disconnect_quiet(client: Any) -> None:
    try:
        await client.disconnect()
    except Exception:
        pass


def _valid_telethon_session(data: bytes) -> bool:
    """Inspect SQLite in memory, before creating a file or Telethon client."""
    if not data.startswith(b"SQLite format 3\x00"):
        return False
    conn = sqlite3.connect(":memory:")
    try:
        conn.deserialize(data)
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA trusted_schema=OFF")
        if conn.execute("PRAGMA quick_check(1)").fetchone() != ("ok",):
            return False
        if conn.execute(
            "SELECT type FROM sqlite_master WHERE name='sessions'"
        ).fetchone() != ("table",):
            return False
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
        if not {"dc_id", "server_address", "port", "auth_key", "takeout_id"} <= columns:
            return False
        row = conn.execute(
            "SELECT dc_id, server_address, port, auth_key FROM sessions LIMIT 1"
        ).fetchone()
        return bool(
            row
            and isinstance(row[0], int)
            and row[0] > 0
            and isinstance(row[1], str)
            and row[1]
            and isinstance(row[2], int)
            and 0 < row[2] <= 65535
            and isinstance(row[3], bytes)
            and len(row[3]) == 256
        )
    except (sqlite3.Error, ValueError, OverflowError):
        return False
    finally:
        conn.close()


async def run_check_session(
    data: bytes,
    *,
    proxies: list[CheckProxy],
    client_factory: Optional[ClientFactory] = None,
    run_id: Optional[str] = None,
    connect_timeout_sec: Optional[float] = None,
    tmp_parent: Optional[Path] = None,
) -> TDataCheckRun:
    """Check one uploaded Telethon session through a leased check-pool proxy."""
    rid = run_id or uuid.uuid4().hex[:12]
    created = utcnow_naive().isoformat()

    def failed(status: str, code: str, detail: str) -> TDataCheckRun:
        return TDataCheckRun(
            run_id=rid,
            created_at=created,
            total=1,
            failed_count=1,
            items=[
                TDataCheckItem(
                    item_id="item-01",
                    relpath="uploaded.session",
                    status=status,
                    error_code=code,
                    error_detail=detail,
                )
            ],
        )

    if len(data) > check_limits.MAX_SESSION_BYTES:
        return failed(
            "structure_invalid", "session_too_large", "session exceeds size limit"
        )
    if not data:
        return failed("structure_invalid", "session_empty", "session is empty")
    if not _valid_telethon_session(data):
        return failed(
            "structure_invalid", "invalid_session", "invalid Telethon session"
        )
    if not proxies:
        return failed("proxy_required", "no_check_proxy", "TDATA_CHECK pool is empty")

    leases = CheckProxyLeaseManager()
    timeout = connect_timeout_sec or check_limits.CONNECT_TIMEOUT_SEC
    leased_id = await leases.acquire_wait(
        [int(proxy.id) for proxy in proxies], timeout_sec=max(5.0, timeout)
    )
    if leased_id is None:
        return failed("proxy_failed", "proxy_lease_timeout", "no check proxy available")
    proxy = next(proxy for proxy in proxies if int(proxy.id) == int(leased_id))
    proxy_dict = proxy.to_telethon_dict()
    client: Any = None
    work_dir: Optional[Path] = None
    try:
        # The directory is private and independent of production SESSIONS_DIR.
        work_dir = Path(
            tempfile.mkdtemp(
                prefix=check_limits.TMP_PREFIX,
                dir=str(tmp_parent) if tmp_parent else None,
            )
        )
        session_path = work_dir / "check.session"
        with session_path.open("xb") as session_file:
            session_file.write(data)
        assert proxy_dict, "direct connect forbidden"
        client = (client_factory or default_client_factory)(
            str(session_path), proxy_dict
        )
        await asyncio.wait_for(client.connect(), timeout=timeout)
        authorized = await asyncio.wait_for(
            client.is_user_authorized(), timeout=timeout
        )
        if not authorized:
            item = TDataCheckItem(
                item_id="item-01",
                relpath="uploaded.session",
                status="unauthorized",
                proxy_id=int(proxy.id),
                proxy_label=proxy.label,
                error_code="session_unauthorized",
                error_detail="session is not authorized",
            )
        else:
            me = await asyncio.wait_for(client.get_me(), timeout=timeout)
            if me is None:
                item = TDataCheckItem(
                    item_id="item-01",
                    relpath="uploaded.session",
                    status="unauthorized",
                    proxy_id=int(proxy.id),
                    proxy_label=proxy.label,
                    error_code="empty_profile",
                    error_detail="empty Telegram profile",
                )
            else:
                phone = getattr(me, "phone", None) or None
                item = TDataCheckItem(
                    item_id="item-01",
                    relpath="uploaded.session",
                    status="ok",
                    phone=phone,
                    username=getattr(me, "username", None) or None,
                    first_name=getattr(me, "first_name", None) or None,
                    last_name=getattr(me, "last_name", None) or None,
                    country=guess_country(phone),
                    user_id=getattr(me, "id", None),
                    proxy_id=int(proxy.id),
                    proxy_label=proxy.label,
                )
    except Exception as exc:
        status, code, detail, retry_after = map_exception(
            exc, tmp_dir=work_dir, secrets=_pool_secrets(proxies)
        )
        if code in {"rpc_error", "proxy_connect_error"}:
            detail = (
                "session check failed"
                if code == "rpc_error"
                else "proxy connection failed"
            )
        item = TDataCheckItem(
            item_id="item-01",
            relpath="uploaded.session",
            status=status,
            proxy_id=int(proxy.id),
            proxy_label=proxy.label,
            error_code=code,
            error_detail=detail,
            retry_after=retry_after,
        )
    finally:
        try:
            if client is not None:
                await _disconnect_quiet(client)
        finally:
            leases.release(int(proxy.id))
            if work_dir is not None:
                for attempt in range(3):
                    try:
                        shutil.rmtree(work_dir)
                        break
                    except OSError:
                        if attempt == 2:
                            raise RuntimeError("session temp cleanup failed") from None
                        time.sleep(0.05)
    return TDataCheckRun(
        run_id=rid,
        created_at=created,
        total=1,
        ok_count=int(item.status == "ok"),
        failed_count=int(item.status != "ok"),
        items=[item],
    )


async def check_single_root(
    tdata_root: Path,
    *,
    item_id: str,
    relpath: str,
    proxies: list[CheckProxy],
    leases: CheckProxyLeaseManager,
    client_factory: ClientFactory,
    convert_fn: ConvertFn,
    spambot_fn: Optional[SpamFn] = None,
    check_spam: bool = False,
    work_dir: Path,
    connect_timeout_sec: float,
) -> TDataCheckItem:
    """Проверка одной TData-папки. Никогда не бросает исключение наружу."""
    secrets = _pool_secrets(proxies)
    try:
        return await _check_single_root_inner(
            tdata_root,
            item_id=item_id,
            relpath=relpath,
            proxies=proxies,
            leases=leases,
            client_factory=client_factory,
            convert_fn=convert_fn,
            spambot_fn=spambot_fn,
            check_spam=check_spam,
            work_dir=work_dir,
            connect_timeout_sec=connect_timeout_sec,
        )
    except Exception as exc:
        return TDataCheckItem(
            item_id=item_id,
            relpath=relpath,
            status="unknown",
            error_code="rpc_error",
            error_detail=safe_detail(exc, tmp_dir=work_dir, secrets=secrets),
        )


async def _check_single_root_inner(
    tdata_root: Path,
    *,
    item_id: str,
    relpath: str,
    proxies: list[CheckProxy],
    leases: CheckProxyLeaseManager,
    client_factory: ClientFactory,
    convert_fn: ConvertFn,
    spambot_fn: Optional[SpamFn],
    check_spam: bool,
    work_dir: Path,
    connect_timeout_sec: float,
) -> TDataCheckItem:
    if not is_tdata_root(tdata_root):
        return TDataCheckItem(
            item_id=item_id,
            relpath=relpath,
            status="structure_invalid",
            error_code="not_tdata_structure",
            error_detail="not a TData folder structure",
        )
    secrets = _pool_secrets(proxies)

    # 1. Локальная конвертация БЕЗ сети (lease ещё не нужен).
    session_path = work_dir / f"{item_id}.session"
    try:
        conv_ok, conv_err = await convert_fn(tdata_root, session_path)
    except Exception as exc:
        conv_ok, conv_err = False, str(exc)
    if not conv_ok:
        try:
            session_path.unlink(missing_ok=True)
        except OSError:
            pass
        return TDataCheckItem(
            item_id=item_id,
            relpath=relpath,
            status="conversion_failed",
            error_code="convert_error",
            error_detail=safe_detail(conv_err, tmp_dir=work_dir, secrets=secrets),
        )

    # 2. Proxy обязателен ДО любого network-вызова.
    if not proxies:
        try:
            session_path.unlink(missing_ok=True)
        except OSError:
            pass
        return TDataCheckItem(
            item_id=item_id,
            relpath=relpath,
            status="proxy_required",
            error_code="no_check_proxy",
            error_detail="TDATA_CHECK pool is empty — direct connect is forbidden",
        )

    proxy_ids = [int(p.id) for p in proxies]
    leased_id = await leases.acquire_wait(
        proxy_ids, timeout_sec=max(5.0, connect_timeout_sec)
    )
    if leased_id is None:
        try:
            session_path.unlink(missing_ok=True)
        except OSError:
            pass
        return TDataCheckItem(
            item_id=item_id,
            relpath=relpath,
            status="proxy_failed",
            error_code="proxy_lease_timeout",
            error_detail="no TDATA_CHECK proxy became available in time",
        )
    proxy = next(p for p in proxies if int(p.id) == int(leased_id))
    proxy_dict = proxy.to_telethon_dict()

    client: Any = None
    try:
        # Единственная точка сети в проверке — всегда с proxy из pool.
        assert proxy_dict, "proxy dict must not be empty (direct connect forbidden)"
        client = client_factory(str(session_path), proxy_dict)
        await asyncio.wait_for(client.connect(), timeout=connect_timeout_sec)
        authorized = await asyncio.wait_for(
            client.is_user_authorized(), timeout=connect_timeout_sec
        )
        if not authorized:
            return TDataCheckItem(
                item_id=item_id,
                relpath=relpath,
                status="unauthorized",
                proxy_id=int(proxy.id),
                proxy_label=proxy.label,
                error_code="session_unauthorized",
                error_detail="session is not authorized",
            )
        me = await asyncio.wait_for(client.get_me(), timeout=connect_timeout_sec)
        if me is None:
            return TDataCheckItem(
                item_id=item_id,
                relpath=relpath,
                status="unauthorized",
                proxy_id=int(proxy.id),
                proxy_label=proxy.label,
                error_code="empty_profile",
                error_detail="empty Telegram profile",
            )
        phone = getattr(me, "phone", None) or None
        username = getattr(me, "username", None) or None
        item = TDataCheckItem(
            item_id=item_id,
            relpath=relpath,
            status="ok",
            phone=phone,
            username=username,
            first_name=getattr(me, "first_name", None) or None,
            last_name=getattr(me, "last_name", None) or None,
            country=guess_country(phone),
            user_id=getattr(me, "id", None),
            proxy_id=int(proxy.id),
            proxy_label=proxy.label,
        )
        if check_spam and spambot_fn is not None:
            try:
                restricted = await asyncio.wait_for(
                    spambot_fn(client),
                    timeout=check_limits.SPAMCHECK_TIMEOUT_SEC,
                )
            except Exception as exc:
                item.status = "unknown"
                item.error_code = "spambot_probe_error"
                item.error_detail = safe_detail(exc, tmp_dir=work_dir, secrets=secrets)
                item.spam_restricted = None
                return item
            if restricted is True:
                item.status = "spam_restriction"
                item.error_code = "spam_restricted"
                item.error_detail = "account is limited (SpamBot)"
                item.spam_restricted = True
            elif restricted is False:
                item.spam_restricted = False
        return item
    except Exception as exc:
        status, code, detail, retry_after = map_exception(
            exc, tmp_dir=work_dir, secrets=secrets
        )
        return TDataCheckItem(
            item_id=item_id,
            relpath=relpath,
            status=status,
            proxy_id=int(proxy.id),
            proxy_label=proxy.label,
            error_code=code,
            error_detail=detail,
            retry_after=retry_after,
        )
    finally:
        if client is not None:
            await _disconnect_quiet(client)
        leases.release(int(proxy.id))
        try:
            session_path.unlink(missing_ok=True)
        except OSError:
            pass


async def run_check_archive(
    data: bytes,
    *,
    proxies: list[CheckProxy],
    client_factory: Optional[ClientFactory] = None,
    convert_fn: Optional[ConvertFn] = None,
    spambot_fn: Optional[SpamFn] = None,
    check_spam: bool = False,
    run_id: Optional[str] = None,
    max_archive_bytes: Optional[int] = None,
    max_folders: Optional[int] = None,
    max_concurrency: Optional[int] = None,
    connect_timeout_sec: Optional[float] = None,
    tmp_parent: Optional[Path] = None,
) -> TDataCheckRun:
    """Проверка multi-TData ZIP: safe extract → bounded run → guaranteed cleanup.

    Partial success: одна плохая папка не отменяет остальные. Временные
    артефакты — только в системном tmp (НЕ data/sessions), каталог удаляется
    в ``finally``. Повторный запуск детерминирован по (data, proxies).
    """
    rid = run_id or uuid.uuid4().hex[:12]
    created = utcnow_naive().isoformat()
    cap_bytes = max_archive_bytes or check_limits.MAX_ARCHIVE_BYTES
    cap_folders = max_folders or check_limits.MAX_FOLDERS
    cap_conc = max_concurrency or check_limits.MAX_CONCURRENCY
    timeout = connect_timeout_sec or check_limits.CONNECT_TIMEOUT_SEC
    factory = client_factory or default_client_factory
    converter = convert_fn or default_offline_convert
    secrets = _pool_secrets(list(proxies or []))

    work_dir = Path(
        tempfile.mkdtemp(
            prefix=check_limits.TMP_PREFIX, dir=str(tmp_parent) if tmp_parent else None
        )
    )
    try:
        try:
            safe_extract_zip(data, work_dir / "zip", max_bytes=cap_bytes)
        except ZipSafetyError as exc:
            return TDataCheckRun(
                run_id=rid,
                created_at=created,
                error_code=f"archive_invalid:{exc.code}",
                error_detail=safe_detail(exc, tmp_dir=work_dir, secrets=secrets),
            )
        extract_dir = work_dir / "zip"
        roots = find_check_roots(extract_dir)
        if not roots:
            return TDataCheckRun(
                run_id=rid,
                created_at=created,
                error_code="archive_invalid:no_tdata_roots",
                error_detail="no valid tdata folders found",
            )
        truncated = len(roots) > cap_folders
        roots = roots[:cap_folders]

        leases = CheckProxyLeaseManager()
        sem = asyncio.Semaphore(max(1, min(cap_conc, len(roots))))

        async def _one(index: int, root: Path) -> TDataCheckItem:
            item_id = f"item-{index:02d}"
            relpath = root.relative_to(extract_dir).as_posix()
            async with sem:
                return await check_single_root(
                    root,
                    item_id=item_id,
                    relpath=relpath,
                    proxies=list(proxies),
                    leases=leases,
                    client_factory=factory,
                    convert_fn=converter,
                    spambot_fn=spambot_fn,
                    check_spam=check_spam,
                    work_dir=work_dir,
                    connect_timeout_sec=timeout,
                )

        items = await asyncio.gather(*[_one(i, r) for i, r in enumerate(roots, 1)])
        ordered = sorted(items, key=lambda it: it.item_id)
        ok_count = sum(1 for it in ordered if it.status == "ok")
        return TDataCheckRun(
            run_id=rid,
            created_at=created,
            total=len(ordered),
            ok_count=ok_count,
            failed_count=len(ordered) - ok_count,
            truncated=truncated,
            items=ordered,
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


__all__ = [
    "CheckProxy",
    "_pool_secrets",
    "check_proxy_from_orm",
    "guess_country",
    "safe_detail",
    "map_exception",
    "default_offline_convert",
    "default_client_factory",
    "check_single_root",
    "run_check_archive",
    "run_check_session",
]
