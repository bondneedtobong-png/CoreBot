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
