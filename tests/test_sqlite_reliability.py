"""Надёжность SQLite (задача 05): единые PRAGMA, UPSERT-идемпотентность, retry.

Все БД — только в tmp_path, data/*.db не трогаем. Стиль — как в
tests/test_parser_concurrency.py (asyncio.run внутри sync-тестов).
"""

from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from database.models import Base, Client, ClientStatus
from database.sqlite_pragmas import (
    SQLITE_BUSY_MAX_ATTEMPTS,
    SQLITE_BUSY_RETRY_STATS,
    SQLITE_BUSY_TIMEOUT_MS,
    SQLITE_WAL_AUTOCHECKPOINT_PAGES,
    is_transient_sqlite_busy,
    register_async_sqlite_pragmas,
    register_sqlite_pragmas,
    reset_retry_stats,
    run_sync_with_busy_retry,
    run_with_busy_retry,
)


def _read_pragmas_sync(engine):
    with engine.connect() as conn:
        out = {}
        for name in (
            "journal_mode",
            "foreign_keys",
            "busy_timeout",
            "wal_autocheckpoint",
        ):
            out[name] = conn.exec_driver_sql(f"PRAGMA {name}").scalar()
        return out


def test_all_three_engines_get_identical_pragmas(tmp_path):
    """Три engine (async bot / sync bot CP / CP DB) — одинаковые PRAGMA."""
    bot_path = (tmp_path / "corebot.db").as_posix()
    cp_path = (tmp_path / "control_plane.db").as_posix()

    aengine = create_async_engine(
        f"sqlite+aiosqlite:///{bot_path}",
        connect_args={"timeout": 30, "check_same_thread": False},
        pool_pre_ping=True,
    )
    register_async_sqlite_pragmas(aengine)

    bot_sync = create_engine(
        f"sqlite:///{bot_path}",
        future=True,
        connect_args={"timeout": 30, "check_same_thread": False},
        pool_pre_ping=True,
    )
    register_sqlite_pragmas(bot_sync)

    cp_sync = create_engine(
        f"sqlite:///{cp_path}",
        future=True,
        connect_args={"timeout": 30, "check_same_thread": False},
        pool_pre_ping=True,
    )
    register_sqlite_pragmas(cp_sync)

    async def read_async():
        async with aengine.connect() as conn:
            out = {}
            for name in (
                "journal_mode",
                "foreign_keys",
                "busy_timeout",
                "wal_autocheckpoint",
            ):
                res = await conn.exec_driver_sql(f"PRAGMA {name}")
                out[name] = res.scalar()
            return out

    pragmas_async = asyncio.run(read_async())
    pragmas_bot = _read_pragmas_sync(bot_sync)
    pragmas_cp = _read_pragmas_sync(cp_sync)

    expected = {
        "journal_mode": "wal",
        "foreign_keys": 1,
        "busy_timeout": SQLITE_BUSY_TIMEOUT_MS,
        "wal_autocheckpoint": SQLITE_WAL_AUTOCHECKPOINT_PAGES,
    }
    for pragmas in (pragmas_async, pragmas_bot, pragmas_cp):
        assert str(pragmas["journal_mode"]).lower() == expected["journal_mode"]
        assert int(pragmas["foreign_keys"]) == expected["foreign_keys"]
        assert int(pragmas["busy_timeout"]) == expected["busy_timeout"]
        assert int(pragmas["wal_autocheckpoint"]) == expected["wal_autocheckpoint"]

    asyncio.run(aengine.dispose())
    bot_sync.dispose()
    cp_sync.dispose()


def test_production_modules_wire_shared_helper():
    """Продакшен-модули используют общий helper, URL/пути не менялись."""
    import inspect

    import control_plane.business.db as cp_bot_db
    import control_plane.database as cp_db
    import database.repository as repo

    assert "register_sqlite_pragmas" in dir(cp_bot_db)
    assert "register_sqlite_pragmas" in dir(cp_db)
    assert "register_async_sqlite_pragmas" in inspect.getsource(repo)

    # Пути/URL не менялись (validate_config требует абсолютных путей в prod).
    from control_plane.config import BOT_DATABASE_URL, CP_DATABASE_URL

    assert (
        BOT_DATABASE_URL == "sqlite:///data/corebot.db"
        or BOT_DATABASE_URL.startswith("sqlite")
    )
    assert CP_DATABASE_URL.startswith("sqlite")
    assert SQLITE_BUSY_TIMEOUT_MS == 30000
    assert SQLITE_WAL_AUTOCHECKPOINT_PAGES == 1000


def _file_engine(db_path: str):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        connect_args={"timeout": 30, "check_same_thread": False},
    )
    register_async_sqlite_pragmas(engine)
    return engine


def test_concurrent_duplicate_client_is_idempotent(tmp_path):
    """Два одновременных create под unique(username) — 1 строка, без падения."""
    from database.repositories import ClientRepository

    db_path = (tmp_path / "dup.db").as_posix()
    engine = _file_engine(db_path)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async def one():
            async with Session() as s:
                row = await ClientRepository.create(
                    s, "dupuser", status=ClientStatus.NEW
                )
                return int(row.id)

        first, second = await asyncio.gather(one(), one())
        assert first == second

        async with Session() as verify:
            n = await verify.scalar(
                select(func.count(Client.id)).where(Client.username == "dupuser")
            )
            assert n == 1

    asyncio.run(scenario())
    asyncio.run(engine.dispose())


def test_concurrent_class_counter_increment_sums_without_conflict(tmp_path):
    """Repro гонки до фикса: два concurrent increment — одна строка, count=2.

    До фикса (SELECT→INSERT) второй воркер падал с
    IntegrityError UNIQUE(client_id, class_key); после UPSERT — суммирование.
    """
    from database.crm_repositories import ClientClassCounterRepository

    db_path = (tmp_path / "counter.db").as_posix()
    engine = _file_engine(db_path)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with Session() as setup:
            setup.add(Client(username="cnt1", status=ClientStatus.NEW))
            await setup.commit()

        async def one():
            async with Session() as s:
                return await ClientClassCounterRepository.increment(s, 1, "pulse", 1)

        results = await asyncio.gather(one(), one())
        assert sorted(results) == [1, 2]

        async with Session() as verify:
            total = await ClientClassCounterRepository.get_counts(verify, 1)
            assert total == {"pulse": 2}

    asyncio.run(scenario())
    asyncio.run(engine.dispose())


def test_concurrent_alive_window_single_winner(tmp_path):
    """Два concurrent create_if_absent — ровно один True, без IntegrityError."""
    from database.crm_repositories import ClientAliveWindowRepository
    from database.models import Account, Mailing

    db_path = (tmp_path / "alive.db").as_posix()
    engine = _file_engine(db_path)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with Session() as setup:
            setup.add(Account(phone="+10000000001", session_name="alive_sess"))
            setup.add(Client(username="alive1", status=ClientStatus.NEW))
            setup.add(Mailing(message_text="hi"))
            await setup.commit()

        async def one():
            async with Session() as s:
                return await ClientAliveWindowRepository.create_if_absent(
                    s, mailing_id=1, account_id=1, client_id=1, window_key=99
                )

        results = await asyncio.gather(one(), one())
        assert sorted(results) == [False, True]

    asyncio.run(scenario())
    asyncio.run(engine.dispose())


def test_retry_recovers_transient_busy_then_succeeds():
    """Мок busy→busy→успех: retry срабатывает, результат возвращается."""
    reset_retry_stats()
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise OperationalError("execute", {}, Exception("database is locked"))
        return "ok"

    async def scenario():
        return await run_with_busy_retry(flaky, base_delay_sec=0.001, op_name="test")

    assert asyncio.run(scenario()) == "ok"
    assert calls["n"] == 3
    assert SQLITE_BUSY_RETRY_STATS["retries"] == 2


def test_retry_exhausts_persistent_busy_in_bounded_time():
    """Мок persistent busy: ≤5 попыток, конечное время, исходный проброс."""
    reset_retry_stats()
    calls = {"n": 0}

    async def always_busy():
        calls["n"] += 1
        raise OperationalError("execute", {}, Exception("database is locked"))

    async def scenario():
        await run_with_busy_retry(always_busy, base_delay_sec=0.001, op_name="test")

    started = time.monotonic()
    with pytest.raises(OperationalError):
        asyncio.run(scenario())
    elapsed = time.monotonic() - started
    assert calls["n"] == SQLITE_BUSY_MAX_ATTEMPTS == 5
    assert SQLITE_BUSY_RETRY_STATS["exhausted"] == 1
    assert elapsed < 10


def test_sync_retry_recovers_and_exhausts():
    """Sync-вариант: busy→успех и persistent busy с лимитом."""
    reset_retry_stats()
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise OperationalError("commit", {}, Exception("database table is locked"))
        return 42

    assert run_sync_with_busy_retry(flaky, base_delay_sec=0.001, op_name="test") == 42
    assert SQLITE_BUSY_RETRY_STATS["retries"] == 1

    reset_retry_stats()
    bad_calls = {"n": 0}

    def always_busy():
        bad_calls["n"] += 1
        raise OperationalError("commit", {}, Exception("SQLITE_BUSY"))

    started = time.monotonic()
    with pytest.raises(OperationalError):
        run_sync_with_busy_retry(always_busy, base_delay_sec=0.001, op_name="test")
    assert bad_calls["n"] == 5
    assert time.monotonic() - started < 10


def test_integrity_error_is_never_retried():
    """IntegrityError (и логические ошибки) — немедленный проброс, 1 попытка."""
    reset_retry_stats()
    calls = {"n": 0}

    async def violates_unique():
        calls["n"] += 1
        raise IntegrityError("insert", {}, Exception("UNIQUE constraint failed: x"))

    async def scenario():
        await run_with_busy_retry(violates_unique, base_delay_sec=0.001, op_name="test")

    with pytest.raises(IntegrityError):
        asyncio.run(scenario())
    assert calls["n"] == 1
    assert SQLITE_BUSY_RETRY_STATS["retries"] == 0
    assert SQLITE_BUSY_RETRY_STATS["failed_fast"] == 1

    assert not is_transient_sqlite_busy(
        IntegrityError("i", {}, Exception("UNIQUE constraint failed"))
    )
    assert not is_transient_sqlite_busy(ValueError("логическая ошибка"))
    assert is_transient_sqlite_busy(
        OperationalError("e", {}, Exception("database is locked"))
    )


def test_no_blanket_integrityerror_around_commits():
    """Нет blanket try/except IntegrityError: только точечные хендлеры границ."""
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parent.parent
    allowed_files = {
        "database/repositories.py",  # Account/Client.create: idempotent-возврат
        "control_plane/business/proxies.py",  # 409 на границе API
        "control_plane/business/groups.py",  # 409 на границе API
        "control_plane/business/tdata_routes.py",  # idempotent-возврат дубля
        "bot/handlers/accounts/groups.py",  # сообщение "уже существует"
    }
    offenders: list[str] = []
    for path in (
        list((repo_root / "database").rglob("*.py"))
        + list((repo_root / "control_plane").rglob("*.py"))
        + list((repo_root / "workers").rglob("*.py"))
        + list((repo_root / "bot").rglob("*.py"))
    ):
        if path.name == "sqlite_pragmas.py":
            continue  # классификатор is_transient_sqlite_busy, не хендлер коммитов
        text = path.read_text(encoding="utf-8")
        if "except IntegrityError" in text:
            rel = path.relative_to(repo_root).as_posix()
            if rel not in allowed_files:
                offenders.append(rel)
    assert offenders == [], f"unexpected IntegrityError handlers: {offenders}"


def test_memory_engine_helper_smoke():
    """Helper регистрируется и на :memory: (StaticPool) без ошибок."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    register_sqlite_pragmas(engine)
    pragmas = _read_pragmas_sync(engine)
    assert int(pragmas["foreign_keys"]) == 1
    assert int(pragmas["busy_timeout"]) == SQLITE_BUSY_TIMEOUT_MS
    engine.dispose()
