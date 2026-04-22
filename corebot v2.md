# CoreBot v2 — поэтапный план реализации

Цель файла: держать единый контекст работ, отмечать прогресс, фиксировать дебаг и следующие шаги.

---

## Правила ведения

- Перед началом любой правки: обновить секцию `Текущий фокус`.
- После каждого завершенного шага: проставить `[x]` и краткий итог в `Журнал выполнения`.
- Любая непонятная ошибка: фиксировать в `Дебаг` (симптом -> гипотеза -> проверка -> итог).
- После существенных изменений: прогонять тесты и проверять логи.
- Не переходить к следующему этапу, пока не выполнены критерии готовности текущего.

---

## Текущий фокус

- Статус: `in_progress`
- Активный этап: `Этап 1 (модульность нейрочаттинга)`
- Ближайшая цель: выделить сервисный слой `services/neurochat/*` без изменения внешнего поведения бота.

---

## Этапы реализации

### Этап 0. Стабилизация прод-цикла и деплоя
- [x] Вынесена инструкция по обновлению на VPS: `docs/VPS_UPDATE_GUIDE.md`
- [x] Добавлен скрипт инициализации env: `scripts/init_env.sh`
- [x] Уточнены шаблоны `.env.example` под VPS-пути
- [x] Закрыть баг graceful shutdown (`AiohttpSession.closed`)
- [x] Зафиксировать корректный systemd ExecStart в RUNBOOK

Критерий готовности:
- Сервис стартует/рестартует без traceback.
- После перезапуска доступны разделы Аккаунты/Прокси/Рассылки.

---

### Этап 1. Модульность нейрочаттинга (ядро)
- [x] Выделить сервисный слой `services/neurochat/*` (manager/config/filters/dialog/llm)
- [x] Убрать дубли и прямые зависимости `mailing -> neuro internals`
- [x] Подготовить единый интерфейс вызова из входящих сообщений

Критерий готовности:
- Нейрочаттинг управляется как независимый модуль.
- Модуль рассылки не содержит нейро-логики генерации ответов.

---

### Этап 2. Иерархия флагов (global/local/account)
- [ ] Добавить глобальный переключатель `neurochat.enabled`
- [ ] Сохранить локальный флаг рассылки `enable_neurochat_after`
- [ ] Реализовать приоритет: global -> local -> account/filter
- [ ] Добавить явное логирование причин deny

Критерий готовности:
- Для каждого входящего видно, почему обработано/пропущено.

---

### Этап 3. Базовый pipeline входящих (без class integration)
- [ ] Унифицировать обработчик входящих через manager pipeline
- [ ] Поддержать placeholders в prompt (first_name/link/peer_username и др.)
- [ ] Вести историю диалогов в едином формате
- [ ] Стабилизировать retry/fallback для LLM

Критерий готовности:
- Входящие обрабатываются предсказуемо, история и ответы консистентны.

---

### Этап 4. Классовая система: pulse/alive и STOP->stop
- [x] `[STOP]` переводится в класс `stop` вместо отдельного stop-list
- [x] Включить +1 к `pulse` на каждом входящем по окончательной схеме
- [x] Реализовать детектор `alive` с окном 60 мин и защитой от двойного счета
- [x] Добавить таблицу/механику conversation window (идемпотентность после рестартов)

Критерий готовности:
- `pulse`/`alive` считаются корректно и не удваиваются после рестарта.

---

### Этап 5. Бот-интерфейс (минималистичное управление)
- [ ] В рассылке: `Нейрочаттинг после рассылки: ВКЛ/ВЫКЛ`
- [ ] В нейрочаттинге: глобальный toggle + настройки + мониторинг
- [x] Убран отдельный STOP-лист из нейрочата
- [ ] Добавить предупреждение при local ON и global OFF

Критерий готовности:
- Управление нейрочатом в Telegram-боте однозначное и прозрачное.

---

### Этап 6. Веб-панель (MVP)
- [ ] Дашборд статусов и нагрузок
- [ ] Раздел диалогов с фильтрами
- [ ] Раздел настройки нейрочата (prompt/model/presets)
- [ ] Раздел классовой системы (фильтры/экспорт)
- [ ] Логи/ошибки OpenRouter

Критерий готовности:
- Оператор видит live-состояние и может управлять конфигом без Telegram.

---

## Журнал выполнения

### 2026-04-21
- Выполнено:
  - Разделены меню: `Мониторинг` заменен на `Нейрочаттинг`.
  - Вынесен `bot/handlers/neurochat.py`.
  - Удалены/очищены мониторинг-роутеры и клавиатуры.
  - Убран `/cancel` в ключевых сценариях, переведено на кнопки назад.
  - Убрана кнопка `Нейрочат и прогресс` в карточке рассылки, добавлена `К нейрочаттингу`.
  - Введены placeholders для system prompt.
  - STOP-лист удален из нейрочат UI, `[STOP]` -> CRM класс `stop`.
  - Добавлен `docs/VPS_UPDATE_GUIDE.md` и `scripts/init_env.sh`.
  - Исправлен graceful shutdown в `bot/main.py`: убрана проверка `bot.session.closed`, добавто безопасное `await bot.session.close()` с обработкой исключений.
  - Прогнаны тесты: `python -m pytest tests/test_backlog_fixes.py -q --tb=short` (7 passed).
  - Обновлен `RUNBOOK.md`: закреплен корректный запуск через `main.py` и добавлен блок восстановления `data/*` (локальный ПК -> VPS).
- Открытые хвосты:
  - Нет критичных хвостов по этапу стабилизации.
### 2026-04-21 (smoke VPS)
- Smoke на VPS пройден: кнопки работают, в `journalctl` только INFO по `/start`, traceback нет.

### 2026-04-21 (этап 1 — старт сервисного слоя)
- Создан каркас `services/neurochat/*`: `manager`, `config_service`, `filters`, `dialog_service`, `llm_service`, `class_bridge`, `commands`.
- `workers/neuro_incoming.py` переведен на сервисные вызовы (LLM retry/fallback, class-bridge, command parsing, gate manager) без изменения callback/UX.
- Локальная проверка: `python -m pytest -q tests/test_backlog_fixes.py` -> `7 passed`.
- Дополнительно: `workers/neuro_incoming.py` превращён в thin-adapter, оркестрация входящих перенесена в `services/neurochat/incoming_service.py`.
- Дополнительно: `bot/handlers/mailing.py` отвязан от прямого `NeuroActionRepository`; счётчики нейро-команд читаются через `services/neurochat/stats_service.py`.
- Дополнительно: `bot/handlers/neurochat.py` переведен на единый сервисный интерфейс `services/neurochat/admin_service.py` (prompt/sampling/actions), без прямого доступа к нейро internals.
- Повторная проверка: `python -m pytest -q tests/test_backlog_fixes.py` -> `7 passed`.

### 2026-04-21 (этап 2 — старт иерархии флагов)
- Добавлен real global toggle `neurochat_enabled` в `instance_settings` (модель + sqlite-миграция в `database/repository.py`).
- Добавлен fallback из `.env`: `NEUROCHAT_ENABLED` (по умолчанию `1`) + методы в `InstanceSettingsRepository` (`get/set/clear/effective`).
- `services/neurochat/config_service.py` и `manager.py` переведены на чтение global toggle из БД/ENV.
- В `services/neurochat/incoming_service.py` включен gate с приоритетом: `global -> mailing local -> connection`, добавлено логирование причин deny (`global_disabled`, `mailing_local_disabled`, `worker_disconnected`).
- Проверка: `python -m pytest -q tests/test_backlog_fixes.py` -> `7 passed`.
- Добавлен UI global toggle в `Нейрочаттинг`: `🌐 Глобально: ВКЛ/ВЫКЛ` + `♻️ Глобально: как в .env`.
- В карточке рассылки `Нейрочат` добавлено предупреждение, если `local ON` при `global OFF`.
- Добавлен account/filter слой в gate: deny по классам `bl` (и подготовка под `ignore`), с reason-кодами в логах.
- Добавлено явное deny-логирование для класса `stop` в incoming pipeline.
- Повторная проверка: `python -m pytest -q tests/test_backlog_fixes.py` -> `7 passed`.

### 2026-04-21 (этап 3 — унификация pipeline, шаг 1)
- В `services/neurochat/manager.py` добавлен `PreparedIncomingContext` и `prepare_incoming_context(...)` для единой подготовки контекста входящих (gate + prompt/history + sampling + ключ/модель).
- `services/neurochat/incoming_service.py` упрощен: подготовительная логика вынесена в manager, сервис оставлен как orchestration отправки/команд.
- Проверка: `python -m pytest -q tests/test_backlog_fixes.py` -> `7 passed`.

### 2026-04-21 (этап 3 — унификация pipeline, шаг 2)
- Добавлен `services/neurochat/post_actions.py` (post-LLM use-cases): `persist_dialog_turn`, `send_text_reply`, `process_send_link_command`, `process_stop_command`.
- `services/neurochat/incoming_service.py` переведен на вызовы `post_actions`, убрана inline-логика отправки текста/ссылки и обработки `[STOP]`.
- Проверка: `python -m pytest -q tests/test_backlog_fixes.py` -> `7 passed`.

### 2026-04-21 (этап 3 — тесты pipeline)
- В `tests/test_backlog_fixes.py` добавлены unit-тесты:
  - `check_incoming_allowed` (priority/deny-reason: `global_disabled` -> `mailing_local_disabled` -> `client_class_bl`);
  - `send_text_reply` (успех/ошибка с возвратом bool).
- Проверка: `python -m pytest -q tests/test_backlog_fixes.py` -> `9 passed`.

### 2026-04-21 (этап 4 — pulse/alive окна)
- Добавлена таблица `client_alive_windows` (модель + SQLite-миграция) с уникальностью `(mailing_id, account_id, client_id, window_key)` для идемпотентности.
- Добавлен `services/neurochat/engagement_service.py`:
  - `track_incoming_engagement(...)` — инкремент `pulse` на каждом входящем в нейро-контексте;
  - `alive` не чаще одного раза за 60 минут через `window_key`.
- `services/neurochat/manager.py` интегрирован с engagement tracker в pre-LLM pipeline.
- Добавлен unit-тест `test_alive_window_key_hour_bucket`.
- Проверка: `python -m pytest -q tests/test_backlog_fixes.py` -> `10 passed`.

### 2026-04-22 (фикс тестовой очереди)
- Исправлен `database/repositories.py`: в `audience_mode=test` клиенты из `mailing_test_recipients` больше не исключаются по прошлым успешным `mailing_logs`.
- Теперь тестовый txt-список переиспользуется на каждом запуске рассылки (кроме `INVALID/BLOCKED`), что позволяет стабильно ретестить нейрочат и классы.
- Проверка: `python -m pytest -q tests/test_backlog_fixes.py` -> `10 passed`.

---

## Дебаг и анализ (рабочий шаблон)

### Инцидент: `<название>`
- Симптом:
  - ...
- Гипотеза:
  - ...
- Проверка:
  - Команды:
    - `journalctl -u corebot.service -n 150 --no-pager`
    - `systemctl status corebot.service --no-pager -l`
    - `sqlite3 /opt/corebot/app/data/corebot.db "select count(*) from accounts;"`
- Итог:
  - ...
- Действие:
  - ...

---

## Минимальный протокол тестирования после каждой задачи

1) Unit/локальные:
- `python -m pytest tests/test_backlog_fixes.py -q --tb=short`

2) Smoke в боте:
- `/start`
- `Аккаунты -> список`
- `Прокси -> список`
- `Рассылка -> карточка -> настройки`
- `Нейрочаттинг -> открытие рассылки`

3) Прод-логи:
- `journalctl -u corebot.service -n 120 --no-pager`
- Нет критичных traceback.

---

## Следующий конкретный шаг

- [x] Зафиксировать в RUNBOOK корректный `ExecStart` (`/opt/corebot/app/main.py`) и секцию восстановления `data/*` (db + sessions + neuro + files).
- [x] Провести smoke на VPS: `/start`, `Аккаунты`, `Прокси`, `Нейрочаттинг`; проверить `journalctl` без traceback.
- [x] Начать Этап 1: создать каркас `services/neurochat/*` и перенести туда оркестрацию входящих без смены текущих callback/UX.
- [x] Продолжить Этап 1: убрать оставшиеся прокси-обертки в `workers/neuro_incoming.py` и оставить worker как thin-adapter к `services/neurochat`.
- [x] Завершить Этап 1: зачистить зависимости `mailing -> neuro internals` и зафиксировать единый интерфейс вызова в сервисе.
- [x] Начать Этап 2: добавить real global toggle `neurochat.enabled` в БД + приоритет global/local/account.
- [x] Продолжить Этап 2: добавить UI/команды управления global toggle в `Нейрочаттинг` + предупреждение при local ON и global OFF.
- [x] Завершить Этап 2: добавить account/filter слой (BL/ignore) в приоритет и лог причин deny на каждую ветку.
- [x] Начать Этап 3: унифицировать pipeline входящих через manager + убрать остатки inline-логики в incoming_service.
- [x] Продолжить Этап 3: вынести post-LLM actions (send_text/send_link/stop) в отдельный service/use-case слой.
- [x] Завершить Этап 3: добавить unit-тесты на manager/post_actions (deny reason + command side-effects) и зафиксировать протокол smoke.
- [x] Начать Этап 4: реализовать корректный `alive` detector (окно 60 мин, защита от double-count после рестартов).
- [ ] Провести smoke на VPS по классам `pulse/alive/stop` и сверить логи `Neuro incoming denied` + CRM counters.
- [ ] Добавить one-command скрипт обновления `update_corebot.sh` (git pull + pip install + restart + status/logs) и описать в RUNBOOK/VPS guide.
