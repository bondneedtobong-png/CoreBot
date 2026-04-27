# Parser-worker (модуль парсинга Telegram)

По умолчанию parser-loop запускается **внутри Control Plane**: если поднята веб-панель/API, то модуль парсинга уже работает и поллит `parsing_tasks` в `corebot.db`.

Подключение к Telegram идет через **Telethon** (сессии `data/sessions/*.session`, прокси из БД). `API_ID`, `API_HASH` и `DATABASE_URL` берутся из окружения (обычно из `.env`).

## Режим по умолчанию (рекомендуется)

Просто запустите веб-панель / Control Plane как обычно — дополнительных процессов не требуется.

Опционально можно выключить встроенный запуск:

- `PARSER_EMBEDDED=0`

## Отдельный процесс (опционально)

Если нужен изолированный процесс (например, на отдельном хосте), запускайте:

```bash
python -m workers.parser_worker
```

Опционально:

- `PARSER_POLL_SEC` — интервал опроса очереди (по умолчанию `2`).

## Systemd (опционально)

Файл юнита (пути подставьте под свой деплой):

```ini
[Unit]
Description=CoreBot Telegram parser worker
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/corebot
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/corebot/.venv/bin/python -m workers.parser_worker
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Сохраните как `corebot-parser.service`, затем `systemctl daemon-reload && systemctl enable --now corebot-parser`.

## Веб-панель

Раздел **Парсинг**: создание задач, список, логи, экспорт `.txt` через Control Plane (`/business/parsing/...`).

Роли:

- `tenant_viewer` — только чтение и экспорт.
- `tenant_admin` / `super_admin` — создание и отмена задач.
