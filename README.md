# 🚀 CoreBot — Telegram Mass Mailer

Система для массовой рассылки сообщений в Telegram через сеть пользовательских аккаунтов (userbot). Управляйте аккаунтами, клиентами и рассылками через Telegram-бота.

---

## 📖 Описание проекта

CoreBot — это **двухкомпонентная система**:

1. **Control Bot** (aiogram 3.x) — управляющий Telegram-бот для владельца. Через него загружаются аккаунты, клиенты, создаются и запускаются рассылки, мониторится статус.
2. **Worker Accounts** (Telethon) — рабочие userbot-аккаунты, которые физически отправляют сообщения получателям.

Владелец взаимодействует только с Control Bot, а тот координирует Worker-аккаунты через `WorkerManager`.

---

## ✨ Основные возможности

### Аккаунты
- Загрузка через ZIP-архив Tdata с автоконвертацией в `.session`
- Детальные текстовые карточки: статус, прокси, счётчик аватарок в профиле
- Редактирование: имя, bio, username; отдельно — **управление фото профиля** (список, добавить, удалить по номеру)
- Установка двухфакторной аутентификации (2FA)
- Принадлежность: **READY** / **WARMUP** / **TEST**
- Проверка прокси и спам-блока (@SpamBot)
- Дневные лимиты и flood-wait отслеживание

### Прокси
- Добавление SOCKS5/HTTP прокси через бота
- Карточки со скрытым паролем и Exit IP
- Привязка прокси к конкретным аккаунтам
- Проверка работоспособности одним кликом

### Клиенты
- Импорт базы из TXT-файла (`@username` на каждой строке)
- Автоматический парсинг и дедупликация
- Статусы: **new** / **contacted** / **invalid** / **blocked**

### Рассылки
- Создание с названием и суффиксом
- Шаблоны: `{username}`, `{date}`, `{time}`, `{fullname}`
- Настройки: typing-имитация, smart delay, batch, дневной лимит
- Запуск, остановка, удаление, просмотр статистики
- Логирование каждого сообщения (успех/ошибка)

### Мониторинг
- `/status` — полный статус системы
- Просмотр логов прямо в боте
- Статистика по аккаунтам, клиентам, рассылкам

### Безопасность
- FloodWait обработка (авто-пауза)
- Детектирование спам-блока
- Дневные лимиты на сообщения
- Доступ только для OWNER_ID

---

## 🛠 Технологический стек

| Компонент | Технология | Версия |
|-----------|------------|--------|
| Control Bot | aiogram | 3.x |
| Worker Accounts | Telethon | latest |
| Конвертация сессий | tgconvertor | latest |
| ORM | SQLAlchemy | 2.x (async) |
| БД | aiosqlite (SQLite) | — |
| Логирование | loguru | latest |
| Утилиты | aiofiles, python-dotenv | latest |
| Проверка прокси | aiohttp-socks | latest |

**Рекомендуется Python 3.11+** (проверено также на 3.12–3.14; после создания venv выполните `pip install -r requirements.txt`, включая нативный `greenlet`).

---

## 📁 Структура проекта

```
CoreBot/
│
├── main.py                     # Точка входа: логгер, конфиг, БД, запуск бота
├── migrate_mailings.py         # Скрипт ручной миграции (устарел — теперь авто-миграция)
├── requirements.txt            # Python-зависимости
├── .env / .env.example         # Секреты и шаблон конфигурации
│
├── bot/                        # 🤖 Control Bot (aiogram 3.x)
│   ├── config.py               # Загрузка .env, валидация, пути, прокси
│   ├── main.py                 # Инициализация Dispatcher, роутеры, команды /start /help /status
│   ├── handlers/               # Обработчики команд и callback
│   │   ├── accounts/           # Аккаунты: пакет (tdata, карточка, профиль, фото, 2FA, прокси…)
│   │   ├── clients.py          # Импорт TXT, парсинг, список, очистка
│   │   ├── mailing.py          # CRUD рассылок, настройки, запуск/остановка
│   │   ├── monitoring.py       # Статус системы, просмотр логов
│   │   └── proxy.py            # CRUD прокси, проверка Exit IP
│   ├── keyboards/
│   │   └── main.py             # Все inline-клавиатуры (~20 функций)
│   └── middlewares/            # Резерв (пока пустой)
│
├── workers/                    # ⚙️ Worker Accounts (Telethon userbot)
│   ├── manager.py              # Worker + WorkerManager: подключение, отправка, рассылка
│   └── session_converter.py    # Конвертация Tdata → .session
│
├── database/                   # 🗄️ Слой данных (SQLAlchemy)
│   ├── models.py               # Модели: Proxy, Account, Client, Mailing, MailingLog + Enums
│   ├── repository.py           # Класс Database: engine, авто-миграция, WAL mode
│   └── repositories.py         # CRUD-репозитории для всех моделей
│
├── utils/                      # 🛠️ Утилиты
│   ├── logger.py               # loguru: консоль + corebot.log + error.log
│   └── proxy_checker.py        # Проверка SOCKS5/HTTP через ipify.org
│
├── data/                       # 📦 Данные (не в git)
│   ├── corebot.db              # SQLite БД
│   ├── sessions/               # .session файлы аккаунтов
│   ├── files/                  # Аватарки, загруженные файлы
│   └── tdata_temp/             # Временная распаковка Tdata
│
├── docs/                       # Документация разработчика
│   ├── ARCHITECTURE.md         # Компоненты и структура handlers/accounts/
│   └── BACKLOG.md              # Известные долги и приоритеты доработок
│
└── logs/                       # 📝 Логи
    ├── corebot.log             # Основной лог (ротация 10MB, 7 дней)
    └── error.log               # Только ошибки (ротация 5MB, 30 дней)
```

---

## 🚀 Быстрый старт

### 1. Установка зависимостей

```bash
# Создание виртуального окружения Python 3.11
python -m venv venv311

# Активация (Windows)
venv311\Scripts\activate

# Активация (Linux/Mac)
source venv311/bin/activate

# Установка зависимостей
pip install -r requirements.txt
```

### 2. Настройка переменных окружения

Скопируйте `.env.example` в `.env` и заполните обязательные параметры:

```ini
# Telegram API (получить на https://my.telegram.org)
API_ID=12345678
API_HASH=abcdef1234567890abcdef1234567890

# Control Bot Token (от @BotFather)
BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrsTUVwxyz123456

# ID владельца бота (узнать через @userinfobot)
OWNER_ID=123456789

# База данных (по умолчанию SQLite)
DATABASE_URL=sqlite+aiosqlite:///data/corebot.db

# Логирование
LOG_LEVEL=INFO

# Настройки рассылок (опционально)
DEFAULT_DELAY_BETWEEN_MESSAGES=5
DEFAULT_DELAY_BETWEEN_ACCOUNTS=10
MAX_RETRIES_ON_FLOOD=3

# Прокси для Control Bot (опционально, для обхода блокировок)
CONTROL_BOT_PROXY_TYPE=socks5
CONTROL_BOT_PROXY_HOST=1.2.3.4
CONTROL_BOT_PROXY_PORT=1080
CONTROL_BOT_PROXY_USERNAME=user
CONTROL_BOT_PROXY_PASSWORD=pass
```

### 3. Запуск

```bash
python main.py
```

> ⚡ **Автоматическая миграция:** При первом запуске бот автоматически выполнит миграцию базы данных (добавит новые поля и конвертирует старые значения membership). Ручной запуск миграций не требуется!

---

## 📋 Переменные окружения

| Переменная | Описание | Обязательная |
|------------|----------|:---:|
| `API_ID` | Telegram API ID (от https://my.telegram.org) | ✅ |
| `API_HASH` | Telegram API Hash | ✅ |
| `BOT_TOKEN` | Токен бота от @BotFather | ✅ |
| `OWNER_ID` | Ваш Telegram ID (узнать через @userinfobot) | ✅ |
| `DATABASE_URL` | URL базы данных (по умолчанию `sqlite+aiosqlite:///data/corebot.db`) | ❌ |
| `LOG_LEVEL` | Уровень логирования: DEBUG, INFO, WARNING, ERROR | ❌ |
| `DEFAULT_DELAY_BETWEEN_MESSAGES` | Задержка между сообщениями (сек) | ❌ |
| `DEFAULT_DELAY_BETWEEN_ACCOUNTS` | Задержка между аккаунтами (сек) | ❌ |
| `MAX_RETRIES_ON_FLOOD` | Максимум попыток при FloodWait | ❌ |
| `CONTROL_BOT_PROXY_TYPE` | Тип прокси для Control Bot: `socks5`, `http` | ❌ |
| `CONTROL_BOT_PROXY_HOST` | Хост прокси для Control Bot | ❌ |
| `CONTROL_BOT_PROXY_PORT` | Порт прокси для Control Bot | ❌ |
| `CONTROL_BOT_PROXY_USERNAME` | Логин прокси для Control Bot | ❌ |
| `CONTROL_BOT_PROXY_PASSWORD` | Пароль прокси для Control Bot | ❌ |

---

## 🎯 Первый запуск — пошагово

1. **Запустите бота** → отправьте `/start`
2. **Добавьте прокси** (рекомендуется): Прокси → Добавить прокси → `user:pass@host:port`
3. **Загрузите аккаунты**: Аккаунты → Загрузить Tdata → Отправьте ZIP с папкой `tdata` → Выберите прокси
4. **Загрузите клиентов**: Клиенты → Загрузить базу → Отправьте TXT с `@username`
5. **Создайте рассылку**: Рассылка → Создать → Название + Суффикс
6. **Настройте**: Добавьте текст, настройте задержки и typing
7. **Запустите**: Откройте рассылку → 🚀 Запустить

---

## 📊 Архитектура

```
┌─────────────────┐
│   Владелец      │
│  (Telegram UI)  │
└────────┬────────┘
         │
         ▼
┌─────────────────┐       ┌──────────────────┐
│  Control Bot    │──────▶│   SQLite БД      │
│  (aiogram 3.x)  │◀─────│   (data/corebot.db)│
└────────┬────────┘       └──────────────────┘
         │
         ▼
┌─────────────────┐
│ Worker Manager  │
│  (Telethon)     │
└────────┬────────┘
         │
    ┌────┴────┐
    ▼         ▼
┌──────┐  ┌──────┐
│Acc #1│  │Acc #N│  ← Userbot аккаунты
└──────┘  └──────┘
```

**Поток данных:** Владелец → Control Bot → БД → WorkerManager → Telethon аккаунты → Telegram API

---

## ⚠️ Известные проблемы и технические долги

Краткая таблица ниже; **актуальный приоритизированный список** — в [`docs/BACKLOG.md`](docs/BACKLOG.md), структура проекта — в [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

| Проблема | Приоритет | Статус |
|----------|:---:|:---:|
| **Дублирование создания DB-сессии** (~40+ повторений) | 🔴 | ⏳ Запланировано |
| ~~**Конфликт callback `cancel`**~~ | — | ✅ Разведены по разделам + legacy в `main.py` |
| **Рассылка / один аккаунт, нет балансировки** | 🔴 | ⏳ Запланировано |
| **`typing_enabled` захардкожен** в `start_mailing()` | 🔴 | ⏳ Запланировано |
| **Нет pytest / регрессионных тестов** | 🔴 | ⏳ Запланировано |
| **Нет graceful shutdown** | 🟡 | ⏳ Запланировано |
| **Нет пагинации** в длинных списках | 🟡 | ⏳ Запланировано |

---

## ✅ Что уже работает

| Возможность | Статус |
|---|:---:|
| Загрузка Tdata через ZIP с конвертацией в .session | ✅ |
| Привязка прокси к аккаунтам | ✅ |
| Карточки аккаунтов (статус, прокси, счётчик аватарок) | ✅ |
| Редактирование профиля (имя, bio, username) + управление фото профиля | ✅ |
| Установка 2FA | ✅ |
| Импорт клиентов из TXT | ✅ |
| CRUD рассылок с настройками (typing, batch, delay) | ✅ |
| Шаблоны сообщений (`{username}`, `{date}`, `{time}`, `{fullname}`) | ✅ |
| FloodWait обработка | ✅ |
| Проверка прокси с Exit IP | ✅ |
| Логирование с ротацией | ✅ |
| Мониторинг `/status` | ✅ |
| Просмотр логов в боте | ✅ |
| Авто-миграция БД при запуске | ✅ |
| Прокси для Control Bot | ✅ |
| Дневные лимиты и spam-block детекция | ✅ |

---

## ⚠️ Важное предупреждение

Этот проект создан в **образовательных целях**.

Используйте ответственно и соблюдайте [Telegram ToS](https://telegram.org/tos).
Не используйте для спама и нарушений правил платформы.

---

## 📄 Лицензия

MIT
