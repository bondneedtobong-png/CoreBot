# Деплой веб-панели CoreBot на тот же VPS, где работает бот

Этот документ — узкая инструкция: поставить веб-панель **на тот же VPS,
где уже крутится `corebot.service`**, ничего не сломав.

Он не дублирует `RUNBOOK.md` (там полный bootstrap нового VPS), а
описывает только добавление панели.

---

## 0) Предпосылки и текущее состояние

Что уже есть в репозитории (после `git pull`):

- Бэкенд панели: `control_plane/` (FastAPI).
- Фронтенд панели (статика): `web-panel/`.
- Агент телеметрии в боте: `utils/telemetry.py` (по умолчанию **выключен**).
- Все зависимости уже в `requirements.txt` (отдельный venv не нужен).

После деплоя у тебя будет работать **два** systemd-сервиса в один venv:

- `corebot.service` — основной бот (как сейчас).
- `corebot-cp.service` — веб-панель + ingest API (новое).

Панель слушает только `127.0.0.1:8081`. Наружу порт **не открывается**.
Доступ с твоего ПК — через SSH-туннель (см. раздел 6).

---

## 1) Безопасность: что важно зафиксировать сразу

1. Порт `8081` **не открывать** в `ufw`. Только loopback.
2. `CP_JWT_SECRET` — длинный случайный (минимум 32 байта).
3. `CP_BOOTSTRAP_ADMIN_PASSWORD` — сильный, **сменить после первого входа**.
4. SQLite панели — отдельный файл, **не пересекается** с `corebot.db` бота.
5. Если хочешь публичный HTTPS — через `nginx` + Let's Encrypt (раздел 7).
   Без этого панель не должна светиться в интернет.

---

## 2) Подготовка `.env` на VPS

Открой `.env`:

```bash
sudo -u corebot nano /opt/corebot/app/.env
```

Убедись, что есть блок Control Plane (если его нет — добавь):

```env
# === CONTROL PLANE (web-panel + ingest) ===
CP_DATABASE_URL=sqlite:////opt/corebot/app/data/control_plane.db
CP_JWT_SECRET=<сюда_длинный_секрет>
CP_BOOTSTRAP_ADMIN_USERNAME=admin
CP_BOOTSTRAP_ADMIN_PASSWORD=<сильный_пароль>

# (опционально) Telegram-нотификации алертов
CP_TELEGRAM_BOT_TOKEN=
CP_TELEGRAM_ALERT_CHAT_ID=

# Бизнес-данные бота: панель открывает основную БД бота на чтение/запись
# (аккаунты, диалоги, ручные отправки). Это тот же файл, что DATABASE_URL,
# но без +aiosqlite (используется sync-движок).
BOT_DATABASE_URL=sqlite:////opt/corebot/app/data/corebot.db

# SSE-стрим (опционально, можно не задавать — есть дефолты):
CP_BUSINESS_STREAM_INTERVAL=1.5
CP_BUSINESS_STREAM_BATCH=200

# === Агент телеметрии в основном боте (corebot.service) ===
# Пока 0 — бот ничего не шлёт в панель. Включим позже (раздел 8).
CP_AGENT_ENABLED=0
CP_INGEST_URL=http://127.0.0.1:8081/ingest/batch
CP_AGENT_TOKEN=
CP_AGENT_NAME=corebot-agent
CP_AGENT_VERSION=dev
```

Сгенерировать сильный JWT-секрет:

```bash
openssl rand -hex 32
```

Сильный пароль админа можно так же.

Проверь права на `.env`:

```bash
chmod 600 /opt/corebot/app/.env
chown corebot:corebot /opt/corebot/app/.env
```

---

## 3) Зависимости

Если репозиторий уже свежий и `update_corebot.sh` отработал — `fastapi/uvicorn/PyJWT/passlib`
уже стоят. Если сомневаешься, прогони:

```bash
sudo -u corebot /opt/corebot/venv/bin/pip install -r /opt/corebot/app/requirements.txt
```

---

## 4) systemd-юнит `corebot-cp.service`

Создать файл `/etc/systemd/system/corebot-cp.service`:

```bash
sudo nano /etc/systemd/system/corebot-cp.service
```

Содержимое:

```ini
[Unit]
Description=CoreBot Control Plane (FastAPI web-panel)
After=network.target

[Service]
Type=simple
User=corebot
Group=corebot
WorkingDirectory=/opt/corebot/app
EnvironmentFile=/opt/corebot/app/.env
ExecStart=/opt/corebot/venv/bin/uvicorn control_plane.main:app --host 127.0.0.1 --port 8081
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Активировать:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now corebot-cp.service
sudo systemctl status corebot-cp.service --no-pager -l
```

Должно быть `active (running)`. Если падает — раздел 9.

---

## 5) Smoke-проверка прямо на VPS

```bash
curl -s http://127.0.0.1:8081/health
# Ожидание: {"ok":true}

curl -s http://127.0.0.1:8081/panel/ -o /tmp/panel.html
head -5 /tmp/panel.html
# Ожидание: HTML с <title>CoreBot Control Panel</title>
```

Проверить, что БД панели создалась:

```bash
ls -lh /opt/corebot/app/data/control_plane.db
```

Если файла нет — значит бутстрап не прошёл. Смотри логи:

```bash
journalctl -u corebot-cp.service -n 100 --no-pager
```

---

## 6) Доступ к панели с твоего ПК (SSH-туннель)

Это безопасный способ — порт 8081 наружу так и **не выставляется**.

На своём ПК (Windows CMD / PowerShell):

```cmd
ssh -L 8081:127.0.0.1:8081 root@<VPS_IP>
```

Пока окно SSH открыто — открой в браузере:

```
http://127.0.0.1:8081/panel/
```

Логин:
- username: значение `CP_BOOTSTRAP_ADMIN_USERNAME` (по умолчанию `admin`)
- password: значение `CP_BOOTSTRAP_ADMIN_PASSWORD`

После входа увидишь Summary / Agents / Logs / Alerts.
Пока бот не шлёт телеметрию — все счётчики будут нулями. Это нормально.

---

## 7) (Опционально) Публичный HTTPS через nginx + Let's Encrypt

Делай этот раздел только если реально нужен внешний доступ без SSH.

### 7.1. Поставить nginx + certbot

```bash
sudo apt update
sudo apt install -y nginx certbot python3-certbot-nginx
```

### 7.2. Конфиг сайта `/etc/nginx/sites-available/corebot-panel`

Замени `panel.example.com` на свой домен, который уже указывает A-записью на VPS.

```nginx
server {
    listen 80;
    server_name panel.example.com;

    # для certbot ACME challenge
    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    location / {
        return 301 https://$host$request_uri;
    }
}

server {
    listen 443 ssl http2;
    server_name panel.example.com;

    # Сертификаты подставит certbot, см. ниже
    ssl_certificate     /etc/letsencrypt/live/panel.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/panel.example.com/privkey.pem;

    # Закрываем ingest снаружи: его дергает только сам бот по 127.0.0.1
    location /ingest/ {
        deny all;
        return 403;
    }

    # Базовая защита заголовками
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "no-referrer" always;

    location / {
        proxy_pass http://127.0.0.1:8081/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Активировать:

```bash
sudo ln -s /etc/nginx/sites-available/corebot-panel /etc/nginx/sites-enabled/corebot-panel
sudo nginx -t
sudo systemctl reload nginx
```

### 7.3. Открыть 80/443 в ufw и получить сертификат

```bash
sudo ufw allow 'Nginx Full'
sudo certbot --nginx -d panel.example.com
```

Certbot сам пропатчит конфиг под HTTPS и поставит cron на продление.

### 7.4. Дальше

После этого открывается:

```
https://panel.example.com/panel/
```

Логин — тот же. Туннель больше не нужен.

---

## 8) (Опционально) Включить отправку телеметрии из бота

Без этого в дашборде нечего показывать. Шаги:

### 8.1. Создать агента и токен в панели

После логина (через SSH-туннель или HTTPS) вызови админ-эндпоинты.
Самый быстрый способ — через `curl` прямо с VPS:

```bash
# 1) Логин (получаем access_token)
ADMIN_USER=admin
ADMIN_PASS=IAGJnfsir82719fajx521iN
TOKEN=$(curl -s -X POST http://127.0.0.1:8081/auth/login \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASS\"}" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

# 2) Создаём агента в дефолтном tenant_id=1
AGENT_ID=$(curl -s -X POST "http://127.0.0.1:8081/admin/agents?tenant_id=1&name=corebot-agent" \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")

# 3) Получаем agent token (один раз — потом скрытый)
curl -s -X POST "http://127.0.0.1:8081/admin/agents/$AGENT_ID/rotate-token" \
  -H "Authorization: Bearer $TOKEN"
```

Ответ будет вида:

```json
{"agent_id": 1, "agent_token": "AbCdEf...очень_длинный_токен"}
```

### 8.2. Прописать токен в `.env` бота

```bash
sudo -u corebot nano /opt/corebot/app/.env
```

Поменять:

```env
CP_AGENT_ENABLED=1
CP_AGENT_TOKEN=<вставь_agent_token_из_шага_8.1>
CP_INGEST_URL=http://127.0.0.1:8081/ingest/batch
```

### 8.3. Перезапустить **только бот** (панель не трогаем)

```bash
sudo systemctl restart corebot.service
sudo journalctl -u corebot.service -n 80 --no-pager
```

В логах должна появиться строка `TelemetryEmitter started`.

После любых рассылок/входящих — в `Logs` и `Summary` панели появятся данные.

---

## 9) Что пойдёт не так — короткий troubleshooting

### `corebot-cp.service` не стартует
```bash
sudo journalctl -u corebot-cp.service -n 200 --no-pager
```
- `ModuleNotFoundError: control_plane` → проверь `WorkingDirectory=/opt/corebot/app`
  и что код реально лежит в `/opt/corebot/app/control_plane/`.
- Падает на bcrypt/passlib → переустанови: `pip install -U "passlib[bcrypt]"`.
- Падает на `sqlite3.OperationalError: unable to open database` →
  `chown -R corebot:corebot /opt/corebot/app/data` и проверь, что путь в
  `CP_DATABASE_URL` существует.

### `/panel/` отдаёт 404
Скорее всего, `web-panel/` отсутствует на VPS (старая версия репо).
- `git pull` → файлы должны появиться по пути `/opt/corebot/app/web-panel/`.
- `control_plane/main.py` сам монтирует панель только если каталог найден.

### Агент шлёт, в панели пусто
- Посмотри, ходят ли запросы:
  ```bash
  sudo journalctl -u corebot-cp.service -f
  ```
  При живом агенте увидишь `POST /ingest/batch` и `200 OK`.
- `401` → токен устарел/перепутан, прокрути ротацию (раздел 8.1, шаг 3).

### Запутался в портах / правах
- Бот: `corebot.service` → процесс `python main.py`.
- Панель: `corebot-cp.service` → процесс `uvicorn control_plane.main:app` на `127.0.0.1:8081`.
- БД бота: `data/corebot.db`.
- БД панели: `data/control_plane.db`. Это **разные** файлы.

---

## 10) Обновление панели в будущем

Скрипт `scripts/update_corebot.sh` обновляет код и зависимости и
перезапускает только `corebot.service`. Чтобы он также рестартил панель,
добавь в `.bashrc` алиас:

```bash
echo "alias cb-update-all='sudo bash /opt/corebot/app/scripts/update_corebot.sh && sudo systemctl restart corebot-cp.service && sudo systemctl status corebot-cp.service --no-pager -l'" >> ~/.bashrc
source ~/.bashrc
```

После этого:

```bash
cb-update-all
```

обновит и бот, и панель.

---

## 11) Чеклист после первого деплоя

- [x] `systemctl is-active corebot.service` → `active`
- [x] `systemctl is-active corebot-cp.service` → `active`
- [x] `curl http://127.0.0.1:8081/health` → `{"ok":true}`
- [x] `http://127.0.0.1:8081/panel/` через SSH-туннель открывается
- [x] Залогинился под `admin` / новым паролем
- [ ] (если нужен публичный доступ) `https://panel.example.com/panel/` работает
- [x] (если нужна телеметрия) `CP_AGENT_ENABLED=1`, в логах бота `TelemetryEmitter started`
- [ ] В Summary растут `events_24h` после рассылки

---

## 12) Бизнес-модули панели (Аккаунты, Диалоги, AI/Manual, Cleanup)

Поверх ingest/dashboard добавлен второй слой — прямая работа с базой бота
(`corebot.db`). Делается через sync-движок SQLAlchemy (`BOT_DATABASE_URL`).
SQLite в WAL-режиме корректно отдаёт чтение/запись из двух процессов сразу.

### 12.1 Что умеет

* `Аккаунты` (`/panel/#/accounts`) — список аккаунтов из `accounts`,
  переключатель `AI_ACTIVE ↔ MANUAL`. Изменение мгновенно подхватывается
  ботом на ближайшем входящем сообщении (без рестарта).
* `Диалоги` (`/panel/#/dialogs`) — три колонки: аккаунты → диалоги
  (по `neuro_chat_messages`) → лента сообщений конкретного диалога.
* Ручная отправка из ленты диалога — кладёт строку в новую таблицу
  `outbound_queue`. В боте крутится `OutboundConsumer`
  (`workers/outbound_consumer.py`), который раз в ~1.5 с разбирает её и
  шлёт через Telethon тем же `Worker`-ом, что и нейрочат. После успешной
  отправки сообщение пишется в `neuro_chat_messages` (`role='assistant'`)
  — поэтому при возврате аккаунта в `AI_ACTIVE` LLM продолжает диалог
  без потери контекста.
* `Логи` — те же `dashboard/logs`, в новом UI с фильтром по уровню.
* `Настройки` — форма безопасной очистки `neuro_chat_messages`
  и `client_interactions` по фильтрам:
  `account_id` / `peer_user_id` / возраст в днях / классы клиентов
  (`dead`, `bl`, `decline`, …). Поддерживается `dry-run`. Удаление
  идёт батчами (`batch_size`, по умолчанию 500) с отдельным `commit`
  каждого батча — индексы не ломаются, длинных транзакций нет.
* Live-обновления — SSE-канал `GET /business/stream` (опрос БД раз в
  ~1.5 с, новые `neuro_chat_messages` стримятся клиенту). EventSource
  подключается с JWT в URL `?token=…`.

### 12.2 Что **не** удаляется при cleanup

`MailingLog`, `accounts`, `clients` (как сущности), `class_counters`,
`mailings`, прокси, `instance_settings`, warmup-логи, `neuro_action_logs`.
Удаляются только переписки и `client_interactions`, попавшие под фильтр.

Всю логику `cleanup` можно запускать и из CLI на VPS:

```bash
cd /opt/corebot/app
sudo -u corebot ./venv/bin/python -m scripts.cleanup_dialogs --older-days 30 --dry-run
```

### 12.3 Миграции на VPS

После `git pull` бот автоматически применит миграции:

* добавит колонку `accounts.ai_mode` (`AI_ACTIVE` по умолчанию);
* создаст таблицу `outbound_queue` с индексами.

Это уже встроено в `database/repository.py::_run_migrations()`. Никаких
ручных `ALTER TABLE` не нужно.

### 12.4 Безопасность ручной отправки

* Любая запись в `outbound_queue` идёт через JWT-аутентифицированный
  endpoint (`POST /business/accounts/{id}/dialogs/{peer}/send`).
* В очередь складывается `requested_by=<username>` (поле в БД).
* Перед отправкой `OutboundConsumer` проверяет, что соответствующий
  `Worker` подключён; иначе строка получает `status=failed` с понятным
  `error`.
* Длина текста ограничена 4000 символов.

### 12.5 Что ещё в плане (не сделано)

Внутри роадмапа `corebot v2.md`:

* CRUD по рассылкам/клиентам/системным настройкам в UI;
* Soft-delete с архивом (сейчас — hard delete батчами);
* Webhook-интеграция вместо SSE (если потребуется publish/sub);
* Смена пароля админа из UI и приглашение операторов;
* Сборка фронта на Vite/React, если зависимости и роуты вырастут.
