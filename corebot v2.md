# CoreBot v2 — журнал работ и дорожная карта

Цель файла: держать единый рабочий контекст, фиксировать прогресс по этапам, дебаг-инциденты и ближайшие шаги. История завершённых правок свёрнута в сводку, чтобы не разрастаться. Полный backlog — в `docs/BACKLOG.md`, шпаргалка команд — в `HELP.md`.

---

## Правила ведения

- Перед началом любой правки: обновить секцию `Текущий фокус`.
- После каждого завершённого шага: проставить `[x]` и краткий итог в `Журнал`.
- Любая непонятная ошибка: фиксировать в `Дебаг` (симптом → гипотеза → проверка → итог).
- После существенных изменений: прогонять `pytest tests/` и сверять `journalctl -u corebot.service`.
- Не переходить к следующему этапу, пока не выполнены критерии готовности текущего.

---

## Текущий фокус

- Статус: `in progress`
- Активный проход: `Дебаг + улучшения (первый проход) — закрыт 2026-06-01`
- Сделано в проходе: debug-sweep (2 latent NameError), утечка `_dialog_locks`, `clients.username → nullable`, доделано меню «База данных», экран «Статус системы», мониторинг deny-причин нейрочата.
- Ближайшая цель: следующий проход — фичи (аналитика воронки / планировщик рассылок / здоровье аккаунтов), плюс sampling-пресеты creative/balanced/strict (Этап 5, остаток).

---

## Этапы реализации

### Этап 0. Стабилизация прод-цикла и деплоя — DONE

- [x] `docs/VPS_UPDATE_GUIDE.md`, `scripts/init_env.sh`, `scripts/update_corebot.sh`, `scripts/cb_push.cmd`.
- [x] Шаблон `.env.example` под VPS-пути, корректный `ExecStart` в `RUNBOOK.md`.
- [x] Graceful shutdown в `bot/main.py` (без `bot.session.closed`-проверки).
- [x] Прогоны `pytest tests/test_backlog_fixes.py` после каждой правки.

### Этап 1. Модульность нейрочаттинга (ядро) — DONE

- [x] Сервисный слой `services/neurochat/*`: `manager`, `config_service`, `dialog_service`, `llm_service`, `class_bridge`, `commands`, `incoming_service`, `post_actions`, `engagement_service`, `stats_service`, `admin_service`, `filters`.
- [x] `workers/neuro_incoming.py` — thin-adapter; вся оркестрация в `incoming_service`.
- [x] `bot/handlers/mailing.py` отвязан от прямого `NeuroActionRepository` (через `stats_service`).
- [x] `bot/handlers/neurochat.py` переведён на `admin_service` (prompt/sampling/actions).

### Этап 2. Иерархия флагов (global / mailing / account) — DONE

- [x] Real global toggle `instance_settings.neurochat_enabled` + fallback `.env: NEUROCHAT_ENABLED`.
- [x] Приоритет `global → mailing local → account/filter`, с reason-кодами в логах: `global_disabled`, `mailing_local_disabled`, `worker_disconnected`, `client_class_bl`, `client_class_stop`.
- [x] UI-toggle в `Нейрочаттинг`: `🌐 Глобально: ВКЛ/ВЫКЛ` + `♻️ как в .env`.
- [x] Предупреждение в карточке рассылки при `local ON & global OFF`.

### Этап 3. Унификация pipeline входящих — DONE

- [x] `prepare_incoming_context(...)` в `manager.py`.
- [x] `post_actions.py`: `persist_dialog_turn`, `send_text_reply`, `process_send_link_command`, `process_stop_command`.
- [x] Unit-тесты: `check_incoming_allowed` (priority/deny-reason), `send_text_reply` (success/error).

### Этап 4. Классовая система: pulse / alive / stop — DONE

- [x] `[STOP]` → класс `stop` (отдельный stop-list удалён).
- [x] `pulse` инкрементируется на каждом входящем в нейро-контексте (после первого касания рассылки).
- [x] `alive` с окном 60 минут через `client_alive_windows(window_key)` — идемпотентно после рестартов.
- [x] Unit-тест `test_alive_window_key_hour_bucket`.

### Этап 5. Бот-интерфейс нейрочата — частично

- [x] Глобальный toggle + фолбэк на `.env`.
- [x] Удалён STOP-лист из UI.
- [x] Предупреждение при `local ON & global OFF`.
- [x] Минимальный мониторинг внутри `Нейрочаттинг` (счётчики причин deny за последние N часов) — `services/neurochat/monitor.py` + экран «📊 Мониторинг нейрочата» (1/6/24 ч). 2026-06-01.
- [ ] Шорткаты «применить sampling-пресет» (creative / balanced / strict).

### Этап 6. Веб-панель — большая часть DONE, см. ниже

Сделано:

- [x] `control_plane/` (FastAPI) + multi-tenant модель + audit log + alert engine с Telegram-нотификациями.
- [x] Sync-движок к `corebot.db` (`BOT_DATABASE_URL`).
- [x] SPA `web-panel/`: Dashboard / Accounts / Dialogs / Mailings / Clients / Groups / Proxies / Logs / Archive / Settings.
- [x] Бизнес-API `/business/*` (54 роута), включая `/instance`, `/groups`, `/proxies`, `/mailings/{id}/prompt`, `/cleanup/v2`, `/archive/*`.
- [x] AI_ACTIVE ↔ MANUAL переключатель + `OutboundConsumer` с lazy reconnect и экспоненциальным бэк-оффом.
- [x] `BotCommandConsumer` (start/pause/stop рассылки из веба через очередь команд).
- [x] SSE `/business/stream?token=...`, live-feed, видимая очередь `outbound_queue` в ленте диалога.
- [x] Бизнес-дашборд (KPI + 24-часовая stacked-bar тайм-серия + классовые бары + топ-аккаунты).
- [x] Soft-delete: `*_archive` таблицы + `mode='archive'` в cleanup, восстановление из архива.
- [x] Редактор рассылки разделён на «Настройки рассылки» и «Настройки нейрочата» (включая system-промпт).
- [x] Диалоги: сортировка аккаунтов по последнему сообщению, поиск по `list_label`, в строке аккаунта — Telegram-имя/`@username`.
- [x] CRUD из UI: аккаунты (теги/прокси/группы/лимиты), группы, прокси с TCP-тестом, глобальные настройки, ключ OpenRouter (Fernet).
- [x] Bugfixes: «исчезающие сообщения после ручной отправки», «прокручивающийся логин-экран», `worker not connected` для ручных отправок.
- [x] Создание новых сущностей из UI: быстрый create для `accounts` и `mailings` (draft).

Осталось (см. также `docs/BACKLOG.md`):

- [x] Логи OpenRouter отдельным каналом: `#/logs` канал `openrouter` + фильтры `provider/model/prompt_id` и поиск по сообщению.
- [x] Раздел «Очередь» в UI (отдельно от диалогов): фильтры + массовый retry/cancel + item-level действия.
- [x] Сортировка/поиск в основном списке аккаунтов (`#/accounts`): фильтр по `list_label/@username/phone/#id`, сортировки `id/dialogs/last_dialog/label`.
- [ ] Импорт листов 211/212 из веба с отчётом и подтверждением.
- [x] Смена пароля в UI + управление операторами: создание пользователя, список пользователей, reset пароля из `#/settings`.
- [x] Role-based ограничения для `tenant_viewer`: read-only, запрет write-операций (cleanup/редактирование/секреты), `/auth/me` и отображение роли в вебе.
- [ ] Авто-cleanup по расписанию (`instance_settings.cleanup_cron`).
- [ ] Расширение pytest от smoke к регрессионным сценариям (рассылка, импорт, группы, прокси, нейрочат, outbound queue).
- [ ] (Опционально) Перевод фронта на Vite/React, если экранов станет больше.

---

## Долги по коду (не привязанные к этапам)

- `services/database/accept_transcript.py` — Telethon-выгрузка переписки на accept всё ещё помечена TODO; написать сервисный путь и подключить к UI.
- В `bot/handlers/mailing.py` дублируется логика API credentials — централизовать в `services/neurochat/admin_service` или `utils/openrouter`.
- Единый `parse_mode=ParseMode.HTML` во всех ответах бота (сейчас местами Markdown).
- Кэш или фоновое обновление `photo_count` после операций с аватарками.
- Webhook-вместо-SSE, если потребуется publish/sub другим клиентам.
- Бэкап-ротация (cron + ротация старых архивов в `data/`).

---

## Журнал (свёрнутая сводка)

История событий до 2026-04-25 свёрнута в сводку ниже. Детальные дневные записи доступны через `git log -- "corebot v2.md"`.

### 2026-04-21
- Этап 0 закрыт: graceful shutdown, RUNBOOK с корректным `ExecStart`, восстановление `data/*` с локального ПК на VPS, `docs/VPS_UPDATE_GUIDE.md`, smoke на VPS без traceback.
- Этап 1 завершён: каркас `services/neurochat/*`, `workers/neuro_incoming.py` → thin-adapter, `bot/handlers/mailing.py|neurochat.py` отвязаны от прямого доступа к нейро internals.
- Этап 2 завершён: real global toggle + UI + предупреждение `local ON / global OFF` + account/filter слой с `client_class_bl/stop`.

### 2026-04-21 (этапы 3 и 4)
- Этап 3: `prepare_incoming_context`, вынос post-LLM use-cases в `post_actions.py`, unit-тесты.
- Этап 4: таблица `client_alive_windows`, `engagement_service.py` (`pulse` + `alive` с защитой от двойного учёта).

### 2026-04-22
- Фикс тестовой очереди: в `audience_mode=test` клиенты из `mailing_test_recipients` больше не исключаются по прошлым `mailing_logs` → стабильный ретест.

### 2026-04-25 (web-panel v1 → v4)
- v1: `control_plane/` + бизнес-модули, AI_ACTIVE/MANUAL, `OutboundConsumer`, SSE.
- v2: видимость очереди в ленте, retry/cancel, boot splash, `outbound_queue.attempts/next_attempt_at`, экспоненциальный бэк-офф.
- v2.2: `worker_manager.connect_all()` при старте бота + `_ensure_worker(...)` lazy reconnect — ручные отправки больше не зависают.
- v3: бизнес-дашборд, soft-delete (`*_archive`), CRUD рассылок и клиентов, `BotCommandConsumer`, страница «Архив».
- v4: UI «на всё основное» + 18 новых `/business/*` роутов: instance settings (Fernet OpenRouter key), groups CRUD, proxies CRUD + TCP test, account editor + safe delete, mailing editor + system-промпт. Bugfixes: исчезающие сообщения, scroll логин-экрана.
- v5: разделение «Настройки рассылки» / «Настройки нейрочата» в редакторе кампании; `AccountListItem.last_dialog_at`; в Диалогах — сортировка по последнему сообщению, поиск по `list_label`, второй строкой Telegram-имя/`@username`.

### 2026-04-25 (документация)
- Удалены устаревшие `control-plane/README.md` и `control_plane/ARCHITECTURE.md` (multi-tenant MVP давно перерос в бизнес-модули из `docs/WEBPANEL_DEPLOY.md`).
- Полностью переписан корневой `README.md` под публичный git: возможности, стек, архитектура, быстрый старт, прод-деплой, ссылки на документацию.
- Создан `HELP.md` — практическая шпаргалка команд (обновление прод-инстанса, локально на ПК, SSH, веб-панель, бот, БД, бэкапы, тесты, прокси, диагностика, скрипты репозитория).
- Обновлены `docs/ARCHITECTURE.md` (карта компонентов и потоков, сервисный слой нейрочата, структура `control_plane/`) и `docs/BACKLOG.md` (отделено сделанное от долгов по приоритетам).
- Этот файл (`corebot v2.md`) переведён в более компактный формат: длинные дневные блоки свёрнуты в сводку.

### 2026-06-01 (дебаг + улучшения, первый проход)

- **Debug-sweep:** `pytest tests/` зелёный (расширен с 26 до 31). pyflakes по проекту: найдено и починено 2 latent `NameError` — `mailing.py cb_mailing_cap_save` (undefined `raw` → падал ввод «Лимит успешных») и `twofa.py process_2fa_password` (undefined `account_id` → падал ввод первого пароля 2FA). Остальные предупреждения — косметика/строковые аннотации. Enum-маппинги эмодзи (`Account/Client/MailingStatus`) сверены — синхронны.
- **Утечка `_dialog_locks`** (`services/neurochat/incoming_service.py`): введён самоочищающийся контекст-менеджер `_dialog_lock` с refcount — лок удаляется, как только его отпустил последний пользователь. Тесты на очистку + сериализацию.
- **`clients.username → nullable`:** модель + идемпотентная пересборка таблицы в `_run_migrations` с авто-снимком `clients_pre_username_backup`; заодно навешен недостающий UNIQUE на `telegram_user_id` (был drift). Проверено на копии и локальной БД (данные целы, idempotent). Прод: бэкап `data/` перед деплоем.
- **Меню «База данных»:** закрыты 3 заглушки — экспорт классов в .txt (Свежак/ЧС/Живые), выгрузка логов (снимок аккаунтов + переписка за период), удаление юзеров (из списка/по классу-статусу) с подтверждением. Новые сервисы `client_export.py`/`client_delete.py`, хендлеры в `exports_delete.py`.
- **Экран «Статус системы»** в главном меню (`menu_status`) + унификация `/status` на общий билдер.
- **Мониторинг нейрочата** (Этап 5): `monitor.py` (in-memory) + экран deny-причин за 1/6/24 ч.
- Коммиты смысловыми кусками (7 шт.). Изменения **закоммичены локально, не запушены** — деплой по `cb_push.cmd` на усмотрение владельца.

### 2026-06-01 (прокси-гейт безопасности + чистка флота + локальная панель)

- **Безопасная загрузка аккаунтов:** `WorkerManager.connect_all` по умолчанию (`require_working_proxy=True`) сначала проверяет прокси (`precheck_proxies`, параллельно) и **не подключает/не авторизует** аккаунты с мёртвым или отсутствующим прокси — защита от банов (вход с IP сервера). Проверено на живой БД: 13 аккаунтов с просроченными прокси → все пропущены.
- **Фикс `utils/proxy_checker`:** для SOCKS5 передавался `proxy_type=5`, в aiohttp_socks SOCKS5=2 → `ValueError`, любой socks5 ложно считался мёртвым. Теперь `ProxyType.SOCKS5/HTTP`. Чинит и кнопку «Тест прокси».
- **Массовая чистка флота** (`services/database/fleet_cleanup.py` + `bot/handlers/fleet_cleanup.py`): Прокси → удалить свободные / все (с отвязкой); Аккаунты → проверить прокси сейчас, отвязать прокси у всех, удалить все аккаунты (+ сессии + зависимые данные). Всё с подтверждением. Регресс-тесты (pytest 33/33).
- **Локальная веб-панель** настроена для демо: в `.env` добавлены `CP_JWT_SECRET`, логин/пароль (`admin`/`corebot2026`), `BOT_DATABASE_URL`, `PARSER_EMBEDDED=0`; пароль админа сброшен в `control_plane.db`. Проверено: `/health`, логин, `/business/*`.

### 2026-06-01 (тестовый режим рассылки + UX режимов)

- **Тестовый режим стал аккаунт-мажорным:** `audience_mode=test` → `WorkerManager._run_test_mailing`: КАЖДЫЙ подключённый аккаунт группы пишет КАЖДОМУ тестовому получателю (без дедупа/ротации, лимит и пауза не применяются). Раньше тест шёл по общему client-major циклу и при 1 username отписывался один аккаунт. Production-цикл (new/classes) не затронут — добавлен только `if test_mode: break`-гард. Новый `ClientRepository.get_test_recipients_all`.
- **Описание логики режима** на экране «Аудитория рассылки» для каждого режима (`_audience_mode_logic_text`: test/new/classes).
- **Ручной ввод тест-получателей:** `@username` можно вписать текстом (строки/запятые/пробелы), не только `.txt` — обработчик `waiting_test_txt` на `F.text` (через `parse_usernames_from_txt`).
- Регресс-тесты (pytest 37/37).

### 2026-06-01 (управление группами аккаунтов из меню «Аккаунты»)

- Управление группами было доступно только из «Список аккаунтов → 📁 Группы» (при пустом списке — недоступно). Добавлена кнопка **«📁 Группы аккаунтов»** прямо в меню «Аккаунты» (открывает существующий `accounts_groups`: список/создать/состав/удалить/проверки).
- **«🗑 Удалить пустые группы»** в меню групп (с подтверждением) — чистка старых групп без аккаунтов; группы с аккаунтами и сами аккаунты не трогаются. `GroupRepository.count_empty/delete_empty`. «Назад» из групп → меню «Аккаунты».
- Регресс-тест на count_empty/delete_empty. pytest 38/38.

### 2026-06-01 (меньше лишних переподключений аккаунтов)

- После остановки/завершения рассылки `_restore_workers_after_mailing` делал `disconnect_all + connect_all` даже когда аккаунты уже подключены — лишний churn (в логах: «Аккаунт N отключён» → сразу реконнект). Теперь, если нужный пул (аккаунты группы с сессией) уже совпадает с загруженным, реконнект **пропускается** — аккаунты остаются на связи для нейрочата. Reconnect только при изменении состава пула. Извлечён `_pool_member_ids`. Тесты на skip/reconnect. pytest 40/40.
- Контекст: реконнект уже-авторизованной сессии через тот же прокси сам по себе не вызывает банов (это штатное поведение клиента), но лишние переподключения — ненужный шум; убрали.

---

## Дебаг (рабочий шаблон)

### Инцидент: latent NameError в обработчиках бота (2026-06-01)
- Симптом: тихие падения при вводе «Лимит успешных» рассылки и первого пароля 2FA.
- Гипотеза: undefined-переменные в редко-проходимых ветках (не покрыты тестами/смоком).
- Проверка: `python -m pyflakes bot services workers control_plane utils database` → `undefined name 'raw'`, `undefined name 'account_id'`.
- Итог: `raw` восстановлен из `message.text`; `account_id` берётся из FSM-state. Зафиксировано в commit `fix: устранены два latent NameError (debug-sweep)`.
- Действие: pyflakes добавлен в инструментарий прохода (не в requirements — ставится локально).

### Инцидент: `<название>`
- Симптом:
  - …
- Гипотеза:
  - …
- Проверка:
  - Команды:
    - `journalctl -u corebot.service -n 200 --no-pager`
    - `journalctl -u corebot-cp.service -n 200 --no-pager`
    - `sqlite3 /opt/corebot/app/data/corebot.db "SELECT count(*) FROM accounts;"`
- Итог:
  - …
- Действие:
  - …

---

## Минимальный протокол после задачи

1. **Unit / локальные тесты:**
   - `python -m pytest tests/test_backlog_fixes.py -q --tb=short`
   - `node -c web-panel/main.js` (если правил фронт)
2. **Smoke в боте:**
   - `/start` → `Аккаунты` → `Прокси` → `Рассылка → карточка → настройки` → `Нейрочаттинг`.
3. **Smoke в веб-панели:**
   - `/health` → 200; логин в `/panel/`; открыть Dashboard / Dialogs / Mailings.
4. **Прод-логи (после деплоя):**
   - `journalctl -u corebot.service -n 120 --no-pager` — нет критичных traceback.
   - `journalctl -u corebot-cp.service -n 120 --no-pager` — нет критичных traceback.

---

## Следующий конкретный шаг

- [ ] Расширить pytest от smoke к регрессии (outbound queue retry, mailing patch lock в RUNNING, классы pulse/alive).

Промежуточно закрыто:
- [x] Валидация обработки классов клиентов (`pulse/alive/accept/decline/stop/bl`) unit/regression-тестами.
- [x] Регрессия на `outbound queue retry` и lock редактирования рассылки в статусе `RUNNING`.
