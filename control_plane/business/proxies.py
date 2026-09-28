"""
Бизнес-API прокси (`proxies` + `proxy_groups`).

Тест соединения — настоящий handshake через прокси, а не голый TCP-connect:
SOCKS5 — greeting + username/password-аутентификация (RFC 1929) + CONNECT,
HTTP — CONNECT с Proxy-Authorization. Мёртвые логины/пароли дают FAIL,
а не «tcp connect ok». Результат сохраняется (`last_checked`, `is_working`).

Эндпоинты:
    GET    /business/proxies                  — список с группой и счётчиком аккаунтов
    POST   /business/proxies                  — создать
    PATCH  /business/proxies/{id}             — изменить
    DELETE /business/proxies/{id}             — удалить (Account.proxy_id → NULL)
    POST   /business/proxies/{id}/test        — handshake-проверка
    POST   /business/proxies/import           — bulk-импорт из строк в группу
    GET    /business/proxy-groups             — список групп прокси
"""
from __future__ import annotations

import base64
import socket
import time
from utils.time import utcnow_naive

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from control_plane.business.db import commit_sync, get_bot_db
from control_plane.business.schemas import (
    ProxyCreate,
    ProxyGroupCreate,
    ProxyGroupItem,
    ProxyImportRequest,
    ProxyImportResult,
    ProxyItem,
    ProxyPatch,
    ProxyTestResult,
)
from control_plane.deps import get_current_user, require_operator_write
from control_plane.models import User
from database.models import Account, Proxy, ProxyGroup, ProxyType
from utils.proxy_line import dedup_key, parse_proxy_line


router = APIRouter(tags=["business-proxies"])

#: Куда стучимся СКВОЗЬ прокси при проверке: открываем CONNECT и сразу
#: закрываем, HTTP не отправляем. 1.1.1.1:80 выбран как стабильный адрес,
#: отвечающий на TCP практически всегда.
PROXY_TEST_TARGET_HOST = "1.1.1.1"
PROXY_TEST_TARGET_PORT = 80
PROXY_TEST_TIMEOUT_S = 8.0


# ----------------------------- helpers -----------------------------------


def _proxy_type_to_str(value: object) -> str:
    if isinstance(value, ProxyType):
        return value.value
    if value is None:
        return "socks5"
    s = str(value).lower()
    return s if s in ("socks5", "http") else "socks5"


def _proxy_type_from_str(value: str | None) -> ProxyType:
    s = (value or "").strip().lower()
    if s != "socks5":
        raise HTTPException(status_code=400, detail="only SOCKS5 proxies are supported")
    return ProxyType.SOCKS5


def _serialize_proxy(
    p: Proxy,
    *,
    accounts_count: int = 0,
    group_name: str | None = None,
) -> ProxyItem:
    return ProxyItem(
        id=int(p.id),
        name=p.name,
        host=p.host,
        port=int(p.port),
        username=p.username,
        proxy_type=_proxy_type_to_str(p.proxy_type),
        is_active=bool(p.is_active),
        is_working=bool(p.is_working),
        group_id=int(p.group_id) if p.group_id else None,
        group_name=group_name,
        accounts_count=int(accounts_count),
        last_checked=p.last_checked,
    )


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError("connection closed by proxy")
        buf += chunk
    return buf


def _socks5_check(
    sock: socket.socket, username: str | None, password: str | None
) -> tuple[bool, str]:
    """SOCKS5 greeting + auth + CONNECT. Возвращает (ok, detail)."""
    want_auth = bool((username or "") or (password or ""))
    # Если логин есть — предлагаем ТОЛЬКО user/pass, чтобы сервер не
    # проскочил через no-auth с мёртвыми кредами.
    sock.sendall(bytes((0x05, 0x01, 0x02 if want_auth else 0x00)))
    ver, method = _recv_exact(sock, 2)
    if ver != 0x05:
        return False, f"socks5 bad greeting reply (ver=0x{ver:02x})"
    if method == 0xFF:
        return False, "socks5 no acceptable auth method"
    authed = False
    if method == 0x02:
        if not want_auth:
            return False, "socks5 requires auth but no credentials stored"
        u = (username or "").encode("utf-8")[:255]
        pw = (password or "").encode("utf-8")[:255]
        sock.sendall(bytes((0x01, len(u))) + u + bytes((len(pw),)) + pw)
        aver, astatus = _recv_exact(sock, 2)
        if aver != 0x01 or astatus != 0x00:
            return False, "socks5 auth failed (bad login/password)"
        authed = True
    elif method != 0x00:
        return False, f"socks5 unknown auth method (0x{method:02x})"
    req = (
        bytes((0x05, 0x01, 0x00, 0x01))
        + socket.inet_aton(PROXY_TEST_TARGET_HOST)
        + PROXY_TEST_TARGET_PORT.to_bytes(2, "big")
    )
    sock.sendall(req)
    ver, rep, _, atyp = _recv_exact(sock, 4)
    if ver != 0x05:
        return False, "socks5 bad connect reply"
    if atyp == 0x01:
        _recv_exact(sock, 6)
    elif atyp == 0x03:
        (ln,) = _recv_exact(sock, 1)
        _recv_exact(sock, ln + 2)
    elif atyp == 0x04:
        _recv_exact(sock, 18)
    else:
        return False, f"socks5 bad address type (0x{atyp:02x})"
    if rep != 0x00:
        names = {0x01: "general failure", 0x02: "not allowed",
                 0x04: "host unreachable", 0x05: "connection refused"}
        return False, f"socks5 connect refused ({names.get(rep, hex(rep))})"
    note = "auth ok" if authed else ("no-auth" if want_auth else "no credentials needed")
    return True, f"socks5 ok ({note}, connect {PROXY_TEST_TARGET_HOST}:{PROXY_TEST_TARGET_PORT})"


def _http_proxy_check(
    sock: socket.socket, username: str | None, password: str | None
) -> tuple[bool, str]:
    """HTTP CONNECT + Proxy-Authorization. Возвращает (ok, detail)."""
    lines = [
        f"CONNECT {PROXY_TEST_TARGET_HOST}:{PROXY_TEST_TARGET_PORT} HTTP/1.1",
        f"Host: {PROXY_TEST_TARGET_HOST}:{PROXY_TEST_TARGET_PORT}",
    ]
    if (username or "") or (password or ""):
        token = base64.b64encode(
            f"{username or ''}:{password or ''}".encode("utf-8")
        ).decode("ascii")
        lines.append(f"Proxy-Authorization: Basic {token}")
    lines += ["Connection: close", "", ""]
    sock.sendall("\r\n".join(lines).encode("ascii"))
    raw = b""
    while b"\r\n" not in raw:
        if len(raw) > 4096:
            return False, "http proxy bad reply (status line too long)"
        chunk = sock.recv(1024)
        if not chunk:
            return False, "http proxy closed connection without reply"
        raw += chunk
    status_line = raw.split(b"\r\n", 1)[0].decode("latin-1")
    parts = status_line.split(" ", 2)
    if len(parts) < 2 or not parts[0].upper().startswith("HTTP/") or not parts[1].isdigit():
        return False, f"http proxy bad reply ({status_line[:60]!r})"
    code = int(parts[1])
    if code == 200:
        return True, f"http connect ok ({PROXY_TEST_TARGET_HOST}:{PROXY_TEST_TARGET_PORT})"
    if code in (401, 407):
        return False, f"http proxy auth failed (HTTP {code})"
    return False, f"http proxy refused (HTTP {code})"


def check_proxy_endpoint(
    host: str,
    port: int,
    proxy_type: str = "socks5",
    username: str | None = None,
    password: str | None = None,
    timeout: float = PROXY_TEST_TIMEOUT_S,
) -> tuple[bool, str]:
    """Полная проверка прокси: TCP + handshake + auth + CONNECT.

    Возвращает (ok, detail). Никогда не кидает исключений наружу —
    любая ошибка соединения превращается в (False, причина).
    """
    try:
        port = int(port)
    except (TypeError, ValueError):
        return False, "bad port"
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as e:
        return False, f"tcp failed: {type(e).__name__}: {e}"
    try:
        with sock:
            if (proxy_type or "socks5").lower() == "http":
                return _http_proxy_check(sock, username, password)
            return _socks5_check(sock, username, password)
    except OSError as e:
        return False, f"{type(e).__name__}: {e}"
    except Exception as e:  # pragma: no cover — safety net
        return False, f"check error: {type(e).__name__}: {e}"


# ----------------------------- proxies -----------------------------------


@router.get("/business/proxies", response_model=list[ProxyItem])
def list_proxies(
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    counts = dict(
        db.execute(
            select(Account.proxy_id, func.count(Account.id))
            .where(Account.proxy_id.is_not(None))
            .group_by(Account.proxy_id)
        ).all()
    )
    groups = {
        int(g.id): g.name
        for g in db.execute(select(ProxyGroup)).scalars().all()
    }
    rows = db.execute(select(Proxy).order_by(Proxy.id)).scalars().all()
    return [
        _serialize_proxy(
            p,
            accounts_count=counts.get(p.id, 0),
            group_name=groups.get(int(p.group_id)) if p.group_id else None,
        )
        for p in rows
    ]


@router.post(
    "/business/proxies",
    response_model=ProxyItem,
    status_code=status.HTTP_201_CREATED,
)
def create_proxy(
    payload: ProxyCreate,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(require_operator_write),
):
    if payload.group_id is not None and payload.group_id > 0:
        if not db.get(ProxyGroup, int(payload.group_id)):
            raise HTTPException(status_code=400, detail="proxy group not found")
    p = Proxy(
        name=payload.name.strip(),
        host=payload.host.strip(),
        port=int(payload.port),
        username=(payload.username or None),
        password=(payload.password or None),
        proxy_type=_proxy_type_from_str(payload.proxy_type),
        is_active=bool(payload.is_active),
        is_working=True,
        group_id=int(payload.group_id) if payload.group_id else None,
    )
    db.add(p)
    try:
        commit_sync(db)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="proxy with this name exists")
    db.refresh(p)
    group_name = None
    if p.group_id:
        gg = db.get(ProxyGroup, int(p.group_id))
        group_name = gg.name if gg else None
    return _serialize_proxy(p, accounts_count=0, group_name=group_name)


@router.patch("/business/proxies/{proxy_id}", response_model=ProxyItem)
def patch_proxy(
    proxy_id: int,
    payload: ProxyPatch,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(require_operator_write),
):
    p = db.get(Proxy, proxy_id)
    if not p:
        raise HTTPException(status_code=404, detail="proxy not found")
    if payload.name is not None:
        p.name = payload.name.strip()
    if payload.host is not None:
        p.host = payload.host.strip()
    if payload.port is not None:
        p.port = int(payload.port)
    if payload.username is not None:
        p.username = payload.username or None
    if payload.password is not None:
        p.password = payload.password or None
    if payload.proxy_type is not None:
        p.proxy_type = _proxy_type_from_str(payload.proxy_type)
    if payload.group_id is not None:
        if int(payload.group_id) <= 0:
            p.group_id = None
        else:
            if not db.get(ProxyGroup, int(payload.group_id)):
                raise HTTPException(status_code=400, detail="proxy group not found")
            p.group_id = int(payload.group_id)
    if payload.is_active is not None:
        p.is_active = bool(payload.is_active)
    try:
        commit_sync(db)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="proxy with this name exists")
    db.refresh(p)
    group_name = None
    if p.group_id:
        gg = db.get(ProxyGroup, int(p.group_id))
        group_name = gg.name if gg else None
    cnt = int(
        db.execute(
            select(func.count(Account.id)).where(Account.proxy_id == p.id)
        ).scalar_one()
        or 0
    )
    return _serialize_proxy(p, accounts_count=cnt, group_name=group_name)


@router.delete(
    "/business/proxies/{proxy_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_proxy(
    proxy_id: int,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(require_operator_write),
):
    p = db.get(Proxy, proxy_id)
    if not p:
        raise HTTPException(status_code=404, detail="proxy not found")
    db.execute(
        update(Account).where(Account.proxy_id == proxy_id).values(proxy_id=None)
    )
    db.delete(p)
    commit_sync(db)


@router.post("/business/proxies/{proxy_id}/test", response_model=ProxyTestResult)
def test_proxy(
    proxy_id: int,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(require_operator_write),
):
    p = db.get(Proxy, proxy_id)
    if not p:
        raise HTTPException(status_code=404, detail="proxy not found")
    started = time.perf_counter()
    ok, detail = check_proxy_endpoint(
        p.host,
        int(p.port),
        _proxy_type_to_str(p.proxy_type),
        p.username,
        p.password,
    )
    elapsed = int((time.perf_counter() - started) * 1000)
    p.last_checked = utcnow_naive()
    p.is_working = bool(ok)
    commit_sync(db)
    return ProxyTestResult(
        proxy_id=int(proxy_id),
        ok=ok,
        elapsed_ms=elapsed,
        detail=detail,
    )


@router.post("/business/proxies/import", response_model=ProxyImportResult)
def import_proxies(
    payload: ProxyImportRequest,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(require_operator_write),
):
    """Bulk-импорт прокси из строк (файл .txt читается в браузере).

    Форматы строк — как в боте: ``host:port@user:pass``,
    ``user:pass@host:port``, ``host:port:user:pass``, ``host:port``
    (+ префиксы ``socks5://`` / ``http://``). Дедуп по
    (host, port, username, password) на всю таблицу; существующая группа
    не меняет purpose.
    """
    group_name = payload.group_name.strip()
    group = db.execute(select(ProxyGroup).where(ProxyGroup.name == group_name)).scalars().first()
    if group is None:
        group = ProxyGroup(name=group_name, purpose=payload.purpose.strip().upper())
        db.add(group)
        try:
            commit_sync(db)
        except IntegrityError:
            db.rollback()
            group = db.execute(select(ProxyGroup).where(ProxyGroup.name == group_name)).scalars().first()
            if group is None:
                raise HTTPException(status_code=409, detail="proxy group race, retry")
        else:
            db.refresh(group)

    existing_keys = {
        (p.host, int(p.port), p.username or "", p.password or "")
        for p in db.execute(select(Proxy)).scalars().all()
    }
    added = skipped = bad = 0
    bad_samples: list[str] = []
    seq = 0
    for raw in payload.lines:
        line = (raw or "").strip()
        if not line:
            continue
        parsed = parse_proxy_line(line)
        if parsed is None or parsed.proxy_type != "socks5":
            bad += 1
            if len(bad_samples) < 5:
                bad_samples.append(line[:80])
            continue
        key = dedup_key(parsed.host, parsed.port, parsed.username, parsed.password)
        if key in existing_keys:
            skipped += 1
            continue
        seq += 1
        base = f"{group.name.lower()}_{seq}_{parsed.host}:{parsed.port}"[:100]
        name = base
        suffix = 1
        while db.execute(select(Proxy.id).where(Proxy.name == name)).scalars().first():
            suffix += 1
            name = f"{base[:90]}~{suffix}"
        db.add(
            Proxy(
                name=name,
                host=parsed.host,
                port=int(parsed.port),
                username=parsed.username,
                password=parsed.password,
                proxy_type=ProxyType.SOCKS5,
                is_active=True,
                is_working=True,
                group_id=int(group.id),
            )
        )
        try:
            commit_sync(db)
        except IntegrityError:
            db.rollback()
            skipped += 1
            continue
        existing_keys.add(key)
        added += 1
    return ProxyImportResult(
        group_id=int(group.id),
        group_name=group.name,
        total_lines=len(payload.lines),
        added=added,
        skipped_duplicates=skipped,
        bad=bad,
        bad_samples=bad_samples,
    )


# ----------------------------- proxy-groups ------------------------------


@router.get("/business/proxy-groups", response_model=list[ProxyGroupItem])
def list_proxy_groups(
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    counts = dict(
        db.execute(
            select(Proxy.group_id, func.count(Proxy.id))
            .where(Proxy.group_id.is_not(None))
            .group_by(Proxy.group_id)
        ).all()
    )
    rows = db.execute(select(ProxyGroup).order_by(ProxyGroup.id)).scalars().all()
    return [
        ProxyGroupItem(
            id=int(g.id),
            name=g.name or f"#{g.id}",
            proxies_count=int(counts.get(g.id, 0)),
            purpose=(getattr(g, "purpose", None) or "ACCOUNT_RUNTIME"),
        )
        for g in rows
    ]


@router.post(
    "/business/proxy-groups",
    response_model=ProxyGroupItem,
    status_code=status.HTTP_201_CREATED,
)
def create_proxy_group(
    payload: ProxyGroupCreate,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(require_operator_write),
):
    """Создание proxy pool с ЯВНЫМ назначением (задача 12).

    ``purpose=TDATA_CHECK`` — только для проверки TData; такие группы
    никогда не используются runtime-автоназначением аккаунтов.
    """
    purpose = (payload.purpose or "ACCOUNT_RUNTIME").strip().upper()
    if purpose not in ("ACCOUNT_RUNTIME", "TDATA_CHECK"):
        raise HTTPException(status_code=400, detail="unknown pool purpose")
    existing = (
        db.execute(select(ProxyGroup).where(ProxyGroup.name == payload.name.strip()))
        .scalars()
        .first()
    )
    if existing:
        raise HTTPException(status_code=409, detail="proxy group with this name exists")
    group = ProxyGroup(name=payload.name.strip(), purpose=purpose)
    db.add(group)
    try:
        commit_sync(db)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="proxy group with this name exists")
    db.refresh(group)
    return ProxyGroupItem(
        id=int(group.id),
        name=group.name,
        proxies_count=0,
        purpose=(group.purpose or "ACCOUNT_RUNTIME"),
    )
