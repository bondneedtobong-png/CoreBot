# Архитектура CoreBot

Обновлено: апрель 2026.

## Назначение

**CoreBot** — система массовой рассылки в Telegram через userbot-аккаунты (Telethon), с панелью управления внутри Telegram-бота (aiogram 3) и опциональной веб-панелью (FastAPI + SPA).

## Карта компонентов

```
        Telegram
           │
   ┌───────┴───────┐
   ▼               ▼
Control Bot   Worker accounts (Telethon)
(aiogram 3)        ▲
   │               │
   ▼               │
   └─► SQLite ◄────┘
       (data/corebot.db, WAL)
            ▲
            │  sync engine (BOT_DATABASE_URL)
            ▼
       Control Plane
       (FastAPI + SPA)
            ▲
       ssh -L 8081 ──► Browser
```

| Компонент | Роль | Точка входа |
|-----------|------|-------------|
| Control Bot | Telegram-бот владельца, управление | `main.py` → `bot/main.py` |
| Workers | Пул Telethon-клиентов: рассылка, нейрочат, ручные ответы | `workers/manager.py` |
| OutboundConsumer | Поллит `outbound_queue`, шлёт ручные ответы из веба | `workers/outbound_consumer.py` |
| ParserWorker | Поллит `parsing_tasks`, Telethon-парсинг каналов/групп/пользователей → `parsed_*` | `python -m workers.parser_worker` |
| BotCommandConsumer | Поллит `bot_commands`, исполняет start/pause/stop рассылки из веба | `workers/bot_command_consumer.py` |
| NeuroIncoming | Thin-adapter входящих → `services/neurochat/*` | `workers/neuro_incoming.py` |
| Services / neurochat | Сервисный слой нейрочата | `services/neurochat/` |
| Services / database | CRM-сервисы (классы-счётчики, accept-транскрипты) | `services/database/` |
| Database | SQLAlchemy 2 (async + sync), авто-миграции | `database/` |
| Control Plane | FastAPI: auth, ingest, dashboard, business API, SSE | `control_plane/` |
| Web panel SPA | Hash-router, SSE, vanilla JS | `web-panel/` |
| Utils | Логгер, прокси-чекер, OpenRouter-клиент, Fernet | `utils/` |

## Поток данных: рассылка

```
Owner → Control Bot → SQLite (mailings, mailing_logs)
                ↓
        WorkerManager.start_mailing()
                ↓
   ротация по messages_per_batch
                ↓
   Worker.send_to(client, text)  →  Telegram API
                ↓
        mailing_logs(success/error)
                ↓
   опц.: NeuroIncoming запускается на ответ клиента
```

Ключевые ограничения вшиты в саму рассылку: `delay_between_messages`, `delay_between_accounts`, `messages_per_batch`, `batch_delay`, `daily_limit`, `auto_stop_hours`, `audience_mode` (`classes`/`test`/`all`).

## Поток данных: нейрочат

```
Telegram → Worker → NeuroIncoming(thin)
                       ↓
        services/neurochat/manager
        ┌───────┬───────┬───────┐
        ▼       ▼       ▼       ▼
       gate  history  prompt  sampling
        │       │       │       │
        └───────┴───┬───┴───────┘
                    ▼
        services/neurochat/llm_service  →  OpenRouter
                    ↓
        services/neurochat/post_actions
        ┌──────────────┬───────────────┐
        ▼              ▼               ▼
   send_text     [SEND_LINK]      [STOP]/[ACCEPT]/...
                                  ↓
                            class_bridge → user_class_counters
```

`gate` проверяет в строгой иерархии:
1. **Global** — `instance_settings.neurochat_enabled` или `.env: NEUROCHAT_ENABLED`.
2. **Mailing local** — `mailings.neurochat_enabled` для активной кампании клиента.
3. **Account / filter** — статус аккаунта + классы клиента (`bl`, `stop`).

Каждый отказ пишется в лог с `reason`-кодом: `global_disabled`, `mailing_local_disabled`, `worker_disconnected`, `client_class_bl`, `client_class_stop`.

## Поток данных: ручной ответ из веба

```
Browser (SPA) → POST /business/accounts/{id}/dialogs/{peer}/send
              → INSERT outbound_queue(status='pending', requested_by=admin)
                                  ↓
         OutboundConsumer (внутри процесса бота, опрос ~1.5 c)
                                  ↓
         _ensure_worker (lazy connect/load_accounts при необходимости)
                                  ↓
         Worker.send_to(...) — ТА ЖЕ Telethon-сессия, что и нейрочат
                                  ↓
   neuro_chat_messages(role='assistant')  ← контекст не теряется
                                  ↓
         SSE /business/stream → клиент видит сообщение в ленте
```

При transient-ошибке: экспоненциальный бэк-офф 2–32 c с jitter, до 5 попыток. На permanent-ошибки (`USER_DEACTIVATED`, `PEER_ID_INVALID`, …) — мгновенно `failed`.

## Пакет `bot/handlers/accounts/`

Монолитный `accounts.py` заменён пакетом с разделением по сценариям:

| Файл | Ответственность |
|------|-----------------|
| `common.py` | `safe_edit_message`, текст/HTML карточки аккаунта |
| `states.py` | Все `StatesGroup` для аккаунтов |
| `tdata.py` | Загрузка ZIP, выбор прокси, конвертация |
| `list_card.py` | Список аккаунтов, `account_view_*`, перепроверка авторизации |
| `profile.py` | Имя, bio, username |
| `photos.py` | Управление фото профиля userbot |
| `twofa.py` | Установка 2FA |
| `membership_delete.py` | Membership, подтверждение и удаление аккаунта |
| `groups.py` | Группы аккаунтов + массовые проверки и редактирование профилей |
| `proxy_assign.py` | Смена прокси аккаунта |
| `tags.py` | Теги аккаунта |
| `cancel.py` | Callback `cancel_accounts` |
| `__init__.py` | Сборка единого `router` через `include_router` |

Импорт для диспетчера не меняется: `from bot.handlers.accounts import router`.

## Сервисный слой нейрочата `services/neurochat/`

| Модуль | Что делает |
|--------|-----------|
| `manager.py` | `prepare_incoming_context(...)`: gate + prompt + history + sampling + ключ/модель |
| `incoming_service.py` | Оркестрация входящих: вызов LLM, retry/fallback, диспатч post-actions |
| `config_service.py` | Чтение global toggle и эффективных настроек (БД → ENV) |
| `dialog_service.py` | История диалога в едином формате |
| `filters.py` | Account/filter гейт (классы клиента, BL/ignore) |
| `llm_service.py` | Запросы к OpenRouter, retry/fallback моделей |
| `commands.py` | Парсер LLM-команд (`[STOP]`, `[ACCEPT]`, `[SEND_LINK]`, …) |
| `post_actions.py` | Use-cases после LLM: `send_text_reply`, `process_send_link_command`, `process_stop_command`, `persist_dialog_turn` |
| `class_bridge.py` | Мост к `user_class_counters` (классовая система) |
| `engagement_service.py` | `pulse` (на каждое входящее) и `alive` (с окном 60 мин) |
| `stats_service.py` | Чтение neuro-action счётчиков (без прямого доступа из mailing) |
| `admin_service.py` | API для UI бота: prompt/sampling/actions |

## Веб-панель: `control_plane/`

```
control_plane/
├── main.py           Регистрация роутеров, mount /panel/, миграции
├── config.py         Pydantic settings (CP_*, BOT_DATABASE_URL)
├── database.py       Async engine для control_plane.db
├── models.py         User, Tenant, Agent, Token, Event, Metric, Alert, AuditLog
├── auth.py           Bcrypt + JWT (PyJWT)
├── deps.py           get_current_user, get_db, get_bot_db
├── routes/
│   ├── auth.py       /auth/login, /auth/refresh
│   ├── ingest.py     /ingest/batch (агентский токен)
│   ├── dashboard.py  /dashboard/{summary,agents,logs,alerts}
│   ├── admin.py      /admin/agents, /admin/users, /admin/tokens
│   └── business.py   /business/accounts/* (CRUD + dialogs + send + cleanup)
├── business/
│   ├── db.py         Sync engine к corebot.db
│   ├── schemas.py    Все Pydantic-схемы /business/*
│   ├── instance.py   /business/instance/* (settings, openrouter-key)
│   ├── groups.py     /business/groups/* (CRUD + many-to-many)
│   ├── proxies.py    /business/proxies/* (CRUD + TCP test)
│   ├── mailings.py   /business/mailings/* (CRUD + prompt + start/pause/stop)
│   ├── clients.py    /business/clients/* (CRUD + class change)
│   ├── dashboard.py  /business/dashboard (бизнес-метрики)
│   └── archive.py    /business/cleanup/v2 + /business/archive/*
└── services/
    ├── alerts.py     Alert engine + Telegram-нотификации
    └── retention.py  Ретенция логов
```

SPA в `web-panel/` (vanilla JS) общается с этим API + EventSource на `/business/stream?token=…`.

## Файлы данных

- `data/corebot.db` — основная БД бота (WAL).
- `data/control_plane.db` — БД веб-панели (пользователи, агенты, audit log, ingest events).
- `data/sessions/*.session` — Telethon-сессии аккаунтов.
- `data/neuro/mailings/{id}/system.txt` — кастомные system-промпты (опционально).
- `data/files/avatars/` — временные файлы при загрузке аватарок через бота.
- `data/tdata_temp/` — распаковка ZIP при импорте.
- `logs/corebot.log`, `logs/error.log` — файловые логи loguru.

## Связанные документы

- Спецификация классовой системы и импорта листов: [DATABASE_MODULE_SPEC.md](DATABASE_MODULE_SPEC.md)
- Подключение веб-панели к боту: [WEBPANEL_DEPLOY.md](WEBPANEL_DEPLOY.md)
- Бэклог и долги: [BACKLOG.md](BACKLOG.md)
- Шпаргалка команд: [../HELP.md](../HELP.md)
