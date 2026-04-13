# Архитектура CoreBot

Обновлено: апрель 2026.

## Назначение

**CoreBot** — система массовой рассылки в Telegram через userbot-аккаунты (Telethon), с панелью управления в виде Telegram-бота (aiogram 3).

## Компоненты

| Компонент | Роль |
|-----------|------|
| `main.py` | Точка входа: логирование, инициализация БД, запуск Control Bot |
| `bot/main.py` | Dispatcher, роутеры, команды `/start`, `/help`, `/status`, сессия aiohttp (прокси для Bot API) |
| `bot/handlers/` | Обработчики callback и сообщений |
| `bot/keyboards/main.py` | Inline-клавиатуры |
| `workers/manager.py` | `Worker` (один Telethon-клиент), `WorkerManager` (пул, рассылка, проверки) |
| `workers/session_converter.py` | Tdata → `.session` (tgconvertor) |
| `database/` | SQLAlchemy 2 async, SQLite (aiosqlite), авто-миграции в `repository.py` |
| `utils/logger.py` | loguru |
| `utils/proxy_checker.py` | Проверка SOCKS5/HTTP (Exit IP) |

## Поток данных

Владелец → Control Bot → SQLite → WorkerManager → Telethon-сессии → Telegram API.

Рассылка читает настройки рассылки и клиентов из БД, берёт сообщения из очереди логики в `WorkerManager.start_mailing()` (см. исходный код для актуальных ограничений).

## Пакет `bot/handlers/accounts/`

Монолитный `accounts.py` заменён пакетом с разделением по сценариям:

| Файл | Ответственность |
|------|-----------------|
| `common.py` | `safe_edit_message`, текст/HTML карточки аккаунта |
| `states.py` | Все `StatesGroup` для аккаунтов |
| `tdata.py` | Загрузка ZIP, `proxy_select_*`, конвертация |
| `list_card.py` | Список аккаунтов, `account_view_*`, перепроверка авторизации |
| `profile.py` | Имя, bio, username |
| `photos.py` | Управление фото профиля userbot |
| `twofa.py` | Установка 2FA |
| `membership_delete.py` | Membership, подтверждение и удаление аккаунта |
| `groups.py` | Группы аккаунтов (таблицы `groups` / `account_groups`), массовые проверки прокси и спам-блока по группе |
| `cancel.py` | Callback `cancel_accounts` |
| `proxy_assign.py` | `account_change_proxy_*`, `proxy_assign_*` |
| `tags.py` | Теги аккаунта |
| `__init__.py` | Сборка единого `router` через `include_router` |

Импорт для диспетчера не меняется: `from bot.handlers.accounts import router`.

## Зависимости ключевых сценариев

- **Карточка аккаунта / профиль / фото:** временный `Worker(account, session_path, account.proxy)` — прокси аккаунта обязателен для корректной проверки сессии, если аккаунт заведён с прокси.
- **2FA / проверки по группе:** `WorkerManager` (`load_accounts`, затем `check_proxies_for_account_ids` / `check_spam_blocks_for_account_ids` для списка `account_id` группы).

## Файлы данных

- `data/corebot.db` — БД  
- `data/sessions/*.session` — Telethon-сессии  
- `data/files/avatars/` — временные файлы при загрузке аватарки через бота  
- `data/tdata_temp/` — распаковка ZIP при импорте  

Кнопки «Отмена» в разных разделах используют разные `callback_data` (`cancel_accounts`, `cancel_mailing`, `cancel_proxy`). Старое значение `cancel` обрабатывается последним роутером в `bot/main.py` и ведёт в главное меню.

Подробный список улучшений и известных ограничений — в [BACKLOG.md](BACKLOG.md).
