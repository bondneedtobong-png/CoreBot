# CoreBot

Управление сетью userbot-аккаунтов Telegram: массовые рассылки, нейрочат, парсинг аудитории, ручные ответы и аналитика — через Telegram-бота владельца и веб-панель оператора. Self-hosted: всё работает в одном Python-окружении на одном VPS, без облаков и сторонних SaaS.

> Статус: активная разработка · Python 3.11+ · SQLite · self-hosted (systemd)

---

## Что это

CoreBot — двухкомпонентная система:

1. **Control Bot** (aiogram 3) — Telegram-бот владельца. Через него управляются аккаунты, прокси, клиенты, парсинг, рассылки и нейрочат.
2. **Worker accounts** (Telethon) — userbot-аккаунты, которые физически шлют сообщения, ведут диалоги и отвечают LLM-ответами.

Поверх этого — **Control Plane** (FastAPI + SPA) с real-time-просмотром диалогов, ручными ответами, CRUD-редакторами рассылок, аккаунтов, клиентов, прокси и групп, встроенной телеметрией и парсингом.

---

## Стек

| Слой | Стек |
|------|------|
| Язык / рантайм | Python 3.11+ (проверено на 3.12–3.14) |
| Control Bot | aiogram >=3.3, aiohttp |
| Workers | Telethon >=1.34, tgconvertor[tddata], PyQt5 |
| LLM | OpenRouter (любая совместимая модель; по умолчанию `openai/gpt-oss-120b:free`) |
| Web backend | FastAPI >=0.115, uvicorn, Pydantic v2, PyJWT, passlib[bcrypt], cryptography |
| Web frontend | Vanilla JS (hash-роутер), Tailwind CDN, Alpine.js, EventSource (SSE) |
| ORM | SQLAlchemy >=2.0 |
| БД | SQLite (aiosqlite), WAL-режим |
| Логирование | loguru |
| Прокси | aiohttp-socks |
| Тесты | pytest >=8 |

Полный список версий — в [`requirements.txt`](requirements.txt) (Python 3.11+).

---

## Возможности

### Аккаунты
- Импорт через Tdata-ZIP с автоматической конвертацией в `.session`.
- Карточка аккаунта: статус, proxy, аватарки, 2FA, теги, дневной лимит, warmup-профиль.
- Редактирование профиля: имя, bio, username, фото (массово или поштучно).
- Группы аккаунтов (many-to-many) с массовым переназначением прокси и редактированием профилей.
- Per-account режим: **AI_ACTIVE** (нейрочат) / **MANUAL** (ручные ответы из веба).
- Авто-проверка прокси и `@SpamBot`-блока, FloodWait-tracking.
- Безопасная загрузка: аккаунты с мёртвым/отсутствующим прокси не подключаются (защита от банов).
- Массовая чистка флота (прокси / аккаунты) с подтверждением.

### Прокси
- SOCKS5 / HTTP через UI бота или веб-панель.
- Группы прокси (для назначения пулом).
- Карточка прокси: маскированный пароль, Exit IP, статус, время последней проверки.
- Быстрая TCP-проверка из веба (без Telegram-handshake).

### Парсинг аудитории
- Сбор каналов, групп и пользователей из Telegram по запросам (`workers/parser/`).
- Пул аккаунтов для парсинга, handлинг FloodWait, фильтры и расширение по глубине.

### Клиенты и классовая система
- Импорт `@username`-листов из TXT с дедупликацией и отчётом.
- **Классы-счётчики** (`new`, `pulse`, `alive`, `accept`, `decline`, `bl`, `stop`, …) — монотонные счётчики, в которые пишут события рассылок и нейрочата.
- **Pulse**: каждое входящее в нейро-контексте после первого касания рассылки.
- **Alive**: окно 60 минут с защитой от двойного учёта при рестартах.
- Гибкий audience filter по классам (включить/исключить, AND/OR) + простой конструктор и текстовый DSL.

### Рассылки
- Шаблоны с плейсхолдерами: `{firstname}`, `{username}`, `{fullname}`, `{date}`, `{time}`, `{datetime}`, `{random4}`, `{link}`.
- Несколько вариантов сообщения на одну кампанию — рандомизация на стороне отправки.
- Тонкие настройки: задержки, дневной лимит, размер пакета, auto-stop через N часов.
- Аудитория: по классам / тест-список TXT / все.
- Ротация нагрузки по всем активным аккаунтам (`messages_per_batch`).
- Управление через очередь `bot_commands` (start/pause/stop из UI).
- Запрет редактирования RUNNING-кампании на уровне API и UI.

### Нейрочат
- Сервисный слой `services/neurochat/*` (manager, dialog, llm, post-actions, engagement).
- LLM через **OpenRouter**; модель и sampling настраиваются.
- Глобальный toggle (БД + `.env`) и per-mailing toggle с иерархией приоритетов.
- System-промпт: глобальный default из `.env` или per-mailing файл, редактор в UI.
- LLM-команды в ответе модели: `[ACCEPT]`, `[DECLINE]`, `[STOP]`, `[SEND_LINK]`, `[HATER]`.
- Зашифрованное хранение `OPENROUTER_API_KEY` (Fernet).
- Логирование причин deny: `global_disabled`, `mailing_local_disabled`, `worker_disconnected`, `client_class_bl`, `client_class_stop`.

### Веб-панель (Control Plane)
FastAPI-бэкенд + SPA (vanilla JS, Tailwind CDN, без npm-сборки).
- **Дашборд**: бизнес-метрики, 24-часовая stacked-bar тайм-серия, распределение классов, топ-аккаунты, последние сообщения.
- **Диалоги**: live-просмотр через SSE, сортировка «как в мессенджере», поиск, ручная отправка из UI.
- **Аккаунты / Группы / Прокси**: CRUD + назначение, TCP-тест.
- **Рассылки**: детальный редактор, параметры рассылки и нейрочата — в отдельных карточках.
- **Клиенты**: фильтры, история взаимодействий, ручное изменение классов.
- **Архив и Cleanup**: мягкое (`archive`) и жёсткое (`hard`) удаление батчами, восстановление из архива.
- **Настройки**: toggle нейрочата, UTC-сдвиг, ключ OpenRouter (с шифрованием), смена пароля.
- **Live**: SSE-стрим новых сообщений в открытый диалог.
- Роли: `super_admin` / `tenant_viewer` (read-only), JWT-авторизация.

### Ручные ответы из веба (без второй сессии)
- Таблица `outbound_queue` + `OutboundConsumer` внутри процесса бота — отправка через ту же Telethon-сессию, что и нейрочат.
- Экспоненциальный бэк-офф (2–32 с + jitter), до 5 попыток; permanent-ошибки — мгновенный `failed`.
- Контекст диалога сохраняется: ручное сообщение пишется в `neuro_chat_messages` как `assistant`.

### Безопасность и эксплуатация
- Доступ к Control Bot — только `OWNER_ID` из `.env`.
- Веб-панель слушает только `127.0.0.1:8081`; доступ через SSH-туннель или nginx + HTTPS.
- JWT для веба, агентский токен для ingest, Fernet для ключа OpenRouter.
- Авто-миграции SQLite при старте. Graceful shutdown воркеров. WAL-режим для параллельного чтения/записи.

---

## Структура репозитория

```
CoreBot/
├── main.py                        Точка входа: логгер, конфиг, БД, запуск бота + воркеров
├── requirements.txt
├── .env.example
├── bot/                           Control Bot (aiogram)
│   ├── handlers/                  /start, аккаунты, прокси, клиенты, рассылка, нейрочат, БД
│   ├── keyboards/                 inline-клавиатуры
│   └── main.py                    Dispatcher, роутеры, сессия
├── workers/                       Telethon-воркеры
│   ├── manager.py                 Worker + WorkerManager (пул, рассылка, проверки)
│   ├── neuro_incoming.py          thin-adapter входящих → services/neurochat/
│   ├── outbound_consumer.py       Очередь ручных отправок из веба
│   ├── bot_command_consumer.py    Очередь команд из веба (start/pause/stop)
│   ├── parser_worker.py           Поллит parsing_tasks, запускает парсинг
│   ├── parser/                    Модуль парсинга (каналы/группы/пользователи)
│   ├── session_converter.py       Tdata → .session
│   └── warmup.py                  Тёплый пул аккаунтов при старте
├── services/                      Бизнес-логика
│   ├── neurochat/                 manager, dialog, llm, post_actions, engagement, …
│   └── database/                  CRM-сервисы (инкременты, accept-транскрипты)
├── database/                      Слой данных (SQLAlchemy 2)
│   ├── models.py                  Все модели: accounts, mailings, clients, neuro_*, archive, …
│   ├── repository.py              engine, авто-миграции, WAL
│   └── repositories.py            CRUD-репозитории
├── control_plane/                 Веб-панель (FastAPI)
│   ├── main.py                    Монтаж панели + routes
│   ├── routes/                    auth, dashboard, business, ingest, admin, …
│   ├── business/                  Бизнес-API (accounts, dialogs, mailings, clients, groups, proxies, instance)
│   └── services/                  alerts, telemetry-агрегаторы
├── web-panel/                     SPA (vanilla JS + Tailwind CDN)
│   ├── index.html
│   ├── main.js                    Hash-роутер, SSE, рендер всех разделов
│   └── styles.css
├── utils/                         Логгер, прокси-чекер, OpenRouter-клиент, Fernet-helper
├── data/                          (не в git) sessions, БД, neuro-промпты, файлы
├── logs/                          (не в git) corebot.log + error.log
├── tests/                         pytest smoke + регрессионные сценарии
├── scripts/                       cb_push.cmd, update_corebot.sh, init_env.sh, cleanup_dialogs.py
└── docs/                          ARCHITECTURE / BACKLOG / PARSER_WORKER / WEBPANEL_DEPLOY / DATABASE_MODULE_SPEC / VPS_UPDATE_GUIDE
```

---

## Как это работает

Бот (`main.py`) и веб-панель (`control_plane/`) работают как два независимых процесса, оба в одном venv, на одном VPS, с SQLite в `data/`.

- Поток данных бота и панели, диаграммы потоков рассылки / нейрочата / ручных ответов — в [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
- Парсинг аудитории: `WorkerManager` поднимает пул Telethon-клиентов; `parser_worker` + `workers/parser/*` выполняют сбор каналов/групп/пользователей по задачам из `parsing_tasks` (см. [`docs/PARSER_WORKER.md`](docs/PARSER_WORKER.md)).
- Веб-панель не дёргает Telegram напрямую: всё управление идёт через `outbound_queue` и `bot_commands` — единая точка управления остаётся внутри процесса бота.

---

## Установка и запуск (локально)

Минимальный Python — **3.11**.

### Быстрый запуск на Windows

После заполнения `.env` запустите двойным кликом:

```bat
start_corebot.bat
```

Скрипт сам создаёт `.venv` при первом запуске, устанавливает зависимости, проверяет конфигурацию, запускает Control Plane и Telegram-бота в отдельных окнах и открывает веб-панель. Дополнительные режимы:

```bat
start_corebot.bat --check
start_corebot.bat --setup
```

### 1. Клонирование и зависимости вручную

```bash
git clone https://github.com/<your-fork>/CoreBot.git
cd CoreBot

python -m venv venv311
# Windows:
venv311\Scripts\activate
# Linux/Mac:
source venv311/bin/activate

pip install -r requirements.txt
```

### 2. Конфиг

```bash
cp .env.example .env
# заполнить минимум: API_ID, API_HASH, BOT_TOKEN, OWNER_ID
```

Откуда брать значения:

| Переменная | Источник |
|------------|----------|
| `API_ID`, `API_HASH` | https://my.telegram.org → API development tools |
| `BOT_TOKEN` | @BotFather → /newbot |
| `OWNER_ID` | @userinfobot |

Всё остальное (пути БД, OpenRouter, прокси, Control Plane) опционально и имеет разумные дефолты.

### 3. Запуск

```bash
python main.py
```

При первом запуске бот сам создаст `data/corebot.db`, выполнит миграции и поднимет воркеры. В Telegram отправьте `/start` с аккаунта владельца.

### 4. Веб-панель (опционально)

В отдельном терминале:

```bash
uvicorn control_plane.main:app --host 127.0.0.1 --port 8081 --reload
```

Откройте `http://127.0.0.1:8081/panel/`, логин/пароль из `CP_BOOTSTRAP_ADMIN_USERNAME` / `CP_BOOTSTRAP_ADMIN_PASSWORD`.

### 5. Тесты

```bash
python -m pytest tests/ -q
```

In-process smoke панели без поднятия uvicorn:

```bash
python -c "from fastapi.testclient import TestClient; from control_plane.main import app; print(TestClient(app).get('/health').json())"
```

### 6. Quality gate (совпадает с `.github/workflows/ci.yml`)

Полный локальный gate — те же команды, что крутит CI на Windows (py3.11)
и Ubuntu (py3.11, py3.12). Failing gate = красный PR. Всё hermetic:
без реальных Telegram/OpenRouter-вызовов и без секретов
(`.env`/`data/` в git не коммитятся; для BAT-проверки `.env`
синтезируется из `.env.example`).

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests/ -q
python -m compileall -q bot control_plane database workers services utils tools scripts tests main.py migrate_mailings.py
python -m ruff check .
python -m ruff format --check bot/handlers/accounts/tdata_check.py control_plane/business/tdata_check_routes.py control_plane/services/heartbeat.py control_plane/services/sanitize.py control_plane/services/snapshot.py control_plane/services/watchdog.py control_plane/version.py database/sqlite_pragmas.py scripts/backup_lib.py scripts/make_release.py scripts/release_lib.py services/tdata_check/__init__.py services/tdata_check/checker.py services/tdata_check/lease.py services/tdata_check/limits.py services/tdata_check/models.py services/tdata_check/zip_safety.py tests/test_backup_restore.py tests/test_config_validation.py tests/test_fleet_automation.py tests/test_integration_flows.py tests/test_lifespan_bootstrap.py tests/test_load_acceptance.py tests/test_observability.py tests/test_release_workflow.py tests/test_sqlite_reliability.py tests/test_tdata_check.py tests/test_utc_time_model.py tools/__init__.py tools/instance_status.py tools/validate_config.py tools/validate_skill.py utils/time.py
node --check web-panel/main.js
for f in scripts/*.sh skills/corebot-vps-deploy/scripts/*.sh; do bash -n "$f" || exit 1; done
python -m tools.validate_skill
python -m tools.validate_config --mode local --env-file .env.example
```

`ruff format --check` покрывает только curated-список файлов задач 02–12
(см. шапку `pyproject.toml`): legacy-код покрыт `ruff check` + `compileall`,
массовый reformat legacy в скоуп задачи 10 не входит. Конфиг pytest —
только `pytest.ini` (в `pyproject.toml` его дубликата нет осознанно).

Только Windows (BAT-гейт с временным env без секретов):

```bat
copy /Y .env.example .env
start_corebot.bat --check
```

---

## Переменные окружения

Все переменные задаются в `.env` (шаблон — [`.env.example`](.env.example)). Значения в репозиторий не коммитятся.

| Переменная | Обязательность | Назначение |
|------------|----------------|------------|
| `API_ID` / `API_HASH` | обязательные | Telegram API app (my.telegram.org) |
| `BOT_TOKEN` | обязательная | Токен Control Bot (BotFather) |
| `OWNER_ID` | обязательная | Telegram ID владельца (админ-доступ) |
| `DATABASE_URL` | нет | SQLite-URL основной БД бота |
| `LOG_LEVEL` | нет | Уровень логирования (по умолчанию INFO) |
| `CONTROL_BOT_PROXY_*` | нет | Прокси для Control Bot (тип/хост/порт/логин/пароль) |
| `CP_AGENT_ENABLED` / `CP_INGEST_URL` / `CP_AGENT_TOKEN` | нет | Агентская телеметрия бот → панель |
| `CP_DATABASE_URL` | нет | SQLite-URL БД веб-панели |
| `CP_JWT_SECRET` | нет | Секрет JWT веб-панели (задать длинный случайный) |
| `CP_BOOTSTRAP_ADMIN_USERNAME` / `CP_BOOTSTRAP_ADMIN_PASSWORD` | нет | Стартовый админ панели |
| `BOT_DATABASE_URL` | нет | Sync-URL до основной БД (для панели) |
| `CP_BUSINESS_STREAM_INTERVAL` / `CP_BUSINESS_STREAM_BATCH` | нет | Параметры SSE-стрима |
| `OPENROUTER_API_KEY` | нет | Ключ OpenRouter (можно задать в UI панели) |
| `OPENROUTER_KEY_ENCRYPTION_KEY` | нет | Fernet-ключ для шифрования OpenRouter-ключа |
| `OPENROUTER_BASE_URL` / `OPENROUTER_HTTP_REFERER` | нет | Endpoint OpenRouter |
| `NEUROCHAT_ENABLED` | нет | Глобальный toggle нейрочата |
| `DEFAULT_NEURO_MODEL` / `NEURO_DEFAULT_*` | нет | Дефолтные модель и sampling-параметры нейрочата |
| `MAILING_BASE_UTC_OFFSET` | нет | UTC-сдвиг для плейсхолдеров `{date}`/`{time}`/`{datetime}` |
| `PARSER_EMBEDDED` / `PARSER_POLL_SEC` | нет | Встроенный парсинг вместе с Control Plane |

---

## Сборка и деплой на VPS

Проект предназначен для self-hosted-деплоя на один VPS (Ubuntu 22.04/24.04) с systemd. Полный пошаговый гайд — [`RUNBOOK.md`](RUNBOOK.md).

Для автоматизированной установки также доступен Codex-skill [`skills/corebot-vps-deploy`](skills/corebot-vps-deploy). Его локальная установленная копия вызывается как `$corebot-vps-deploy`.

Коротко:

1. Ubuntu 22.04/24.04, отдельный пользователь `corebot`, venv и код в `/opt/corebot/`.
2. Два systemd-юнита: `corebot.service` (бот) и `corebot-cp.service` (панель), оба через `EnvironmentFile=/opt/corebot/app/.env`.
3. Панель слушает только `127.0.0.1:8081`; доступ через `ssh -L 8081:127.0.0.1:8081 root@<VPS_IP>` или nginx + HTTPS (см. [`docs/WEBPANEL_DEPLOY.md`](docs/WEBPANEL_DEPLOY.md)).
4. Обновление: локально `scripts\cb_push.cmd "msg"`, на VPS — `scripts/update_corebot.sh` (подробности — [`docs/VPS_UPDATE_GUIDE.md`](docs/VPS_UPDATE_GUIDE.md)).

Без сборщика/бандлера: веб-панель раздаётся статикой из самой FastAPI-панели, прод-сборки фронта нет.

---

## Ограничения и известные проблемы

- **Парсинг** — встроенный loop запускается вместе с Control Plane (`PARSER_EMBEDDED`); отдельный процесс — `python -m workers.parser_worker`. Оба варианта опциональны.
- **Backlog**: авто-cleanup по расписанию (`instance_settings.cleanup_cron`) и импорт листов из веба — ещё не реализованы; полный список — [`docs/BACKLOG.md`](docs/BACKLOG.md).
- **Accept-транскрипты**: Telethon-выгрузка переписки на accept отмечена TODO (не подключена к UI).
- Единый `parse_mode=HTML` во всех ответах бота — пока местами Markdown.

---

## Документация

| Файл | Когда читать |
|------|-------------|
| [`HELP.md`](HELP.md) | Шпаргалка команд на каждый день |
| [`RUNBOOK.md`](RUNBOOK.md) | Деплой на новый VPS с нуля |
| [`docs/VPS_UPDATE_GUIDE.md`](docs/VPS_UPDATE_GUIDE.md) | Обновление прод-инстанса (детали, ветки, fallback) |
| [`docs/WEBPANEL_DEPLOY.md`](docs/WEBPANEL_DEPLOY.md) | Подключение веб-панели, nginx + HTTPS |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Карта компонентов и потоков данных |
| [`docs/PARSER_WORKER.md`](docs/PARSER_WORKER.md) | Модуль парсинга аудитории |
| [`docs/DATABASE_MODULE_SPEC.md`](docs/DATABASE_MODULE_SPEC.md) | Спецификация классовой системы и импорта листов |
| [`docs/BACKLOG.md`](docs/BACKLOG.md) | Что ещё не сделано / приоритеты |
| [`corebot v2.md`](corebot%20v2.md) | Журнал работ и ближайшая дорожная карта |

---

## Безопасность и легальность

- Доступ к боту — только у `OWNER_ID` из `.env`.
- Веб-панель не выставляется в интернет без HTTPS и nginx (см. [`docs/WEBPANEL_DEPLOY.md`](docs/WEBPANEL_DEPLOY.md)).
- `.env`, `data/sessions/`, `data/corebot.db`, `data/control_plane.db` — никогда не коммитьте.
- Используйте проект ответственно и в соответствии с [Telegram Terms of Service](https://telegram.org/tos). Авторы не несут ответственности за нарушение правил Telegram, спам или иное использование, противоречащее ToS платформы и местному законодательству.
