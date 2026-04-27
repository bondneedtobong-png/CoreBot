# HELP.md — шпаргалка команд CoreBot

Практический справочник того, чем реально пользуешься в работе. Сгруппировано по сценариям. Полные процедуры — в `RUNBOOK.md` и `docs/`.

---

## Оглавление

- [1. Обновление прод-инстанса (самое частое)](#1-обновление-прод-инстанса-самое-частое)
- [2. Локально на ПК (Windows)](#2-локально-на-пк-windows)
- [3. SSH в VPS](#3-ssh-в-vps)
- [4. Веб-панель: доступ + диагностика](#4-веб-панель-доступ--диагностика)
- [5. Бот: статус, логи, рестарт](#5-бот-статус-логи-рестарт)
- [6. База данных](#6-база-данных)
- [7. Бэкапы и восстановление](#7-бэкапы-и-восстановление)
- [8. Тесты, линт, локальный запуск](#8-тесты-линт-локальный-запуск)
- [9. Прокси и сети](#9-прокси-и-сети)
- [10. Telegram-бот: операторские команды](#10-telegram-бот-операторские-команды)
- [11. Диагностика типичных «не работает»](#11-диагностика-типичных-не-работает)
- [12. Скрипты репозитория — что делает каждый](#12-скрипты-репозитория--что-делает-каждый)

---

## 1. Обновление прод-инстанса (самое частое)

**Каноничный порядок: сначала `git push` локально → потом `git pull` на VPS.** Не наоборот: иначе свои локальные правки придётся мержить руками на сервере.

### Быстрый путь (две команды на двух машинах)

#### А. На ПК (Windows CMD/PowerShell)

```cmd
cd /d C:\Users\bond\Desktop\CoreBot
scripts\cb_push.cmd "коммит-сообщение"
```

`cb_push.cmd` делает: `git add -A` → `git commit -m "..."` → `git push origin main`. Если staged-изменений нет — коммит пропускается (push всё равно отработает).

С другой веткой:

```cmd
scripts\cb_push.cmd "коммит-сообщение" prod
```

#### B. На VPS (через SSH)

```bash
ssh root@<VPS_IP>
cb-update            # alias на scripts/update_corebot.sh, см. ниже
```

`update_corebot.sh` делает:
1. `git fetch origin` + `git checkout main` + `git pull --ff-only origin main`
2. `pip install -r requirements.txt` в venv
3. `pip cache purge` (мягко, без падения)
4. `systemctl restart corebot.service`
5. печатает `status` и `journalctl -n 120`

**Если панель тоже обновилась** — рестартни и её:

```bash
sudo systemctl restart corebot-cp.service
sudo systemctl status  corebot-cp.service --no-pager -l
```

Или сразу обе сервиса одной командой (alias из `docs/WEBPANEL_DEPLOY.md` §10):

```bash
cb-update-all
```

### Установка алиасов (один раз)

```bash
echo "alias cb-update='sudo bash /opt/corebot/app/scripts/update_corebot.sh'" >> ~/.bashrc
echo "alias cb-update-all='sudo bash /opt/corebot/app/scripts/update_corebot.sh && sudo systemctl restart corebot-cp.service && sudo systemctl status corebot-cp.service --no-pager -l'" >> ~/.bashrc
source ~/.bashrc
```

### Длинный путь (без алиасов и скриптов)

```bash
cd /opt/corebot/app
git config --global --add safe.directory /opt/corebot/app   # один раз
git fetch origin
git checkout main
git pull --ff-only origin main
/opt/corebot/venv/bin/pip install -r requirements.txt
sudo systemctl restart corebot.service
sudo systemctl restart corebot-cp.service
sudo systemctl status  corebot.service --no-pager -l
journalctl -u corebot.service -n 120 --no-pager
```

Подробнее (включая обновление другой ветки и `BRANCH=prod`) — в `docs/VPS_UPDATE_GUIDE.md`.

---

## 2. Локально на ПК (Windows)

### Git: посмотреть/закоммитить/откатить

```cmd
git status
git diff
git diff --cached
git log --oneline -20

git checkout -- <file>            :: откатить незакоммиченный файл
git restore --staged <file>       :: убрать из stage
git reset --hard origin/main      :: жёстко выровняться по удалёнке (потеряешь локальное!)
```

### Виртуальное окружение

```cmd
python -m venv venv311
venv311\Scripts\activate
pip install -r requirements.txt
```

### Запуск бота локально

```cmd
venv311\Scripts\activate
python main.py
```

Запуск веб-панели локально (`http://127.0.0.1:8081/panel/`):

```cmd
venv311\Scripts\activate
uvicorn control_plane.main:app --host 127.0.0.1 --port 8081 --reload
```

---

## 3. SSH в VPS

### Базовое подключение

```cmd
ssh root@<VPS_IP>
```

### SSH-туннель к веб-панели (стандартный способ открыть UI)

```cmd
ssh -L 8081:127.0.0.1:8081 root@<VPS_IP>
```

Пока окно SSH открыто — браузер: `http://127.0.0.1:8081/panel/`. Порт `8081` наружу VPS **не открывается**, доступ только через туннель (или nginx+HTTPS, см. `docs/WEBPANEL_DEPLOY.md` §7).

### Копирование файлов

```cmd
:: ПК → VPS
scp .env root@<VPS_IP>:/opt/corebot/app/.env
scp -r data\sessions root@<VPS_IP>:/opt/corebot/app/data/

:: VPS → ПК
scp root@<VPS_IP>:/opt/corebot/app/data/corebot.db .\backup\
```

---

## 4. Веб-панель: доступ + диагностика

### Открыть UI

1. SSH-туннель: `ssh -L 8081:127.0.0.1:8081 root@<VPS_IP>`
2. Браузер: `http://127.0.0.1:8081/panel/`
3. Логин/пароль из `.env`: `CP_BOOTSTRAP_ADMIN_USERNAME` / `CP_BOOTSTRAP_ADMIN_PASSWORD`

### Smoke на VPS (без браузера)

```bash
curl -s http://127.0.0.1:8081/health
# {"ok":true}

curl -s http://127.0.0.1:8081/panel/ -o /tmp/panel.html
head -5 /tmp/panel.html
# <title>CoreBot Control Panel</title>
```

### Логи панели

```bash
sudo systemctl status corebot-cp.service --no-pager -l
journalctl -u corebot-cp.service -n 150 --no-pager
journalctl -u corebot-cp.service -f                 # live tail
```

### Рестарт только панели

```bash
sudo systemctl restart corebot-cp.service
```

### Проверка JWT-логина из CLI (например, чтобы выпустить агентский токен)

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8081/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<пароль>"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
echo "$TOKEN"
```

Дальше — см. `docs/WEBPANEL_DEPLOY.md` §8 (создание агента и токена).

---

## 5. Бот: статус, логи, рестарт

```bash
sudo systemctl status corebot.service --no-pager -l
sudo systemctl restart corebot.service
sudo systemctl stop    corebot.service
sudo systemctl start   corebot.service

journalctl -u corebot.service -n 200 --no-pager       # последние 200 строк
journalctl -u corebot.service -f                      # live tail
journalctl -u corebot.service --since "1 hour ago"    # за последний час
journalctl -u corebot.service --since "today" | tail -200
```

Файловые логи (отдельно от systemd-журнала):

```bash
tail -f /opt/corebot/app/logs/corebot.log
tail -f /opt/corebot/app/logs/error.log
```

---

## 6. База данных

Бот хранит всё в одном файле SQLite (бизнес — `corebot.db`, веб-панель — `control_plane.db`). Оба в WAL-режиме, читать можно живьём, запись — лучше когда сервис остановлен (либо короткими запросами).

### Быстрые запросы

```bash
sqlite3 /opt/corebot/app/data/corebot.db
```

```sql
.tables
.schema accounts

SELECT count(*) FROM accounts;
SELECT count(*) FROM clients;
SELECT count(*) FROM mailings;
SELECT count(*) FROM neuro_chat_messages;
SELECT count(*) FROM outbound_queue WHERE status='pending';

-- топ-аккаунты по диалогам
SELECT account_id, count(DISTINCT peer_user_id)
FROM neuro_chat_messages GROUP BY account_id ORDER BY 2 DESC LIMIT 10;

-- зависшая очередь ручных
SELECT id, account_id, peer_user_id, attempts, status, error
FROM outbound_queue WHERE status IN ('pending','sending','failed') ORDER BY id DESC LIMIT 20;
```

Выйти: `.exit`.

### Безопасная массовая чистка переписок (CLI)

```bash
cd /opt/corebot/app
sudo -u corebot ./venv/bin/python -m scripts.cleanup_dialogs --older-days 30 --dry-run
sudo -u corebot ./venv/bin/python -m scripts.cleanup_dialogs --classes dead,bl,decline
sudo -u corebot ./venv/bin/python -m scripts.cleanup_dialogs --account 5 --peer 12345678
```

Удаляются только `neuro_chat_messages` и `client_interactions` под фильтр. Остальное (`accounts`, `clients`, `mailings`, прокси, классы) — не трогается. То же самое доступно из веб-панели в разделе «Настройки» → cleanup (с режимом `archive` для soft-delete).

### Авто-миграции

Никакого Alembic. При старте бота (`main.py`) и панели (`control_plane/main.py`) выполняется `_run_migrations()` — `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE … ADD COLUMN` для всех новых полей. Ничего вручную ALTER-ить не надо.

---

## 7. Бэкапы и восстановление

### Снять бэкап (минимальный набор)

На VPS:

```bash
tar -czvf /opt/corebot/corebot-data-$(date +%F).tar.gz \
  -C /opt/corebot/app data .env
```

Что обязательно бэкапить:
- `data/corebot.db` — основная база
- `data/control_plane.db` — пользователи панели, ingest
- `data/sessions/` — Telethon-сессии аккаунтов **(нет копии — нет аккаунтов)**
- `data/neuro/` — кастомные system-промпты
- `.env` — секреты

### Восстановление (ПК → VPS)

```cmd
set VPS=<IP_СЕРВЕРА>
scp ".env"                                                root@%VPS%:/opt/corebot/app/.env
scp "data\corebot.db"                                     root@%VPS%:/opt/corebot/app/data/corebot.db
scp -r "data\sessions"                                    root@%VPS%:/opt/corebot/app/data/
scp -r "data\neuro"                                       root@%VPS%:/opt/corebot/app/data/
scp -r "data\files"                                       root@%VPS%:/opt/corebot/app/data/
```

И на VPS — выровнять права:

```bash
sudo systemctl stop corebot.service
sudo chown -R corebot:corebot /opt/corebot/app
sudo chmod 600 /opt/corebot/app/.env
sudo find /opt/corebot/app/data -type d -exec chmod 755 {} \;
sudo find /opt/corebot/app/data -type f -exec chmod 644 {} \;
sudo systemctl start corebot.service
sudo systemctl status corebot.service --no-pager -l
```

Подробности и checklists — в `RUNBOOK.md` §11.

---

## 8. Тесты, линт, локальный запуск

```cmd
:: PowerShell / CMD
venv311\Scripts\activate

python -m pytest tests/ -q                          :: весь набор
python -m pytest tests/test_backlog_fixes.py -q     :: только бэклог-тесты
python -m pytest -k "neurochat" -q                  :: подмножество по имени

python -m pyflakes bot services workers control_plane utils database
```

Sanity-check фронтенда (без сборщика, чистый JS):

```cmd
node -c web-panel/main.js
```

In-process smoke веб-панели (без uvicorn, без сети):

```cmd
python -c "from fastapi.testclient import TestClient; from control_plane.main import app; c=TestClient(app); print(c.get('/health').json())"
```

---

## 9. Прокси и сети

### Проверить, что VPS видит свои порты

```bash
ss -tlnp | grep -E '8081|443|80'
```

Должно быть `127.0.0.1:8081` (панель) и опционально `0.0.0.0:443`/`80` (если поднят nginx). Если `0.0.0.0:8081` — это плохо: `ufw` всё равно режет, но безопаснее держать `127.0.0.1`.

### TCP-проверка прокси без Telegram (как в кнопке «Тест» в панели)

```bash
nc -zv -w 5 <proxy_host> <proxy_port>     # Linux
```

Глубокая проверка (SOCKS5 + Exit IP) — внутри бота: `Прокси → выбрать → Проверить`.

### Проверка HTTPS-сертификата (если поднят nginx)

```bash
sudo certbot certificates
sudo nginx -t
sudo systemctl reload nginx
```

---

## 10. Telegram-бот: операторские команды

В Telegram-чате с ботом (доступно только `OWNER_ID` из `.env`):

| Команда | Что делает |
|---------|-----------|
| `/start` | Открыть главное меню |
| `/help`  | Краткая справка по разделам |
| `/status` | Сводка по системе (аккаунты, рассылки, нейрочат, очередь) |

Дальше — навигация только по inline-кнопкам: `Аккаунты`, `Прокси`, `Клиенты`, `Рассылка`, `Нейрочаттинг`, `База данных`.

---

## 11. Диагностика типичных «не работает»

### `corebot.service` падает после `git pull`

```bash
journalctl -u corebot.service -n 200 --no-pager | grep -i -E 'error|traceback'
```

Чаще всего:
- забыли `pip install -r requirements.txt` после обновления — `ModuleNotFoundError`
- кривой `.env` — `pydantic` ругается на конкретное поле
- `Permission denied` на `data/` — `chown -R corebot:corebot /opt/corebot/app`

### Веб-панель открывается, но «пустая»

1. Открыть DevTools → Network. Проверить, что `/business/accounts` отдаёт 200 и валидный JSON.
2. Проверить, что в `.env` стоит `BOT_DATABASE_URL=sqlite:////opt/corebot/app/data/corebot.db` (без `+aiosqlite`).
3. `journalctl -u corebot-cp.service -n 100 --no-pager` — ищи `OperationalError: unable to open database file`.

### Ручная отправка из веба зависает в `pending`

```sql
SELECT id, status, attempts, error, next_attempt_at FROM outbound_queue ORDER BY id DESC LIMIT 20;
```

- Если `status='failed'` и `error='worker not connected'` после 5 попыток — посмотри в `journalctl -u corebot.service` строки `Worker.connect`. Возможно, аккаунт не авторизован (`unauthorized`).
- Если `status='pending'` и `next_attempt_at` в будущем — это нормально, экспоненциальный бэк-офф (см. `workers/outbound_consumer.py`).

### Нейрочат не отвечает

Лог-фильтр на причины deny:

```bash
journalctl -u corebot.service --since "1 hour ago" | grep -i 'Neuro incoming denied'
```

Возможные `reason`:
- `global_disabled` → в панели «Настройки → Инстанс» или `.env: NEUROCHAT_ENABLED=0`
- `mailing_local_disabled` → у конкретной рассылки выключен нейрочат
- `worker_disconnected` → аккаунт офлайн / не авторизован
- `client_class_bl` → клиент в чёрном списке

### `detected dubious ownership` от git на VPS

```bash
git config --global --add safe.directory /opt/corebot/app
```

---

## 12. Скрипты репозитория — что делает каждый

| Скрипт | Где запускать | Что делает |
|--------|---------------|------------|
| `scripts/cb_push.cmd` | ПК (CMD) | `git add -A` → `git commit -m "..."` → `git push origin <branch>`. Безопасно вызывать когда staged-изменений нет. |
| `scripts/update_corebot.sh` | VPS (`sudo`) | git pull → `pip install -r requirements.txt` → `pip cache purge` → `systemctl restart corebot.service` → status + последние логи. Поддерживает `BRANCH=prod`. |
| `scripts/init_env.sh` | VPS, при первом деплое | Копирует `.env.example` → `.env`, спрашивает `BOT_TOKEN` и `OWNER_ID`, проверяет наличие `API_ID/API_HASH`, ставит `chmod 600`. |
| `scripts/cleanup_dialogs.py` | VPS, через `python -m` | Безопасная батч-чистка `neuro_chat_messages` + `client_interactions` по фильтрам (`--older-days`, `--account`, `--peer`, `--classes`, `--dry-run`). |
| `scripts/build_neurochat.py` | ПК | Сборка единого файла промпта/документации нейрочата из чанков (служебное). |
| `scripts/assemble_neurochat.py` | ПК | Дополнительный шаг сборки нейрочата (служебное, см. исходник). |

---

## Что читать дальше

- `README.md` — обзор проекта и возможностей
- `RUNBOOK.md` — пошаговый деплой нового VPS с нуля
- `docs/VPS_UPDATE_GUIDE.md` — полный разбор обновления (включая `BRANCH=prod`)
- `docs/WEBPANEL_DEPLOY.md` — разворачивание панели на уже работающем боте
- `docs/ARCHITECTURE.md` — структура кода
- `docs/BACKLOG.md` — что ещё не сделано
- `docs/DATABASE_MODULE_SPEC.md` — спецификация классовой системы
- `corebot v2.md` — рабочий журнал и дорожная карта
