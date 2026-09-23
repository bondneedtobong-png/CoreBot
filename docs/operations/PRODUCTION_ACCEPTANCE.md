# Production acceptance: воспроизводимый сценарий чистого VPS

Статус сценария: **НЕ ВЫПОЛНЕН — BLOCKED: no VPS** (см. `GO_LIVE_REPORT.md`).
Окружение задачи 11: чистого Ubuntu VPS нет, подключаться к production-серверам
запрещено, реальных Telegram-аккаунтов/токенов для рассылки нет. Поэтому этот
документ — исполняемый план приёмки для оператора на первом disposable VPS;
ни один шаг ниже локально не прогонялся и не отмечается выполненным.

Локально выполнено вместо VPS-прогона (честные доказательства, task 11):

- `tests/test_load_acceptance.py` — load-сценарий all-mocks (DB storm, API,
  drain, lifecycle, backup/restore), 2/2 зелёных;
- полный pytest: **217/217** зелёных; ruff check + format — чисто;
- TData-регрессия уже покрыта: `tests/test_tdata_web_import.py` зелёный
  (не дублируется здесь).

## Правила выполнения

1. Все секреты и адреса — только плейсхолдеры (`<VPS_IP>`, `<BOT_TOKEN>`,
   `<OWNER_ID>`, `<SHA>`). Реальные токены/IP в этот файл не вписывать.
2. Никакой реальной массовой рассылки и никаких действий, нарушающих
   Telegram ToS. Тестовая рассылка — только на **свои** тестовые аккаунты.
3. Каждый шаг: команда → ожидаемый результат → заполнить поля
   `timestamp (UTC)` / `version/SHA` / `факт`. Шаг без заполненных полей
   считается невыполненным.
4. Бюджеты SLO рядом с шагами: остановка сервисов ≤ 5 мин, `/health/ready`
   200 в течение ≤ 3 мин после рестарта, `/start` ≤ 5 мин, RTO ≤ 60 мин
   (тот же VPS) / ≤ 4 ч (новый VPS), RPO ≤ 24 ч.
5. Источник истины по шагам одного хоста — `RUNBOOK.md`; по парку —
   `docs/operations/FLEET_OPERATIONS.md`. Этот файл их не дублирует,
   а фиксирует порядок приёмки и поля результата.

## 0. Preflight чистого VPS

| № | Команда | Ожидаемый результат | timestamp (UTC) | version/SHA | факт |
|---|---------|---------------------|-----------------|-------------|------|
| 0.1 | `ssh <SSH_USER>@<VPS_IP>` | вход по ключу, `lsb_release -a` → Ubuntu 22.04/24.04 | | | |
| 0.2 | `python3 --version` (≥ 3.11) | версия подходит либо ставится 3.11 по контракту | | | |
| 0.3 | `df -h / /opt`, `free -h` | ≥ 2 ГБ свободно (MIN_FREE_MB update-скрипта), RAM зафиксировать | | | |
| 0.4 | Собраны секреты человека: `<BOT_TOKEN>`, `<OWNER_ID>` (карточка RUNBOOK §3) | карточка заведена, секреты только локально, не в Git | | | |
| 0.5 | Выбран pinned SHA релиза: `git rev-parse --short=12 <TAG>` → `<SHA>` | SHA записан, «latest master» запрещён | | | |

## 1. Install pinned SHA

| № | Команда | Ожидаемый результат | timestamp (UTC) | version/SHA | факт |
|---|---------|---------------------|-----------------|-------------|------|
| 1.1 | `sudo bash skills/corebot-vps-deploy/scripts/install_corebot.sh --source-dir <REPO> --env-file <LOCAL_ENV> --dry-run` | `DRY RUN OK`, план без изменений | | `<SHA>` | |
| 1.2 | `sudo bash skills/corebot-vps-deploy/scripts/install_corebot.sh --source-dir <REPO> --env-file <LOCAL_ENV>` | юниты созданы, сервисы `enable --now`, `.env` 600, `corebot:corebot` | | `<SHA>` | |

## 2. Validator production + first start

| № | Команда | Ожидаемый результат | timestamp (UTC) | version/SHA | факт |
|---|---------|---------------------|-----------------|-------------|------|
| 2.1 | `cd /opt/corebot/app && COREBOT_ENV=production /opt/corebot/venv/bin/python -m tools.validate_config --mode production` | exit 0, без fail-fast ошибок | | `<SHA>` | |
| 2.2 | `systemctl is-active corebot.service corebot-cp.service` | `active / active` | | | |
| 2.3 | `ss -ltnH '( sport = :8081 )'` | слушает только `127.0.0.1:8081`, `0.0.0.0:8081` нет | | | |

## 3. Login + readiness

| № | Команда | Ожидаемый результат | timestamp (UTC) | version/SHA | факт |
|---|---------|---------------------|-----------------|-------------|------|
| 3.1 | `ssh -L 8081:127.0.0.1:8081 <SSH_USER>@<VPS_IP>`, открыть `http://127.0.0.1:8081/panel/`, логин `<ADMIN>/<ПАРОЛЬ>` | вход успешен | | | |
| 3.2 | `curl --fail http://127.0.0.1:8081/health/live` | HTTP 200 `{"ok":true}` за ≤ 2 с | | | |
| 3.3 | `curl --fail http://127.0.0.1:8081/health/ready` | HTTP 200, все компоненты `ok`, за ≤ 2 с (SLO) | | | |
| 3.4 | `curl --fail http://127.0.0.1:8081/version` | `version`+`sha` == `<SHA>`, без секретов | | `<SHA>` | |
| 3.5 | `/start` от `<OWNER_ID>` в Telegram | ответ бота в течение ≤ 5 мин (SLO) | | | |
| 3.6 | `python -m tools.instance_status --json` на хосте | `overall` ok/degraded, без секретов в выводе | | | |

## 4. TData import (только свои тестовые аккаунты)

Зависимость: task 12 (TData precheck через proxy pool). Без закрытой task 12 —
только импорт ранее проверенных собственных сессий, новых аккаунтов не лить.

| № | Команда | Ожидаемый результат | timestamp (UTC) | version/SHA | факт |
|---|---------|---------------------|-----------------|-------------|------|
| 4.1 | `POST /business/tdata/import` (панель) с собственным TData | аккаунт создан, повторный импорт идемпотентен (тот же id) | | | |
| 4.2 | Проверка сессии в панели | аккаунт connected | | | |

## 5. Parser (один режим!)

| № | Команда | Ожидаемый результат | timestamp (UTC) | version/SHA | факт |
|---|---------|---------------------|-----------------|-------------|------|
| 5.1 | `grep PARSER_EMBEDDED /opt/corebot/app/.env` | ровно `0` или `1`; при `1` отдельного parser-процесса нет | | | |
| 5.2 | Создать parsing task в панели (маленький scope, свои источники) | task `completed`, строки в результатах, без дублей | | | |

## 6. Test mailing (только свои тестовые получатели)

| № | Команда | Ожидаемый результат | timestamp (UTC) | version/SHA | факт |
|---|---------|---------------------|-----------------|-------------|------|
| 6.1 | Рассылка в тестовом режиме на свои аккаунты (MailingTestRecipient) | доставка только тестовым получателям, INVALID исключены | | | |
| 6.2 | `grep -c FloodWait /opt/corebot/app/logs/error.log` | 0 новых FloodWait за тест | | | |

## 7. Нейрочат (mocked/controlled LLM)

На VPS нет моков кода — «controlled» означает изолированный ключ/лимиты,
а не продовый трафик.

| № | Команда | Ожидаемый результат | timestamp (UTC) | version/SHA | факт |
|---|---------|---------------------|-----------------|-------------|------|
| 7.1 | Нейрочат с тестовым ключом/лимитом на диалог с собственным аккаунтом | ответ получен, fallback при отсутствии ключа — graceful, без падения | | | |
| 7.2 | Отключить ключ → повторить | корректное сообщение об отключении LLM, бот жив | | | |

## 8. Manual outbound

| № | Команда | Ожидаемый результат | timestamp (UTC) | version/SHA | факт |
|---|---------|---------------------|-----------------|-------------|------|
| 8.1 | Ручное сообщение себе из панели (outbound_queue) | статус `sent`, запись в истории диалога | | | |

## 9. Graceful shutdown

| № | Команда | Ожидаемый результат | timestamp (UTC) | version/SHA | факт |
|---|---------|---------------------|-----------------|-------------|------|
| 9.1 | `systemctl stop corebot.service corebot-cp.service` → `systemctl start ...` (cp, затем bot) | остановка ≤ 5 мин, после старта `/health/ready` 200 ≤ 3 мин | | | |

## 10. Update новой версии

| № | Команда | Ожидаемый результат | timestamp (UTC) | version/SHA | факт |
|---|---------|---------------------|-----------------|-------------|------|
| 10.1 | `sudo bash scripts/update_corebot.sh --sha <NEW_SHA> --dry-run` | exit 0, изменений нет | | `<NEW_SHA>` | |
| 10.2 | `sudo bash scripts/update_corebot.sh --sha <NEW_SHA>` | exit 0, backup создан, `/version` sha == `<NEW_SHA>`, `/health/ready` 200 ≤ 3 мин, `/start` ≤ 5 мин | | `<NEW_SHA>` | |
| 10.3 | `bash scripts/release_status.sh` | статус подтверждает версию, без секретов | | `<NEW_SHA>` | |

## 11. Искусственный health failure

| № | Команда | Ожидаемый результат | timestamp (UTC) | version/SHA | факт |
|---|---------|---------------------|-----------------|-------------|------|
| 11.1 | `systemctl stop corebot.service` (ждать > 2 мин) | Telegram-алерт Control Plane + журнал (SLO #1), `/health/ready` → 503 через > 5 мин (SLO #3) | | | |
| 11.2 | `systemctl start corebot.service` | recovery-алерт, `/health/ready` 200 | | | |

## 12. Rollback

| № | Команда | Ожидаемый результат | timestamp (UTC) | version/SHA | факт |
|---|---------|---------------------|-----------------|-------------|------|
| 12.1 | Откат по `RELEASE_CONTRACT.md` §5 (код `<PREV_SHA>` + restore предобновленческого архива) | `/version` sha == `<PREV_SHA>`, данные на момент бэкапа, RTO ≤ 60 мин, факт отката в карточке | | `<PREV_SHA>` | |

## 13. Backup/restore на disposable instance

| № | Команда | Ожидаемый результат | timestamp (UTC) | version/SHA | факт |
|---|---------|---------------------|-----------------|-------------|------|
| 13.1 | `sudo -u corebot bash skills/corebot-vps-deploy/scripts/backup_corebot.sh` | архив `corebot-<STAMP>.tar.gz` 600 + `.sha256`, BACKUP-WALL зафиксировать | | | |
| 13.2 | `sudo -u corebot bash skills/corebot-vps-deploy/scripts/restore_corebot.sh --archive /opt/corebot/backups/<ARCHIVE> --target /tmp/restore-drill` | restore в **пустой** каталог, manifest+checksums ok, права (dirs 755, files 644, `.env` 600) | | | |
| 13.3 | `integrity_check` обеих восстановленных БД | `ok / ok` | | | |
| 13.4 | RESTORE-WALL total (backup + restore + verify) | ≤ 30 мин (триггер ADR 0002 #6), факт в `BACKUP_RESTORE_DRILL.md` | | | |

## 14. Cleanup + load-сверка

| № | Команда | Ожидаемый результат | timestamp (UTC) | version/SHA | факт |
|---|---------|---------------------|-----------------|-------------|------|
| 14.1 | `rm -rf /tmp/restore-drill`, тестовые рассылки/таски удалены | на инстансе нет тестового мусора | | | |
| 14.2 | `python -m pytest tests/test_load_acceptance.py -q` (локально или на хосте) | зелёный прогон, метрики сравнить с `GO_LIVE_REPORT.md` | | | |
| 14.3 | Карточка инстанса (RUNBOOK §3) обновлена: версии, даты, drill | «добавить VPS без импровизации» — да/нет, честно | | | |
