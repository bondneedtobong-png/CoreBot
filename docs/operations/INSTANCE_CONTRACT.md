# Контракт инстанса CoreBot

Один пользователь — один изолированный инстанс. Один VPS обслуживает ровно
одного пользователя (владельца). Совместное использование инстанса несколькими
пользователями запрещено.

## Модель изоляции

Каждый инстанс независим от всех остальных:

- свой VPS (Ubuntu 22.04 / 24.04 LTS, Python 3.11+);
- свой `.env` с уникальными `BOT_TOKEN` и `OWNER_ID`;
- своя база `data/corebot.db` (SQLite WAL);
- своя база `data/control_plane.db`;
- свой каталог сессий `data/sessions/`;
- свои systemd-сервисы `corebot.service` и `corebot-cp.service`;
- свои бэкапы в `/opt/corebot/backups/`.

Общие между инстансами могут быть только `API_ID` / `API_HASH`
(один Telegram app на команду). `BOT_TOKEN`, `OWNER_ID`, базы данных,
сессии Telethon, `.env` и логи никогда не разделяются между пользователями.

## Системный пользователь

- Имя: `corebot`, группа: `corebot`.
- Создание: `useradd -r -m -s /bin/bash -d /opt/corebot corebot`.
- Оба сервиса (`corebot.service`, `corebot-cp.service`) запускаются
  от `User=corebot` / `Group=corebot`.
- Запуск сервисов от `root` запрещён, за исключением выполнения
  административных команд (`systemctl`, `chown`, установка пакетов).

## Каталоги и владельцы

| Путь | Назначение | Владелец |
|------|-----------|----------|
| `/opt/corebot/app` | checkout кода, `.env`, `data/`, `logs/` | `corebot:corebot` |
| `/opt/corebot/venv` | виртуальное окружение Python | `corebot:corebot` |
| `/opt/corebot/backups` | tar-архивы бэкапов | `corebot:corebot` (каталог), файлы `600` |
| `/opt/corebot/app/data` | `corebot.db`, `control_plane.db`, `sessions/`, `neuro/`, `files/` | `corebot:corebot` |
| `/opt/corebot/app/data/sessions` | файлы `*.session` Telethon-аккаунтов | `corebot:corebot` |
| `/opt/corebot/app/logs` | `corebot.log`, `error.log` | `corebot:corebot` |
| `/etc/systemd/system/corebot.service` | unit основного бота | `root:root`, `644` |
| `/etc/systemd/system/corebot-cp.service` | unit Control Plane | `root:root`, `644` |

## Права доступа

- `/opt/corebot/app/.env` — `600`, владелец `corebot:corebot`.
  Чтение группой и остальными запрещено.
- Каталоги внутри `data/` — `755`, владелец `corebot:corebot`.
- Файлы внутри `data/` (включая `*.db` и `*.session`) — `644`,
  владелец `corebot:corebot`.
- Файлы в `/opt/corebot/backups/` — `600`, владелец `corebot:corebot`
  (архивы содержат копию `.env` и баз данных).
- Проверка после переноса данных:
  `chown -R corebot:corebot /opt/corebot/app`,
  `chmod 600 /opt/corebot/app/.env`.

## Сеть и порты

- Control Plane слушает только loopback: `127.0.0.1:8081`.
  Привязка к `0.0.0.0` запрещена.
- UFW разрешает только SSH. Порт `8081` в UFW не открывается.
- Доступ к веб-панели — только через SSH-туннель
  `ssh -L 8081:127.0.0.1:8081 <user>@<host>`, затем
  `http://127.0.0.1:8081/panel/` в локальном браузере.
- Телеметрия агента по умолчанию выключена (`CP_AGENT_ENABLED=0`)
  и при включении ходит только на `http://127.0.0.1:8081/ingest/batch`.

## Сервисы

- `corebot.service`: `ExecStart=/opt/corebot/venv/bin/python main.py`,
  `WorkingDirectory=/opt/corebot/app`,
  `EnvironmentFile=/opt/corebot/app/.env`,
  `Restart=on-failure`, `RestartSec=10`.
- `corebot-cp.service`:
  `ExecStart=/opt/corebot/venv/bin/uvicorn control_plane.main:app --host 127.0.0.1 --port 8081`,
  `Restart=on-failure`, `RestartSec=5`.
- Точка входа бота — корень проекта `main.py`
  (инициализация БД, миграции, bootstrap модулей),
  а не `python -m bot.main`.

## Запреты

1. Два инстанса на одном VPS.
2. Общий `.env`, общая БД или общий каталог `sessions/` у двух пользователей.
3. Перезапись существующих `.env`, `data/`, `logs/`, `data/sessions/`
   при установке или обновлении без предварительного бэкапа.
4. Значения-заглушки в `.env`: пустые `API_ID` / `API_HASH` / `BOT_TOKEN` /
   `OWNER_ID`, а также `change-me` и `admin123` в любом поле.
   Установка при таких значениях останавливается с ошибкой.
5. Открытие порта `8081` наружу или привязка Control Plane к `0.0.0.0`.
6. Одновременный запуск встроенного парсера (`PARSER_EMBEDDED=1`)
   и отдельного процесса `python -m workers.parser_worker`.
   Разрешён ровно один режим парсера на инстанс.

## Связанные документы

- `RUNBOOK.md` — пошаговый деплой и учётная карточка инстанса.
- `docs/operations/SLO.md` — целевые показатели доступности и восстановления.
- `docs/operations/RELEASE_CONTRACT.md` — обновления, бэкапы, откат.
- `docs/operations/CONFIG_CONTRACT.md` — полный перечень переменных `.env`.
- `docs/operations/adr/0001-fleet-management.md` — выбор оркестратора парка VPS.
