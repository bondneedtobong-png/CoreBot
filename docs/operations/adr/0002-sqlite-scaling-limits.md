# ADR 0002: Пределы SQLite и измеримые условия перехода на PostgreSQL

- Статус: принято.
- Дата: 2026-09-22.
- Контекст: задача 05. Один инстанс CoreBot держит `data/corebot.db`
  одновременно из трёх писателей (async-бот, sync Control Plane, парсер)
  плюс отдельную `data/control_plane.db`. Настройки унифицированы
  (`database/sqlite_pragmas.py`: WAL, `foreign_keys=ON`, `busy_timeout=30000`,
  `wal_autocheckpoint=1000`), конкурентные записи идут через SQLite
  `ON CONFLICT`, остаточные `SQLITE_BUSY` — через ограниченный retry (≤5
  попыток). Этот ADR фиксирует, при каких измеренных числах SQLite
  признаётся исчерпанным.
- Решение по умолчанию: оставаться на SQLite. Переход на PostgreSQL
  запускается только при срабатывании любого порога из раздела 2
  (два замера подряд с интервалом ≥24 ч, не разовый всплеск).

## 1. Инварианты SQLite-режима (действуют всегда)

- Все engine получают PRAGMA из `database/sqlite_pragmas.py`; дрейф настроек
  запрещён (тест `tests/test_sqlite_reliability.py::test_all_three_engines_get_identical_pragmas`).
- Миграции только forward-only; откат — restore из бэкапа
  (см. `RELEASE_CONTRACT.md`, разделы 4–5).
- Бэкап `data/` + `.env` ежедневно (RPO ≤24 ч); проверка восстановления
  `sqlite3 … "PRAGMA integrity_check;" → ok` не реже 1 раза в 90 дней
  (см. `SLO.md`, раздел 4).

## 2. Пороги перехода (любой из них = триггер)

| # | Порог-триггер | Как мерить (команда/метрика) |
|---|---------------|------------------------------|
| 1 | WAL-файл `data/corebot.db-wal` > 256 МБ дольше 1 часа, либо `PRAGMA wal_checkpoint(TRUNCATE)` возвращает `busy` 3 раза подряд | `ls -la /opt/corebot/app/data/*.db-wal` каждый час; `sqlite3 /opt/corebot/app/data/corebot.db "PRAGMA wal_checkpoint(TRUNCATE);"` вручную при росте |
| 2 | p95 latency однострочной записи (INSERT/UPDATE по PK) > 500 мс на 10-минутном окне | замер в еженедельном обходе: `sqlite3` + `.timer on`, 100× `INSERT` в scratch-таблицу tmp-БД на том же диске; p95 считается по выводу |
| 3 | Частота transient `SQLITE_BUSY`-ретраев > 5 событий/мин дольше 15 минут, либо счётчик `exhausted` > 0 дважды за 24 ч | счётчик `SQLITE_BUSY_RETRY_STATS` (`database/sqlite_pragmas.py`, заберёт задача 08); до неё — `grep -c "SQLite busy" /opt/corebot/app/logs/error.log` за 15-минутные окна |
| 4 | Размер `data/corebot.db` > 5 ГБ (критический порог SLO; предупреждение > 2 ГБ) | еженедельно `du -sh /opt/corebot/app/data/corebot.db` (SLO, раздел 5, алерт 5) |
| 5 | > 3 OS-процессов одновременно пишут `corebot.db` дольше 1 часа (сверх штатных: бот + CP + один парсер) | `fuser /opt/corebot/app/data/corebot.db` + `systemctl status`; второй parser-процесс запрещён контрактом инстанса (п.6) и считается инцидентом, а не нормой |
| 6 | RTO-риск: drill восстановления бэкапа (restore + `integrity_check` обеих БД + `/health/ready` 200) занимает > 30 минут, либо tar-бэкап > 2 ГБ | drill из задачи 09 на пустом каталоге с секундомером; размер — `ls -la /opt/corebot/backups/` |

## 3. Что НЕ является триггером

- Разовый всплеск WAL или единичный `database is locked` при рестарте сервисов.
- Рост БД до 2 ГБ без нарушения остальных порогов (действует предупреждение SLO, не миграция).
- Желание «центральной SaaS-панели»: запрещено `RELEASE_CONTRACT.md` (раздел 2),
  решается парком инстансов (ADR 0001), а не сменой СУБД.

## 4. Процедура при срабатывании

1. Зафиксировать два замера триггера (≥24 ч между ними) в карточке инстанса.
2. Снять backup-before-change (тот же формат, что перед обновлением).
3. Отдельной задачей спроектировать миграцию (схема, перенос WAL→PG,
   forward-only план, откат=restore); этот ADR миграцию не описывает.

## Связанные документы

- `INSTANCE_CONTRACT.md` — один VPS, две SQLite-БД, режимы парсера.
- `SLO.md` — RPO ≤24 ч, RTO ≤60 мин / ≤4 ч, алерты диска и размера БД.
- `RELEASE_CONTRACT.md` — forward-only миграции, откат через restore.
- `database/sqlite_pragmas.py` — единые PRAGMA и retry-метрика.
- `tests/test_sqlite_reliability.py` — тесты паритетности и идемпотентности.
