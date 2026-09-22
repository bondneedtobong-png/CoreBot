"""Единая UTC-модель времени CoreBot.

Правило (зафиксировано задачей 02, действует до отдельной миграции схемы):

- ORM ``DateTime`` без ``timezone`` продолжает получать **naive UTC**
  через :func:`utcnow_naive`. Исторические данные и типы колонок не меняются;
  существующая SQLite-база открывается без миграции.
- :func:`utcnow_aware` — только для внешних протоколов / ISO-сериализации
  наружу (SSE ``ts``, телеметрия). Значения из БД при сериализации остаются
  naive (``.isoformat()`` без суффикса ``+00:00``), свежие внешние метки —
  aware (``.isoformat()`` с одним ``+00:00``).

Причины: legacy-вызов ``datetime.utcnow`` deprecated в Python 3.12+
(DeprecationWarning, удаление в будущих версиях), а смешивание naive/aware
даёт ``TypeError: can't compare offset-naive and offset-aware datetimes``.

Модуль без побочных эффектов при импорте.
"""
from __future__ import annotations

from datetime import datetime, timezone


def utcnow_naive() -> datetime:
    """Текущий момент UTC как naive datetime для SQLite-схемы/ORM.

    Эквивалент legacy ``datetime.utcnow`` без DeprecationWarning.
    Возвращаемое значение всегда ``tzinfo is None``.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utcnow_aware() -> datetime:
    """Текущий момент UTC как aware datetime для внешних протоколов.

    Использовать только для ISO-сериализации наружу (SSE/telemetry).
    НЕ записывать в ORM-колонки ``DateTime`` без timezone.
    """
    return datetime.now(timezone.utc)
