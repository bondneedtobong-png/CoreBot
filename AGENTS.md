# AGENTS — контекст кодера CoreBot

Читать перед любой правкой. Запуск/деплой — [DEPLOY.md](DEPLOY.md), обзор — [README.md](README.md). Скилл VPS: `skills/corebot-vps-deploy/SKILL.md`.

## 1. Архитектура (что где)

- `main.py` → `bot/main.py` (Dispatcher, роутеры) + `workers/*` + `utils/background_tasks`, `utils/telemetry`. Инициализация БД `database/repository.py::db.connect()` + `_run_migrations()` (без Alembic: `CREATE TABLE IF NOT EXISTS` + `ADD COLUMN`).
- `bot/handlers/` — `accounts/` пакет (14 модулей: tdata, list_card, profile, photos, twofa, groups, proxy_assign, tags…), `database/`, `mailing.py`, `neurochat.py`, `proxy.py`, `fleet_cleanup.py`, `system_status.py`. Импорт для диспетчера: `from bot.handlers.accounts import router`.
- `workers/manager.py` (~1800 строк) — `Worker` + `WorkerManager`: пул Telethon, рассылка (`start_mailing`, ротация `messages_per_batch`), `connect_all(require_working_proxy=True)`, `precheck_proxies`, `_restore_workers_after_mailing`. `neuro_incoming.py` — thin-адаптер → `services/neurochat/`. `outbound_consumer.py` (~1.5с поллинг `outbound_queue`), `bot_command_consumer.py` (start/pause/stop из веба), `parser_worker.py` / `parser/` (каналы/группы/пользователи, `parsing_tasks` → `parsed_*`), `warmup.py`, `session_converter.py` (Tdata→session).
- `services/neurochat/` — `manager.prepare_incoming_context` (gate+prompt+history+sampling), `incoming_service` (оркестрация), `llm_service` (OpenRouter), `post_actions` (persist/send/SEND_LINK/STOP), `engagement_service` (pulse каждое входящее, alive окно 60 мин через `client_alive_windows`), `filters`, `class_bridge` → `user_class_counters`, `admin_service` (UI), `monitor` (deny-счётчики), `config_service`, `dialog_service`.
- `services/database/` — CRM: инкременты классов, `fleet_cleanup.py`, `client_export.py`, `client_delete.py`, `accept_transcript.py` (TODO: выгрузка переписки на accept не подключена к UI).
- `database/` — `models.py` (accounts, mailings, clients, neuro_*, outbound_queue, bot_commands, archive…), `repository.py` (async engine + миграции + WAL), `repositories.py`, `crm_repositories.py`, `session.py::session_scope()`, `sqlite_pragmas.py` (WAL + busy_timeout 30s + retry).
- `control_plane/` — `main.py` (монтаж, `/panel/`), `config.py` (Pydantic `CP_*`, `BOT_DATABASE_URL`), `business/db.py` (sync engine к corebot.db), `business/` (accounts, dialogs, mailings, clients, groups, proxies, parsing, instance, archive, tdata_check), `routes/` (auth, ingest, dashboard, stream, business), `services/` (alerts, sanitize, heartbeat, snapshot, watchdog). Три engine на один `corebot.db` — держать WAL/retry (см. §4).
- `web-panel/` — SPA vanilla JS (`main.js` ~4800 строк, hash-роутер, EventSource `/business/stream?token=`), без сборки: `node --check web-panel/main.js`.
- `utils/` — logger (loguru), proxy_checker, openrouter-клиент, Fernet-helper, telemetry (default `CP_AGENT_ENABLED=0`). `tools/` — `validate_config` (gate local/production), `instance_status`, `validate_skill`. `scripts/` — `cb_push.cmd`, `update_corebot.sh` (pinned SHA), `init_env.sh`, `cleanup_dialogs.py`, `release_status.sh`, `backup_lib.py`.

Потоки: рассылка `Owner→Bot→SQLite→WorkerManager→Telethon→mailing_logs`; нейрочат `Worker→neuro_incoming→gate(global→mailing→account/bl/stop)→LLM(OpenRouter)→post_actions→class_bridge`; ручной ответ `SPA→POST /business/.../send→outbound_queue→OutboundConsumer→та же Telethon-сессия→neuro_chat_messages(assistant)→SSE`.

## 2. Контракты (не ломать)

- **Инстанс:** 1 VPS = 1 `.env` + 1 `corebot.db` + свои sessions. Общее — только `API_ID/API_HASH`. Панель `127.0.0.1:8081`, наружу не открывать.
- **Конфиг:** источник — `.env.example` (~49 ключей), `COREBOT_ENV=local|production`. Production fail-fast (`tools.validate_config --mode production`, `ExecStartPre` в юнитах): пустые Telegram-ключи, `change-me*/admin123`, JWT <32, относительные sqlite-пути = старт запрещён.
- **Релиз:** артефакт `<tag> (<sha12>)` + `RELEASE.json`, живой `GET /version` без секретов, `.deployed_sha`. Обновление только pinned SHA через `update_corebot.sh`, порядок `corebot-cp`→`corebot`, состояние «новый бот + старый CP» запрещено. Exit: 0 ok/no-op, 2 до swap, 3 откат выполнен, 4 ручное восстановление.
- **SLO (30д):** бот ≥99.5%, CP ready ≥99.0% (`/health/ready` 200 ≤2с, p95 ≤1с); стоп на обновление ≤5 мин, ≤2 рестарта; RPO ≤24ч, RTO same-host ≤60 мин / new-host ≤4ч; бэкап ежедневно, drill раз в 90 дней; алерты: сервис down >2 мин, ready 503 >5 мин, диск >80/90%, БД >2/5ГБ, бэкап старше 26ч.
- **Парсер:** ровно один loop. При `PARSER_EMBEDDED=0` он запускается в боте и использует уже подключённые Telethon-клиенты; при `PARSER_EMBEDDED=1` запускается в CP, тогда аккаунты не должны быть одновременно подключены ботом. Отдельный `python -m workers.parser_worker` запускайте только вместо обоих встроенных loops.
- **SQLite:** WAL везде, `busy_timeout`, короткие транзакции, `session_scope()` + retry на commit; долгие батчи — чанками. Не плодить 4-й engine.

## 3. Команды кодера

```bash
python -m pytest tests/ -q
python -m compileall -q bot control_plane database workers services utils tools scripts tests main.py
python -m ruff check .
node --check web-panel/main.js
python -m tools.validate_skill
python -m tools.validate_config --mode local
COREBOT_ENV=production python -m tools.validate_config --mode production
start_corebot.bat --check   # Windows, без запуска
```

Полный gate = CI (см. README). `ruff format --check` — только curated-список из шапки `pyproject.toml` (33 файла задач 02–12); legacy маcсово не реформатить. Pytest-конфиг только `pytest.ini`. Перед коммитом: gate зелёный; после правки фронта — smoke `/health`→логин→Dashboard/Dialogs/Mailings; после деплоя — `journalctl -u corebot.service -u corebot-cp.service -n 120`.

## 4. Код-ревью 2026-09-23 (что чинить первым)

Критично (проверено чтением кода, без правок):
1. `bot/main.py` — прокси-URL с `user:pass` пишется в лог; `AiohttpSession(proxy=url)` не держит `socks5://` (нужен `ProxyConnector` из `aiohttp_socks` — импорт-проба есть, использования нет).
2. `workers/manager.py` — `FloodWait` сбрасывает `is_running`, но `is_connected` остаётся true → ротация бьёт в тот же аккаунт; `disconnect_all()` без try (один упавший disconnect роняет остальных); `Worker.connect()` без disconnect старого client (утечка соединений/хендлеров); `MTProxy` маппится как password вместо `secret` Telethon.
3. `workers/outbound_consumer.py::_ensure_worker` → `load_accounts()` делает `disconnect_all+clear` — роняет пул во время рассылки (race); отправка по `int(peer_id)` (`get_input_entity` часто промахивается — рассылочный путь через username правильнее); `FLOOD_WAIT/PEER_FLOOD` не в permanent — ретраится 5 раз.
4. `workers/bot_command_consumer.py` + `manager.start_mailing` — `mailing.start` всегда `ok: scheduled`, даже при `_mailing_busy` (панель врёт).
5. `main.py` — `sys.exit()` внутри `async main()` пропускает `finally` (graceful shutdown); ловится только `KeyboardInterrupt`, нет `SIGTERM/CancelledError`.
6. `control_plane/routes/admin.py` — `POST /admin/users` берёт пароль из query + не валидирует `role` (эскалация до super_admin); refresh-токен создаётся, но нигде не проверяется; CORS `["*"] + allow_credentials=True`; JWT в URL (`/business/stream?token=`) + токен в `localStorage` (XSS-кража, нет httpOnly/ротации); ошибки токена отдают текст исключения наружу.
7. `control_plane/routes/ingest.py` — PK `id=int(time*1e6)` (коллизии), ingest-схемы без `max_length`/лимита батча (log-bomb/DoS).
8. Миграции `database/repository.py` — пересборка `clients` через DROP+RENAME с вечным `clients_pre_username_backup`, `except: log+continue` (полумиграция молча стартует), дрейф полей vs `models.py`; `bootstrap_defaults` без rollback (гонка = дубли).
9. Транзакции: ~50 `commit()` без `busy_retry` (retry только точечно), часть `business/*` без rollback.

Среднее: ~130 `except Exception` (худшие глотают ошибки: `manager:1368`, `photos`, `groups`); дубли heartbeat/start-stop/load+connect в трёх консьюмерах; `neuro_incoming._registered` дублирует хендлер при пересоздании client; Pydantic местами без лимитов (`GroupAccountsSet`, `CleanupRequest.classes`, `ParsingTaskCreate.params`); `manager` без лимита размера текста → цена LLM.

Хорошее (не ломать): `session_scope+busy_retry` в консьюмерах, claim `pending→processing`, backoff+jitter, `interruptible_sleep`, `precheck_proxies(sem=25)`, proxy-gate по умолчанию, `HTML-fallback`, `safe_edit_message`, `sqlite_pragmas`, `sanitize.py` (маскирует токены/прокси/телефоны), строгие `Field/pattern/ge/le` в business-схемах, `selectinload`/bulk-классы (N+1 в основном закрыт), `NoStoreStaticFiles`.

## 5. Открытый бэклог

- Импорт TXT-листов из веба с отчётом; авто-cleanup по `cleanup_cron`; accept-транскрипты (Telethon-выгрузка → UI); sampling-пресеты creative/balanced/strict; конструктор audience-фильтра в карточке рассылки; перевод фронта на Vite/React только если экранов станет сильно больше.
- Закрыто ранее (не переоткрывать): нейро-сервисы, иерархия флагов, pulse/alive, outbound/bot-command консьюмеры, SSE, дашборд, soft-delete/archive, CRUD accounts/groups/proxies/mailings/clients, OpenRouter-канал логов, страница Queue, поиск/сортировка accounts, смена пароля/операторы, RBAC viewer read-only, задачи 01–11 production-roadmap (контракты, UTC, lifespan, валидация, SQLite-retry, релиз/rollback, флот-Ansible, observability, бэкапы, CI).
- Открытые хвосты roadmap: 12 tdata-precheck через proxy-pool, 13 web-panel UX (mojibake/UTF-8).

## 6. Правила правок

- Маленькие смысловые коммиты; `git status/diff/log` перед коммитом; секреты/БД/сессии не коммитить.
- Не менять поведение старта в local без нужды; production-гейт обязан оставаться fail-fast.
- Миграции — идемпотентные, с бэкапом затрагиваемой таблицы; никакого ручного ALTER на проде.
- Веб-панель не дергает Telegram напрямую — только через `outbound_queue`/`bot_commands`.
- Не открывать 8081 наружу; не класть секреты в URL/логи; JWT-пароли только через body + валидация ролей.
- Не запускать `install` поверх живого `/opt/corebot/app`; обновление только по §2.3 DEPLOY.
