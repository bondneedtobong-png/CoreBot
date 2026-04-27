# CoreBot

Управление сетью userbot-аккаунтов Telegram: рассылки, нейрочат, ручные ответы и аналитика. Управляющий бот в Telegram + веб-панель для оператора. Без облаков и сторонних SaaS — всё крутится в одном venv на одном VPS.

> Status: Active development · Python 3.11+ · SQLite · self-hosted

---

## Что это

CoreBot — двухкомпонентная система:

1. **Control Bot** (aiogram 3) — Telegram-бот владельца. Через него управляются аккаунты, прокси, клиенты, рассылки и нейрочат.
2. **Worker accounts** (Telethon) — userbot-аккаунты, которые физически шлют сообщения, ведут диалоги и отвечают LLM-ответами.

Поверх этого — **Control Plane** (FastAPI + SPA) с реал-тайм-просмотром диалогов, ручными ответами, CRUD-редакторами рассылок/аккаунтов/прокси и встроенной телеметрией.

---

## Возможности

### Аккаунты

- Импорт через Tdata-ZIP с автоматической конвертацией в `.session`.
- Карточка аккаунта: статус, прокси, аватарки, 2FA, теги, дневной лимит, warmup-профиль.
- Редактирование профиля: имя, bio, username, фото (массово или поштучно).
- Группы аккаунтов (many-to-many) с массовым переназначением прокси и редактированием профилей.
- Per-account режим работы: **AI_ACTIVE** (нейрочат) / **MANUAL** (ручные ответы из веба).
- Авто-проверка прокси и `@SpamBot`-блока, FloodWait-tracking.

### Прокси

- SOCKS5 / HTTP через UI бота или веб-панель.
- Группы прокси (для назначения пулом).
- Карточка прокси: маскированный пароль, Exit IP, статус, время последней проверки.
- Быстрая TCP-проверка из веба (без Telegram-handshake).

### Клиенты и классовая система

- Импорт `@username`-листов из TXT с дедупликацией и отчётом.
- **Классы-счётчики** (`new`, `pulse`, `alive`, `accept`, `decline`, `bl`, `stop`, …) — монотонные счётчики, в которые пишут события из рассылок и нейрочата.
- **Pulse**: каждое входящее в нейро-контексте, после первого касания рассылки.
- **Alive**: окно 60 минут с защитой от двойного учёта при рестартах (таблица `client_alive_windows`).
- Гибкий audience filter рассылки по классам (включить/исключить, AND/OR).
- Поиск в двух режимах: простой конструктор и текстовый DSL (`{class{subclass}}`, `>=`, `and`, `or`, `not`).

### Рассылки

- Шаблоны сообщений с плейсхолдерами: `{firstname}`, `{username}`, `{fullname}`, `{date}`, `{time}`, `{datetime}`, `{random4}`, `{link}`.
- Несколько вариантов сообщения (по одному в строке) — рандомизация на стороне отправки.
- Тонкие настройки: задержки между сообщениями/аккаунтами/пакетами, дневной лимит, размер пакета, auto-stop через N часов.
- Аудитория: `classes` (по фильтру), `test` (TXT-список без подмены), `all`.
- Ротация по `messages_per_batch` — нагрузка размазывается по всем активным аккаунтам.
- Управление через очередь `bot_commands` (UI-команды `start/pause/stop` ставятся в очередь, исполнение остаётся внутри процесса бота).
- Запрет редактирования RUNNING-кампании на уровне API и UI.

### Нейрочат

- Отдельный сервисный слой `services/neurochat/*` (manager, dialog, llm, post-actions, engagement).
- LLM через **OpenRouter** (`openai/gpt-oss-120b:free` по умолчанию, любая совместимая модель).
- Глобальный toggle (БД + `.env: NEUROCHAT_ENABLED`) и per-mailing toggle, с приоритетом `global → mailing local → account/filter`.
- System-промпт: глобальный default из `.env` или per-mailing файл (`data/neuro/mailings/{id}/system.txt`), редактор в UI с кнопкой «Сбросить к DEFAULT».
- Sampling overrides через JSON в карточке рассылки (temperature/top_p/top_k/max_tokens/…).
- LLM-команды в ответе модели: `[ACCEPT]`, `[DECLINE]`, `[STOP]`, `[SEND_LINK]`, `[HATER]` — каждая увеличивает соответствующий класс и пишет событие в `interactions`.
- Зашифрованное хранение `OPENROUTER_API_KEY` в БД (Fernet через `OPENROUTER_KEY_ENCRYPTION_KEY`).
- Подробное логирование причин deny: `global_disabled`, `mailing_local_disabled`, `worker_disconnected`, `client_class_bl`, `client_class_stop`.

### Веб-панель (Control Plane)

FastAPI-бэкенд + SPA на ванильном JS + Tailwind CDN (без сборщика, без зависимостей по npm).

- **Дашборд**: бизнес-метрики (аккаунты по статусам, диалоги, очередь ручных, входящие/исходящие/ручные за 24 ч, активные рассылки с прогрессом), 24-часовая stacked-bar тайм-серия, распределение клиентских классов, топ-аккаунты, last-20 сообщений.
- **Диалоги**: live-просмотр через SSE. Список аккаунтов слева отсортирован «как в мессенджере» (по последнему сообщению), с поиском по `list_label` и второй строкой — Telegram-имя/`@username`. Лента переписки справа, ручная отправка прямо из UI.
- **Аккаунты**: таблица + редактор (имя, bio, теги, прокси, группы, лимит, warmup, статус/membership), безопасное удаление с каскадной очисткой зависимых таблиц.
- **Группы / Прокси**: CRUD + назначение, TCP-тест прокси.
- **Рассылки**: список + детальный редактор. Параметры рассылки (текст, варианты, задержки, аудитория) и нейрочат (toggle, модель, sampling, system-промпт) — в **двух разных карточках**.
- **Клиенты**: фильтр по `q/class_key/status`, история взаимодействий, ручное изменение классов.
- **Архив и Cleanup**: безопасное массовое удаление переписок (`mode=archive` пишет в `*_archive` таблицы, `mode=hard` удаляет; всё батчами, с `dry_run`). Восстановление из архива.
- **Логи**: фильтр по уровню, постраничный просмотр.
- **Настройки**: глобальный toggle нейрочата, базовый UTC-сдвиг, ключ OpenRouter (с шифрованием и маской), смена пароля админа.
- **Live**: SSE `/business/stream?token=...` стримит новые сообщения в открытый диалог.

### Ручные ответы из веба (без второй сессии)

- Отдельная таблица `outbound_queue` + `OutboundConsumer` (worker внутри процесса бота), который шлёт через **тот же Telethon-клиент**, что и нейрочат — Telegram не видит «второй сессии».
- Экспоненциальный бэк-офф (2–32 c +jitter) до 5 попыток на transient-ошибки.
- Permanent-ошибки (`USER_DEACTIVATED`, `PEER_ID_INVALID`) — мгновенный `failed`.
- После успешной отправки сообщение пишется в `neuro_chat_messages` (`role='assistant'`), поэтому при возврате аккаунта в AI_ACTIVE нейрочат продолжает диалог без потери контекста.
- Ручные сообщения видны в ленте диалога с status-pill (`pending` / `sending` / `failed` / `cancelled`) и кнопками «Повторить» / «Отменить».

### Безопасность и эксплуатация

- Доступ к Control Bot — только `OWNER_ID` из `.env`.
- Веб-панель слушает только `127.0.0.1:8081`. Доступ через SSH-туннель или nginx + Let's Encrypt.
- JWT для веба, агентский токен для ingest, Fernet-шифрование для ключа OpenRouter.
- Авто-миграции SQLite при старте (`CREATE TABLE IF NOT EXISTS` + `ALTER TABLE … ADD COLUMN`). Никаких ручных миграций.
- Graceful shutdown: остановка активной рассылки и `disconnect_all()` всех воркеров перед выходом.
- WAL-режим SQLite — параллельное чтение/запись из бота и панели в один файл.

---

## Технологии

| Слой | Стек |
|------|------|
| Control Bot | aiogram 3.x, aiohttp |
| Workers | Telethon, tgconvertor |
| LLM | OpenRouter (любая модель) |
| Web backend | FastAPI, uvicorn, Pydantic v2, PyJWT, passlib[bcrypt], cryptography |
| Web frontend | Vanilla JS (hash router), Tailwind CDN, Alpine.js, EventSource (SSE) |
| ORM | SQLAlchemy 2.x (async + sync) |
| Storage | SQLite (aiosqlite) в WAL-режиме |
| Логирование | loguru |
| Прокси | aiohttp-socks |

Минимальный Python — **3.11**, проверено также на 3.12–3.14.

---

## Архитектура (упрощённо)

```
        Telegram
           │
   ┌───────┴───────┐
   ▼               ▼
Control Bot   Worker Accounts (Telethon)
(aiogram 3)        │
   │   ▲           │
   ▼   │           ▼
   └─► SQLite ◄──── OutboundConsumer / NeuroIncoming
       (data/corebot.db, WAL)
            ▲
            │  sync engine (BOT_DATABASE_URL)
            ▼
       Control Plane
       (FastAPI + SPA)
            │
       ssh -L 8081 ──► Browser
```

- Бот и панель работают в **разных systemd-юнитах** (`corebot.service` / `corebot-cp.service`), оба внутри одного venv.
- БД бота (`corebot.db`) и БД панели (`control_plane.db`) — разные файлы.
- Бот не дёргает Telegram из панели напрямую: всё проходит через `outbound_queue` и `bot_commands` — единая точка управления остаётся внутри процесса бота.

Подробности — в `docs/ARCHITECTURE.md` и `docs/WEBPANEL_DEPLOY.md`.

---

## Структура репозитория

```
CoreBot/
├── main.py                        Точка входа: логгер, конфиг, БД, запуск бота + воркеров
├── requirements.txt
├── .env.example
│
├── bot/                           Control Bot (aiogram)
│   ├── handlers/                  /start, аккаунты, прокси, клиенты, рассылка, нейрочат, БД
│   ├── keyboards/                 inline-клавиатуры
│   └── main.py                    Dispatcher, роутеры, сессия
│
├── workers/                       Telethon-воркеры
│   ├── manager.py                 Worker + WorkerManager (пул, рассылка, проверки)
│   ├── neuro_incoming.py          thin-adapter входящих → services/neurochat/
│   ├── outbound_consumer.py       Очередь ручных отправок из веба
│   ├── bot_command_consumer.py    Очередь команд из веба (start/pause/stop рассылки)
│   └── session_converter.py       Tdata → .session
│
├── services/                      Бизнес-логика
│   ├── neurochat/                 manager, dialog, llm, post_actions, engagement, …
│   └── database/                  CRM-сервисы (incrementы, accept-транскрипты)
│
├── database/                      Слой данных (SQLAlchemy 2)
│   ├── models.py                  Все модели: accounts, mailings, clients, neuro_*, archive, …
│   ├── repository.py              engine, авто-миграции, WAL
│   └── repositories.py            CRUD-репозитории
│
├── control_plane/                 Веб-панель (FastAPI)
│   ├── main.py                    Монтаж панели + routes
│   ├── routes/                    auth, dashboard, business, ingest, admin, …
│   ├── business/                  Бизнес-API (accounts, dialogs, mailings, clients, groups, proxies, instance)
│   └── services/                  alerts, telemetry-агрегаторы
│
├── web-panel/                     SPA (vanilla JS + Tailwind CDN)
│   ├── index.html
│   ├── main.js                    Hash-router, SSE, рендер всех разделов
│   └── styles.css
│
├── utils/                         Логгер, прокси-чекер, OpenRouter-клиент, Fernet-helper
│
├── data/                          (не в git) sessions, БД, neuro-промпты, файлы
├── logs/                          (не в git) corebot.log + error.log
├── tests/                         pytest smoke + бэклог-фиксы
├── scripts/                       cb_push.cmd, update_corebot.sh, init_env.sh, cleanup_dialogs.py
└── docs/                          ARCHITECTURE / BACKLOG / WEBPANEL_DEPLOY / DATABASE_MODULE_SPEC / VPS_UPDATE_GUIDE
```

---

## Быстрый старт (локально)

### 1. Клонирование и зависимости

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

При первом запуске бот сам создаст `data/corebot.db`, выполнит миграции и подключит все добавленные аккаунты. В Telegram — отправь `/start` владельцем.

### 4. Веб-панель (опционально)

В отдельном терминале:

```bash
uvicorn control_plane.main:app --host 127.0.0.1 --port 8081 --reload
```

Открой `http://127.0.0.1:8081/panel/`, логин/пароль из `CP_BOOTSTRAP_ADMIN_USERNAME` / `CP_BOOTSTRAP_ADMIN_PASSWORD`.

---

## Прод-деплой на VPS

Полный пошаговый гайд для нового сервера — `RUNBOOK.md`.

Короткое резюме:

1. Ubuntu 22.04/24.04, Python 3.11+, отдельный пользователь `corebot`, venv в `/opt/corebot/venv`, код в `/opt/corebot/app`.
2. Два systemd-юнита: `corebot.service` (бот) и `corebot-cp.service` (панель), оба читают `/opt/corebot/app/.env`, оба `User=corebot`.
3. Панель слушает только `127.0.0.1:8081`. Доступ через `ssh -L 8081:127.0.0.1:8081 root@<VPS_IP>` или nginx+HTTPS (см. `docs/WEBPANEL_DEPLOY.md` §7).
4. Обновление: локально `scripts\cb_push.cmd "msg"` → на VPS `cb-update`. Подробно — `HELP.md` §1 и `docs/VPS_UPDATE_GUIDE.md`.

---

## Документация

| Файл | Когда читать |
|------|-------------|
| `HELP.md` | Шпаргалка по командам на каждый день |
| `RUNBOOK.md` | Деплой на новый VPS с нуля |
| `docs/VPS_UPDATE_GUIDE.md` | Обновление прод-инстанса (детали, ветки, fallback) |
| `docs/WEBPANEL_DEPLOY.md` | Подключение веб-панели к существующему боту, nginx+HTTPS |
| `docs/ARCHITECTURE.md` | Карта компонентов и потоков данных |
| `docs/DATABASE_MODULE_SPEC.md` | Спецификация классовой системы и импорта листов |
| `docs/BACKLOG.md` | Что ещё не сделано / приоритеты |
| `corebot v2.md` | Журнал работ и ближайшая дорожная карта |

---

## Тесты

```bash
python -m pytest tests/ -q
```

Smoke-сценарии в боте — раздел 2 в `corebot v2.md`. Sanity-check фронта:

```bash
node -c web-panel/main.js
```

In-process smoke панели без поднятия uvicorn:

```bash
python -c "from fastapi.testclient import TestClient; from control_plane.main import app; print(TestClient(app).get('/health').json())"
```

---

## Безопасность и легальность

- Доступ к боту только у `OWNER_ID` из `.env`.
- Веб-панель не выставляется в интернет без HTTPS и nginx (см. `docs/WEBPANEL_DEPLOY.md` §1).
- `.env`, `data/sessions/`, `data/corebot.db`, `data/control_plane.db` — никогда не коммитить.
- Используйте проект ответственно и в соответствии с [Telegram Terms of Service](https://telegram.org/tos). Авторы не несут ответственности за нарушение правил Telegram, спам или иное использование, противоречащее ToS платформы и местному законодательству.

---

## Лицензия

MIT.
