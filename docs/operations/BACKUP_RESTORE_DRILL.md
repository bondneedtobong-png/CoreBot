# Backup/restore drill (задача 09)

Повторять не реже 1 раза в 90 дней (SLO, раздел 4). Следующий drill после
зафиксированного ниже: не позже **2026-12-21**.

## Последний drill: 2026-09-22 (локальная симуляция, VPS недоступен)

Окружение: Windows + WSL bash, systemd/cron/systemd-timer на машине
отсутствуют — drill выполнен на симуляции инстанса в tmp-каталоге
(структура `.env` + `data/` + `logs`, SYNTHETIC-секреты, тестовые SQLite в
WAL-режиме, fake-sessions). Юниты `corebot-backup.service/.timer`
проверены статически (тесты `test_backup_service_unit`,
`test_backup_timer_unit`, `test_timer_and_service_idempotent_single_job`);
запуск timer на VPS — после деплоя юнитов, вручную по RUNBOOK §11.

### Шаги и измерения

1. Fixture: `.env` (синтетика), `corebot.db`/`control_plane.db`
   (`user_version` 3/5, 200 строк), `data/sessions/acc.session`, лог.
2. Запущен параллельный WAL-writer в `corebot.db` (25 с, ~4.05 млн
   INSERT — нагрузка заведомо выше продовой).
3. Во время записи выполнен
   `skills/corebot-vps-deploy/scripts/backup_corebot.sh`:
   **BACKUP-WALL=1s**, архив 1 219 331 байт, режимы `600` (архив,
   `.sha256`, `.last_backup_ok`) / `700` (каталог).
4. Тем же прогоном выполнен
   `skills/corebot-vps-deploy/scripts/restore_corebot.sh` в пустой каталог:
   sidecar ok, manifest+checksums ok, права (dirs 755, files 644,
   `.env 600`), **RESTORE-WALL=1s**.
5. `PRAGMA integrity_check` обеих восстановленных БД: **ok / ok**.
6. Снапшот point-in-time consistent: live-строк 4 051 026 (writer продолжал
   работать), в восстановленной БД 319 281 — состояние на момент backup,
   без разрывов WAL.

### Выводы

- Фактический локальный RTO-drill (backup + restore + verify): **~24 с
  wall** (из них ~23 с — ожидание завершения фонового writer; сами
  backup+restore ≈ 2 с). Бюджет RTO на том же VPS (≤ 60 мин) и на новом
  VPS (≤ 4 ч) покрыт с запасом на порядки; на VPS добавятся только
  рестарт сервисов и health-гейт (минуты).
- RPO ≤ 24 ч обеспечивается ежедневным timer (03:17 + `RandomizedDelaySec`
  15 мин, `Persistent=true`); просрочка > 26 ч ловится watchdog
  (`storage:backup-stale`) и видна в `tools/instance_status`.
- Контракт restore подтверждён тестами: отказ в непустой каталог (exit 4),
  запрет перезаписи live-инстанса даже с `--allow-nonempty` (exit 4, данные
  нетронуты), отказ при расхождении sidecar (exit 3) и manifest (exit 5).

## Как повторить

Автоматически (все проверки выше, синтетика в tmp/WSL-/tmp):

```bash
python -m pytest tests/test_backup_restore.py -q
```

Вручную на VPS (пустой каталог, архив из `/opt/corebot/backups/`):

```bash
sudo -u corebot bash /opt/corebot/app/skills/corebot-vps-deploy/scripts/restore_corebot.sh \
  --archive /opt/corebot/backups/<corebot-YYYYMMDDTHHMMSSZ.tar.gz> --target /tmp/restore-drill
python3 /opt/corebot/app/scripts/backup_lib.py integrity-check --db /tmp/restore-drill/data/corebot.db
python3 /opt/corebot/app/scripts/backup_lib.py integrity-check --db /tmp/restore-drill/data/control_plane.db
```

Результат каждого drill дописывать сверху в этот файл (дата, шаги,
измеренное время, выводы) и обновлять дату следующего drill.
