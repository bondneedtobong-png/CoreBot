# Бэклог и долги по коду

Актуально на апрель 2026. Сжатый список, без архивных деталей. Полная история — в `corebot v2.md`.

---

## Сделано (за последние итерации)

### Бот / архитектура

- Раздельные callback-конфликты: `cancel_accounts` / `cancel_mailing` / `cancel_proxy` + legacy `cancel`.
- Удаление аккаунта: финальный коллбэк по `^account_delete_\d+$`, корректная работа при отсутствии записи.
- Единый контекст БД: `database.session.session_scope()` (rollback при исключении), миграция handlers + `workers/manager.py`.
- Рефакторинг `bot/handlers/accounts/*` (пакет вместо одного файла).
- Рассылка: ротация по `messages_per_batch`, `mailing.use_typing`, пагинация в длинных списках.
- Graceful shutdown: stop активной рассылки + `disconnect_all()` всех Telethon-клиентов.
- Сервисный слой нейрочата `services/neurochat/*` (manager, dialog, llm, post-actions, engagement, …).
- Иерархия флагов нейрочата: `global → mailing local → account/filter` с reason-кодами в логах.
- Классовая система: `pulse` / `alive` (окно 60 мин с `client_alive_windows`), `[STOP] → класс stop`.

### Веб-панель

- Бэкенд `control_plane/` (FastAPI), бизнес-API `/business/*` (54 роута) поверх `corebot.db` через sync-engine.
- SPA `web-panel/`: hash-router, sidebar, разделы Dashboard / Accounts / Dialogs / Mailings / Clients / Groups / Proxies / Logs / Archive / Settings.
- AI_ACTIVE ↔ MANUAL переключатель аккаунта; `OutboundConsumer` с lazy reconnect и экспоненциальным бэк-оффом.
- Очередь команд `bot_commands` + `BotCommandConsumer` (start/pause/stop рассылки из веба, без прямого Telethon из панели).
- `BotCommandConsumer` + `OutboundConsumer` запускаются вместе с ботом, воркеры подключаются `connect_all()` сразу при старте — ручные отправки работают мгновенно.
- Soft-delete через `*_archive` таблицы + `POST /business/cleanup/v2 { mode: 'archive'|'hard' }`.
- SSE `GET /business/stream?token=...` — live-сообщения нейрочата.
- Бизнес-дашборд (KPI + 24-часовой stacked-bar + классовые бары + топ-аккаунты + live-feed).
- Редактор рассылки в веб-панели: разделён на «Настройки рассылки» и «Настройки нейрочата» (включая system-промпт).
- Диалоги: сортировка аккаунтов по последнему сообщению, поиск по `list_label`, второй строкой — Telegram-имя/`@username`.
- CRUD из UI: аккаунты (включая теги/прокси/группы/лимиты), группы, прокси (с TCP-тестом), глобальные настройки инстанса, ключ OpenRouter (Fernet).
- Тесты: pytest 10/10, in-process FastAPI smoke на ключевые роуты.

---

## Высокий приоритет

1. **Создание сущностей из веба** (сейчас можно только редактировать существующие):
   - новая рассылка с пресетом (название, аудитория, шаблон сообщения);
   - новый аккаунт через UI (минимально — манипуляция метаданными; импорт Tdata пока только в боте).
2. **Логи OpenRouter отдельным каналом** — сейчас всё валится в общий лог. Нужна выделенная вкладка в UI и фильтр по `provider/model/prompt_id`.
3. **Управление операторами**: смена пароля админа из UI; пригласить оператора с ограниченными правами (read-only / no-cleanup / no-secrets).
4. **Расширение pytest** — переход от smoke к регрессионным сценариям (рассылка, импорт листов, группы, прокси, нейрочат, outbound queue).

## Средний приоритет

5. **Авто-cleanup по расписанию** — в `instance_settings` добавить `cleanup_cron`, исполнять из `BotCommandConsumer` или отдельного `RetentionWorker`.
6. **Полноценная страница «Очередь»** в веб-панели (отдельно от диалогов): фильтры по `status/account/peer`, групповой retry/cancel, экспорт.
7. **Мини-конструктор фильтра аудитории** в карточке рассылки UI (сейчас в боте — простой режим, в вебе пока только выбор группы и `audience_mode`).
8. **Сортировка/поиск в основном списке аккаунтов** (`#/accounts`) — сейчас остаётся `order_by(id)`.
9. **Поиск в двух режимах для клиентов** (простой конструктор + DSL) — спецификация в `DATABASE_MODULE_SPEC.md` §8, в UI пока только `q/class_key/status`.
10. **Импорт листов 211/212 из веб-панели** с отчётом и подтверждением (сейчас только в боте).

## Низкий приоритет / идеи

- Кэш или фоновое обновление `photo_count` на карточке аккаунта после операций с аватарками.
- Единый `parse_mode=ParseMode.HTML` во всех ответах бота.
- Перевод фронта на Vite/React, если экранов станет больше или появится state-management (сейчас vanilla JS — оптимально по сложности).
- Webhook-вместо-SSE, если потребуется publish/sub другим клиентам.
- Бэкап-ротация (cron + ротация старых архивов).

---

## Связанные документы

- Спецификация модуля «База данных»: [DATABASE_MODULE_SPEC.md](DATABASE_MODULE_SPEC.md)
- Архитектура и потоки данных: [ARCHITECTURE.md](ARCHITECTURE.md)
- Подключение веб-панели: [WEBPANEL_DEPLOY.md](WEBPANEL_DEPLOY.md)
- Журнал работ и ближайшая дорожная карта: [../corebot v2.md](../corebot%20v2.md)
