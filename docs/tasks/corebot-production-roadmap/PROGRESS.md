# CoreBot production queue — журнал приёмки

Оркестратор: `docs/tasks/MUSE-SPARK-1.3-ORCHESTRATOR.md`. Порядок строго `01 → 11`.
База: `083c525`. В commit включаются только файлы текущей задачи; `docs/tasks/` — входная очередь, не продукт.

## 01 — Эксплуатационные контракты и ADR ✅ принята 2026-09-22

- Commit: (см. git log ниже, `docs: task 01 operational contracts + ADR`).
- База исполнения: `083c525d2e0b380953e51b0b592c02956b463690`.
- Изменённые файлы:
  - `docs/operations/INSTANCE_CONTRACT.md` (нов.)
  - `docs/operations/SLO.md` (нов.)
  - `docs/operations/RELEASE_CONTRACT.md` (нов.)
  - `docs/operations/adr/0001-fleet-management.md` (нов.)
  - `docs/operations/CONFIG_CONTRACT.md` (нов., 49 env из `.env.example`)
  - `docs/ARCHITECTURE.md` (+6 строк ссылок, без переписывания)
- Проверки: `git diff --check` чист; grep `TODO|TBD|FIXME|...` пуст;
  «разумный»/«быстрый» отсутствуют; скан секретов (токены/IP/ключи) пуст;
  зафиксированы Python 3.11+, Ubuntu 22.04/24.04, loopback `127.0.0.1:8081`;
  SLO/RPO/RTO числом (99.5 %/99.0 %, простой обновления ≤5 мин / ≤30 мин/мес,
  RPO ≤24 ч, RTO ≤60 мин / ≤4 ч); ADR 0001 выбирает Ansible с control node
  (WSL/Linux), секреты — Vault, без секретов в Git; Bash — примитивы одного
  хоста, центральный агент отклонён.
- Замечания/риски для следующих задач: SLO-цифры — допущения, валидировать
  метриками первых недель (задачи 08/11); задача 07 обязана покрыть
  Vault-политику и инвентарь «хост=пользователь»; миграции forward-only,
  откат = restore бэкапа;   `OPENROUTER_KEY_ENCRYPTION_KEY` обязателен при
  ключе в SQLite — не ослаблять.

## 02 — Единая UTC-модель времени ✅ принята 2026-09-22

- Commit: (см. git log, `refactor: task 02 unified UTC time model`).
- База исполнения: `e20e2e3` (задача 01).
- Изменённые файлы: нов. `utils/time.py` (`utcnow_naive`/`utcnow_aware` + docstring-правило
  naive UTC для ORM до отдельной миграции схемы), нов. `tests/test_utc_time_model.py`
  (9 тестов); замены `datetime.utcnow` → helper (~150 точек) в 28 файлах:
  `bot/handlers/mailing.py`, `bot/handlers/proxy.py`, `control_plane/auth.py`,
  `control_plane/business/{archive,dashboard,mailings,parsing,proxies}.py`,
  `control_plane/models.py`, `control_plane/routes/{business,dashboard,ingest,stream}.py`,
  `control_plane/services/alerts.py`, `control_plane/tasks.py`,
  `database/{models,crm_repositories,repositories}.py`, `scripts/cleanup_dialogs.py`,
  `services/database/client_export.py`, `utils/{app_logs,telemetry}.py`,
  `workers/{bot_command_consumer,manager,warmup}.py`,
  `workers/parser/{account_pool,filters,task_runner}.py`.
  Наружу (SSE hello/ping, telemetry `ts`) — aware `+00:00`; значения из БД при
  `.isoformat()` остались naive; `MAILING_BASE_UTC_OFFSET`, типы колонок,
  исторические данные не тронуты.
- Проверки (оркестратор, независимо от исполнителя): поиск `datetime.utcnow`
  по `bot control_plane database services workers utils main.py` — 0 в коде
  (только docstring-упоминания без скобок в `utils/time.py`); evaluated-at-import
  defaults (`default=utcnow_naive()`) — 0; `python -m compileall -q ...` — exit 0;
  `tests/test_utc_time_model.py` — 9/9; полный `pytest -q` — 62 passed, 2 failed;
  оба падения — `tests/test_tdata_web_import.py` (`python-multipart` отсутствует),
  доказано предсуществующими прогоном на чистом `e20e2e3` через `git stash`
  (там же 2 failed); `data/corebot.db` открывается read-only, `integrity_check=ok`,
  `accounts=5`, без миграции.
- Отклонение от строгого DoD: `pytest -q` не полностью зелёный по причине вне скоупа
  задачи (нет `python-multipart` в окружении) — передано задаче 10 (CI/requirements).
- Риски следующим задачам: формат внешних `ts` naive → `+00:00` — проверить строгих
  потребителей в 03/08; возможны isort/ruff-замечания по порядку импортов (задача 10);
  остаточные `datetime.now(timezone.utc)` вне скоупа — аудит naive/aware в задаче 05.

## 03 — FastAPI lifespan и безопасный bootstrap ✅ принята 2026-09-22

- Commit: (см. git log, `refactor: task 03 FastAPI lifespan, no import side effects`).
- База исполнения: `0a3b402` (задача 02).
- Изменённые файлы: `control_plane/main.py` (lifespan через `asynccontextmanager`,
  `FastAPI(..., lifespan=...)`; `bootstrap_defaults()` только в startup-части;
  единый try/finally: bootstrap → `bot_db.connect()` + именованная задача
  `embedded-parser-loop` → yield → cancel/await → `bot_db.disconnect()`;
  оба `@app.on_event` удалены; вызов bootstrap при импорте удалён;
  `PARSER_EMBEDDED` вынесен в `is_parser_embedded_enabled()`; URL/API/health-JSON
  без изменений), нов. `tests/test_lifespan_bootstrap.py` (5 тестов).
- Проверки (оркестратор): `on_event` в `control_plane` — 0 совпадений;
  `git diff --check` чист; scoped diff только `main.py` + новый тест;
  структура lifespan и отсутствие вызова bootstrap при импорте — чтением файла;
  полный `pytest -q` — 67 passed (62 + 5 новых), 2 failed — те же предсуществующие
  `test_tdata_web_import` (нет `python-multipart`), неизменны с базы;
  on_event-DeprecationWarning исчез (остался только baseline-варнинг loguru).
- Риски следующим задачам (от исполнителя): `CP_BOOTSTRAP_*` читаются при импорте
  `config.py`, fail-fast на `admin123` — зона задачи 04; `bootstrap_defaults()`
  синхронен внутри lifespan — кандидат на `to_thread` (задача 05); стаб
  `tdata_routes` в новом тесте условный — при установленном multipart тесты идут
  по реальному пути.

## 04 — Централизованная конфигурация и production-secrets ✅ принята 2026-09-22

- Commit: (см. git log, `feat: task 04 centralized config validation + production fail-fast`).
- База исполнения: `49f99d1` (задача 03).
- Создано: `tools/__init__.py`, `tools/validate_config.py` (stdlib-only, env
  перечитывается при вызове; `validate(mode, env)`, `require_valid_production_config()`,
  CLI `--mode local|production [--env-file]`, exit 0/1/2; секреты не печатаются —
  только имена + длина), `tests/test_config_validation.py` (39 тестов).
- Изменено: `main.py` + `control_plane/main.py` (production-gate до `db.connect()`/
  `bootstrap_defaults()`, local — no-op); `start_corebot.bat --check` на общем
  валидаторе; `skills/corebot-vps-deploy/scripts/{install,verify}_corebot.sh`
  (вызов валидатора + `ExecStartPre --mode production` в генерируемых юнитах);
  `skills/.../SKILL.md`, `references/deployment.md`; `.env.example` (`COREBOT_ENV`
  + fail-fast-комментарии); `QUICKSTART.md`, `RUNBOOK.md` (минимально, по контракту).
- Проверки (оркестратор, независимо): `tests/test_config_validation.py` — 39/39;
  полный `pytest -q` — 106 passed + те же 2 предсуществующих tdata/multipart;
  эталоны в TEMP (удалены): prod-дефолт → exit 2 (8 ошибок), prod-синтетика → 0,
  `--mode bogus` → 1; скан значений секретов в обоих выводах — 0 совпадений;
  `git diff --check` чист; `.env`/`data/*` не тронуты, секретов в репо нет.
- Отклонение от текста задачи (обоснованное, принято): Bash-валидация НЕ удалена —
  оставлена как pre-venv early gate (работает без Python до установки venv),
  валидатор добавлен как authoritative runtime-gate + ExecStartPre.
- Риски следующим задачам: production требует абсолютных sqlite-путей — задача 05
  не должна их делать относительными; installer падает раньше (exit валидатора) —
  учесть в rollback-процедурах задачи 06; fleet-генератор задачи 07 — плоский
  `KEY=value` без expansion; при новых env-переменных пополнять `KNOWN_KEYS`
  в тестах (задача 10).

## 05 — Надёжность SQLite и транзакций ✅ принята 2026-09-22

- Commit: (см. git log, `fix: task 05 unified SQLite PRAGMAs, busy retry, UPSERTs`).
- База исполнения: `0dea82d` (задача 04).
- Создано: `database/sqlite_pragmas.py` (единые WAL / foreign_keys=ON /
  busy_timeout=30000 / wal_autocheckpoint=1000; `register_*` для sync и async;
  retry только transient `SQLITE_BUSY` ≤5 попыток, exp-backoff+jitter,
  `SQLITE_BUSY_RETRY_STATS`; таблица инвентаризации трёх engine в docstring),
  `tests/test_sqlite_reliability.py` (11 тестов),
  `docs/operations/adr/0002-sqlite-scaling-limits.md` (6 числовых порогов PG-триггера).
- Изменено (10 файлов): `database/repository.py`, `database/repositories.py`,
  `database/crm_repositories.py`, `control_plane/business/db.py`,
  `control_plane/database.py`, `control_plane/business/mailings.py`,
  `control_plane/routes/business.py`, `control_plane/business/tdata_routes.py`,
  `workers/parser/storage.py`, `workers/bot_command_consumer.py` —
  helper вместо инлайн-PRAGMA, UPSERT/ON CONFLICT + точечные idempotent-хендлеры
  (Account.create, Client.create, counters, alive-window, sessions, groups,
  transcripts, stops, mailing-state, proxy-cursor). Пути/URL БД не менялись.
- Проверки (оркестратор): `git diff --check` чист; `BEGIN IMMEDIATE` — 0;
  новые `except IntegrityError` — только 2 точечных (rollback + возврат
  существующей строки, чтением кода); blanket-отсутствует; PRAGMA-паритет и
  инвенторизация — чтением helper; `test_sqlite_reliability` — 11/11;
  смежные concurrency/DB-тесты — зелёные; полный `pytest -q` — 117 passed +
  те же 2 предсуществующих tdata/multipart; контракты 01, `.env`, `data/*`
  не тронуты.
- Риски следующим задачам: playbook задачи 07 — проверка PRAGMA-паритета;
  задаче 08 забрать `SQLITE_BUSY_RETRY_STATS` в алерты; drill задачи 09 меряет
  порог restore ≤30 мин; нагрузке задачи 11 упереться в пороги ADR — ожидаемо.

## 06 — Версионируемый deploy, update и rollback одного VPS ✅ принята 2026-09-22

- Commit: (см. git log, `feat: task 06 versioned single-VPS update + auto-rollback`).
- База исполнения: `ee7d8bd` (задача 05).
- Создано: `VERSION` (0.1.0), `control_plane/version.py` (`GET /version`,
  allowlist version/sha/python_requires/ubuntu/released_at/code_checksum, без
  секретов; `/health*` не тронуты), `scripts/release_lib.py`,
  `scripts/make_release.py`, `scripts/release_status.sh`,
  `tests/test_release_workflow.py` (27 тестов).
- Изменено: `scripts/update_corebot.sh` (переписан: preflight → backup →
  stage → deps → stop/swap cp→bot → start cp→bot → readiness-gate + version-паритет
  → success(.deployed_sha) | авторолбэк + повторный gate; exit 0/2/3/4; --dry-run;
  no-op; rsync --checksum), `control_plane/main.py` (+version_router),
  `.gitignore` (RELEASE.json, .deployed_sha), `scripts/init_env.sh` (только
  CRLF→LF), skill SKILL.md/deployment.md/troubleshooting.md, `RUNBOOK.md` (§17
  восстановление после неудачного обновления).
- Проверки (оркестратор): `bash -n` всех 6 .sh — OK (системным bash.exe);
  `init_env.sh` diff — только line-endings; `/version` — чтением кода
  (allowlist, секретов нет); `test_release_workflow` — 27/27; полный `pytest -q` —
  144 passed + те же 2 предсуществующих tdata/multipart; `git diff --check` чист;
  контракты, `.env`, `data/*` не тронуты; `git reset --hard` на persistent —
  отсутствует. Симуляция rollback (мок systemctl/curl, tmp-репо v1→v2) — по отчёту
  исполнителя: exit 3, код v1, persistent целы, порядок cp→bot ×2.
- Примечание: персональный `~/.codex/skills/corebot-vps-deploy` синхронизирован
  исполнителем (вне git, не коммитится).
- Риски следующим задачам: `docs/VPS_UPDATE_GUIDE.md` описывает старый flow —
  обновить в задаче 07; алерты на exit 3/4 и version-MISMATCH — задача 08;
  ручной откат из RUNBOOK §17 прогнать в drill задачи 09; CI задачи 10 — `bash -n`
  + e2e на Linux; первый реальный VPS-прогон update — только с --dry-run и бэкапом.
