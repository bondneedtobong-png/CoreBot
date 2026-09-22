"""Задача 02: единая UTC-модель времени.

Проверяет utils.time helpers, отсутствие datetime.utcnow в собственном
коде, совместимость с legacy naive значениями и сериализацию без
двойного +00:00.
"""
from __future__ import annotations

import pathlib
import warnings
from datetime import datetime, timezone


def test_helpers_naive_format():
    from utils.time import utcnow_aware, utcnow_naive

    naive = utcnow_naive()
    assert isinstance(naive, datetime)
    assert naive.tzinfo is None, "utcnow_naive must be naive UTC for ORM"

    aware = utcnow_aware()
    assert isinstance(aware, datetime)
    assert aware.tzinfo is not None, "utcnow_aware must be tz-aware UTC"


def test_helpers_monotonic_non_decreasing():
    from utils.time import utcnow_aware, utcnow_naive

    prev_naive = utcnow_naive()
    prev_aware = utcnow_aware()
    for _ in range(50):
        cur_naive = utcnow_naive()
        cur_aware = utcnow_aware()
        assert cur_naive >= prev_naive, "utcnow_naive must not go backwards"
        assert cur_aware >= prev_aware, "utcnow_aware must not go backwards"
        prev_naive, prev_aware = cur_naive, cur_aware


def test_helpers_compare_with_legacy_naive_without_type_error():
    from utils.time import utcnow_naive

    legacy = datetime(2026, 1, 1)  # naive UTC как в существующей SQLite
    now = utcnow_naive()
    assert (now - legacy).total_seconds() > 0
    assert legacy < now
    assert now >= legacy


def test_helpers_emit_no_deprecation_warning():
    from utils.time import utcnow_aware, utcnow_naive

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        utcnow_naive()
        utcnow_aware()


def test_legacy_utcnow_emits_deprecation_warning():
    """Фиксирует исходную проблему: datetime.utcnow() deprecated в 3.14."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        datetime.utcnow()
    assert any(
        "utcnow" in str(w.message).lower() or "deprecat" in str(w.message).lower()
        for w in caught
    ), "expected DeprecationWarning from datetime.utcnow()"


def test_no_utcnow_in_own_code():
    roots = ["bot", "control_plane", "database", "services", "workers", "utils", "scripts"]
    base = pathlib.Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    files = [base / "main.py"]
    for r in roots:
        d = base / r
        if r == "scripts":
            # собственный код: только scripts/cleanup_dialogs.py в скоупе задачи
            f = d / "cleanup_dialogs.py"
            if f.exists():
                files.append(f)
            continue
        files.extend(sorted(d.rglob("*.py")))
    for f in files:
        if f.name == "time.py" and f.parent.name == "utils":
            continue  # helper-модуль намеренно упоминает legacy API в docstring
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if "datetime.utcnow" in line:
                offenders.append(f"{f.relative_to(base)}:{i}:{line.strip()[:160]}")
    assert not offenders, "datetime.utcnow still used:\n" + "\n".join(offenders)


def test_model_defaults_are_callable_naive():
    from utils.time import utcnow_naive

    import database.models as m
    import control_plane.models as cpm

    checked = 0
    for mod in (m, cpm):
        for tbl in mod.Base.registry.mappers if hasattr(mod.Base, "registry") else []:
            _ = tbl
    # Явно проверяем известные модели вместо хрупкого обхода registry API.
    for model in [m.Account, m.Mailing, m.NeuroChatMessage]:
        for col in model.__table__.columns:
            if col.name in ("created_at", "updated_at", "last_activity", "sent_at"):
                if col.default is not None and not isinstance(col.default, bool):
                    default = col.default.arg if hasattr(col.default, "arg") else None
                    if default is not None:
                        assert callable(default), f"{model.__name__}.{col.name} default not callable"
                        # SQLAlchemy 2.0 заворачивает zero-arg callable в wrapper(ctx);
                        # вызываем с dummy-контекстом как это делает ORM при INSERT.
                        val = default(None)
                        assert isinstance(val, datetime) and val.tzinfo is None
                        checked += 1
                if col.onupdate is not None and hasattr(col.onupdate, "arg"):
                    assert callable(col.onupdate.arg), f"{model.__name__}.{col.name} onupdate not callable"
    for model in [cpm.Tenant, cpm.User, cpm.Alert]:
        for col in model.__table__.columns:
            if col.name in ("created_at", "last_triggered_at"):
                if col.default is not None and hasattr(col.default, "arg"):
                    assert callable(col.default.arg), f"{model.__name__}.{col.name} default not callable"
                    checked += 1
    assert checked > 0, "expected at least one datetime default to check"
    # helper сам возвращает naive
    assert utcnow_naive().tzinfo is None


def test_serialization_no_double_offset():
    from utils.time import utcnow_aware, utcnow_naive

    naive_iso = utcnow_naive().isoformat()
    assert "+00:00" not in naive_iso, "naive ORM isoformat must have no suffix"

    aware_iso = utcnow_aware().isoformat()
    assert aware_iso.count("+00:00") == 1, "aware external ts must carry exactly one +00:00"
    assert "++" not in aware_iso and "+00:00+00:00" not in aware_iso

    # legacy naive значение из БД сериализуется без суффикса
    legacy_iso = datetime(2026, 1, 1, 12, 0, 0).isoformat()
    assert legacy_iso == "2026-01-01T12:00:00"


def test_existing_sqlite_opens_without_migration():
    import sqlite3

    base = pathlib.Path(__file__).resolve().parents[1]
    db_path = base / "data" / "corebot.db"
    if not db_path.exists():
        return  # в CI без data/ — нечего проверять
    con = sqlite3.connect(str(db_path))
    try:
        tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
        assert "accounts" in tables
    finally:
        con.close()
