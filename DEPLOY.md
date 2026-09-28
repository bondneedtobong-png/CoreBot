# DEPLOY — запуск локально и на VPS

Единственная инструкция по запуску. Скилл `skills/corebot-vps-deploy` — канонический путь для VPS (его `SKILL.md` + `references/` — источник истины по деталям, здесь — сжатая выжимка).

## 1. Локально (Windows, разработка)

Требования: Python 3.11+, git.

```powershell
git clone <repo> CoreBot; cd CoreBot
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
notepad .env   # заполнить API_ID, API_HASH, BOT_TOKEN, OWNER_ID
```

Запуск:

```bat
start_corebot.bat            :: бот + панель в отдельных окнах + открыть UI
start_corebot.bat --check    :: проверка без запуска (Python, venv, .env)
start_corebot.bat --setup    :: переустановить зависимости и запустить
```

Вручную (два терминала):

```powershell
python main.py
uvicorn control_plane.main:app --host 127.0.0.1 --port 8081 --no-access-log
```

UI: `http://127.0.0.1:8081/panel/`, health: `http://127.0.0.1:8081/health/ready`. Логин панели — `CP_BOOTSTRAP_ADMIN_USERNAME/PASSWORD` из `.env` (не путать с OWNER_ID).

Первые шаги в боте (порядок важен): **прокси → аккаунты (Tdata-ZIP) → клиенты (TXT @username) → тест-рассылка → нейрочат (ключ OpenRouter)**. Аккаунт без рабочего прокси не подключится — так задумано.

Проверка конфига (маскирует секреты):

```powershell
python -m tools.validate_config --mode local --env-file .env        # разработка
python -m tools.validate_config --mode production --env-file .env   # как на VPS, fail-fast
```

Tdata-конвертация требует Python 3.11 (opentele/PyQt5). Если основной venv новее: `TDATA_CONVERTER_PYTHON=C:\путь\к\Python311\python.exe`.

Парсер: ровно один режим. `PARSER_EMBEDDED=0` запускает loop в боте и использует подключённые им Telegram-сессии. Режим `1` запускает loop в панели, если сессии не подключены ботом. Отдельный `python -m workers.parser_worker` используйте только вместо встроенных loops.

## 2. VPS (Ubuntu 22.04/24.04, systemd) — через скилл

Модель: **1 коллега = 1 VPS = 1 `.env` + 1 `corebot.db` + свои `data/sessions/`**. Общее между инстансами — только `API_ID/API_HASH`.

### 2.1 Входные данные скилла

Вызов: `$corebot-vps-deploy` (файлы: `skills/corebot-vps-deploy/SKILL.md`, `references/deployment.md`, `references/troubleshooting.md`, скрипты `scripts/install_corebot.sh`, `verify_corebot.sh`, `backup_corebot.sh`, `restore_corebot.sh`).

Нужно до старта: SSH host/user (root/sudo), исходник (git checkout или каталог в `/tmp/CoreBot`, НЕ клонировать поверх `/opt/corebot/app`), env-файл вне репо с правами `600`.

Обязательные ключи: `API_ID`, `API_HASH`, `BOT_TOKEN`, `OWNER_ID`, `CP_JWT_SECRET`, `CP_BOOTSTRAP_ADMIN_USERNAME`, `CP_BOOTSTRAP_ADMIN_PASSWORD`. Секреты: `openssl rand -hex 32`, в history не светить.

Рекомендуемые пути (дефолт скилла):

```env
DATABASE_URL=sqlite+aiosqlite:////opt/corebot/app/data/corebot.db
BOT_DATABASE_URL=sqlite:////opt/corebot/app/data/corebot.db
CP_DATABASE_URL=sqlite:////opt/corebot/app/data/control_plane.db
PARSER_EMBEDDED=0
COREBOT_ENV=production
```

### 2.2 Workflow (что делает агент по скиллу)

1. Прочитать `references/deployment.md`, осмотреть VPS до изменений.
2. Если `/opt/corebot/app` существует — сначала `scripts/backup_corebot.sh`. Существующий инстанс никогда не трактовать как first install.
3. Зафиксировать один parser-режим (`PARSER_EMBEDDED`).
4. `scripts/install_corebot.sh --dry-run` с целевыми путями, запросить явное подтверждение перед SSH-заливкой, установкой пакетов, записью systemd, рестартами, firewall.
5. Запустить installer как root, затем `scripts/verify_corebot.sh`.
6. Доложить SHA/версию через `scripts/release_status.sh` + живой `GET /version`. Успех — только при `/health/ready` HTTP 200.

Safety-контракт: installer отказывается работать в непустой каталог; `.env`/`data/`/`logs`/`data/sessions/` никогда не перезаписываются; `change-me*`/`admin123` и пустые Telegram-ключи — reject; панель только `127.0.0.1:8081`, порт в UFW не открывать; пути `/opt/corebot/app` + `/opt/corebot/venv` по умолчанию; оба юнита содержат `ExecStartPre=... tools.validate_config --mode production`.

Команды скилла:

```bash
sudo bash scripts/install_corebot.sh --source-dir /tmp/CoreBot --env-file /root/corebot.env
sudo bash scripts/verify_corebot.sh
sudo bash scripts/backup_corebot.sh
sudo bash scripts/restore_corebot.sh --archive /opt/corebot/backups/<corebot-UTC.tar.gz> --target /tmp/restore-drill
bash scripts/release_status.sh
curl --fail http://127.0.0.1:8081/version
curl --fail http://127.0.0.1:8081/health/ready
```

Доступ к панели: только `ssh -L 8081:127.0.0.1:8081 user@VPS` → `http://127.0.0.1:8081/panel/`. Публичный HTTPS — опционально через nginx + certbot с `deny /ingest/` снаружи.

### 2.3 Обновление (pinned SHA, с авто-rollback)

«Последний master без SHA» — запрещено. Только через `scripts/update_corebot.sh --sha <sha>` (или `--tag`), сначала `--dry-run`. `git pull / checkout / reset --hard` на живом `/opt/corebot/app` — запрещены (код подменяется staged `git archive` с excludes `.env/data/logs`).

```bash
cd /opt/corebot/app
git fetch origin
TARGET_SHA="$(git rev-parse origin/main)"
sudo bash scripts/update_corebot.sh --dry-run --sha "$TARGET_SHA"
sudo bash scripts/update_corebot.sh --sha "$TARGET_SHA"
bash scripts/release_status.sh
curl --fail http://127.0.0.1:8081/health/ready
```

Порядок внутри: preflight → backup → stage → deps → stop `corebot-cp`→`corebot` → swap → start `corebot-cp`→`corebot` → readiness (`/health/live`+`/health/ready` 200) + паритет `/version` == target. Exit: `0` ok/no-op, `2` до swap (ничего не тронуто), `3` откат выполнен, `4` откат неполный (ручное восстановление, бэкап и SHA напечатаны).

Быстрый цикл с ПК: `scripts\cb_push.cmd "msg"` (add+commit+push) → на VPS update по SHA выше.

### 2.4 Бэкап / restore

Состав: `.env`, `data/corebot.db`, `data/control_plane.db`, `data/sessions/` (+ `data/neuro/`, `logs/` по возможности). SQLite — только через `.backup` API (`backup_lib.py` / `backup_corebot.sh`), никогда живой копией `.db-wal`. Архив `/opt/corebot/backups/corebot-<UTC>.tar.gz` (600, каталог 700) + `<архив>.sha256` + `backup_manifest.json` + cron-метка `.last_backup_ok`. Retention 7 шт / 30 дней, последний успешный не удаляется. Расписание: `systemd/corebot-backup.{service,timer}` (03:17 + jitter 15 мин). Restore — только в пустой каталог (`restore_corebot.sh --archive … --target <empty-dir>` + `integrity_check`), перезапись живого инстанса запрещена. Drill — раз в 90 дней. SLO: RPO ≤24ч, RTO same-host ≤60 мин, new-host ≤4ч.

### 2.5 Диагностика

```bash
systemctl status corebot.service corebot-cp.service --no-pager -l
journalctl -u corebot.service -u corebot-cp.service -n 200 --no-pager
curl -i http://127.0.0.1:8081/health/ready
ss -ltnp | grep 8081   # должен быть 127.0.0.1:8081, не 0.0.0.0
```

Частое: ready 503 — права/пути обеих БД; парсер двоится — два parser-режима; `database is locked` — дубли процессов; `detected dubious ownership` — `git config --global --add safe.directory /opt/corebot/app`; 401 ingest — ротировать токен агента. Детали — `skills/corebot-vps-deploy/references/troubleshooting.md`.
