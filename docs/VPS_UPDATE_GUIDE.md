# Инструкция по обновлению CoreBot на VPS (контракт задачи 06 + fleet-путь 07)

> Контракт обновления: pinned SHA/тег через `scripts/update_corebot.sh`
> (preflight → backup → stage → deps → stop/swap cp→bot → readiness +
> version-паритет → success | авторолбэк, exit 0/2/3/4, `--dry-run`, no-op).
> Прямые `git pull` / `git checkout` / `git reset --hard` на живом
> `/opt/corebot/app` ЗАПРЕЩЕНЫ (код подменяется только staged-экспортом
> `git archive` с excludes `.env`/`data`/`logs`). Для парка из нескольких VPS
> тот же скрипт вызывается через Ansible: `docs/operations/FLEET_OPERATIONS.md`
> (canary 1 → batch, провал canary блокирует очередь). Ниже — путь одного VPS.

## 1) Важный факт про авто-синк

Автоматического подтягивания кода из GitHub **нет**.  
Обновление делается вручную командами (или скриптом ниже).

## 2) Порядок обновления (строго, контракт задачи 06)

1. Зафиксировать целевой SHA/тег (никогда «последний master без SHA»).
2. Вызвать `scripts/update_corebot.sh --sha <sha>` (или `--tag <tag>`):
   скрипт сам делает preflight (validator production), backup, staged swap,
   зависимости, рестарт `corebot-cp` → `corebot`, readiness-gate и паритет
   `/version` с авто-откатом при провале.
3. Проверить статус/версию (`scripts/release_status.sh`, `/health/ready`).

## 3) Обновление вручную (pinned SHA, без pull на живом каталоге)

```bash
cd /opt/corebot/app

# Фикс "detected dubious ownership" (без ручной настройки каждый раз)
git config --global --add safe.directory /opt/corebot/app

# 1) Зафиксировать целевой SHA (пример: origin/main) — код НЕ трогаем pull'ом
git fetch origin
TARGET_SHA="$(git rev-parse origin/main)"

# 2) Обновление — только через скрипт (staged git archive, живой checkout не трогается)
sudo bash /opt/corebot/app/scripts/update_corebot.sh --sha "$TARGET_SHA"

# 3) Проверка
bash scripts/release_status.sh
curl --fail http://127.0.0.1:8081/health/ready
```

Запрещено на живом `/opt/corebot/app`: `git pull`, `git checkout <ветка>`,
`git reset --hard`. Обновление «на последний master без фиксации SHA»
запрещено релизным контрактом — всегда фиксируйте SHA/тег.

## 4) Ошибка `detected dubious ownership`

Причина: git видит, что текущий пользователь и владелец каталога репозитория не совпадают.  
Стабильное решение:

```bash
git config --global --add safe.directory /opt/corebot/app
```

Проверить, что правило добавилось:

```bash
git config --global --get-all safe.directory
```

Если репозиторий принадлежит `corebot`, можно запускать git-команды от него:

```bash
sudo -u corebot -H bash -lc 'cd /opt/corebot/app && git fetch origin && git pull --ff-only origin main'
```

## 5) One-command обновление (скрипт)

В репозитории есть скрипт: `scripts/update_corebot.sh`.

### Установка на VPS

```bash
cd /opt/corebot/app
chmod +x scripts/update_corebot.sh
```

### Запуск

```bash
# Pinned SHA (основной путь, релизный контракт):
sudo bash /opt/corebot/app/scripts/update_corebot.sh --sha <полный-SHA>
# По тегу:
sudo bash /opt/corebot/app/scripts/update_corebot.sh --tag v1.4.0
# Проверка плана без изменений:
sudo bash /opt/corebot/app/scripts/update_corebot.sh --sha <SHA> --dry-run
```

Коды завершения: `0` — успех / no-op (уже на этом SHA) / dry-run;
`2` — ошибка до swap (ничего не изменено); `3` — провал с выполненным
авто-откатом; `4` — откат неполный, ручное восстановление по RUNBOOK §17
(бэкап и SHA напечатаны скриптом).

По умолчанию:
- `APP_DIR=/opt/corebot/app`
- `VENV_PY=/opt/corebot/venv/bin/python`
- сервисы: сначала `corebot-cp.service`, затем `corebot.service`
- readiness: `/health/live` + `/health/ready` на `127.0.0.1:8081`

Флаг `--branch` оставлен только как резолвер SHA для совместимости;
прямое обновление «ветки без SHA» контрактом запрещено.

## 6) Что делает скрипт (8 шагов задачи 06)

1. `STEP 1/8 preflight`: validator production + git + Python 3.11+ + диск,
   резолв target SHA, no-op если код и `.deployed_sha` уже на нём.
2. `STEP 2/8 backup`: обязательный предобновленческий tar в
   `/opt/corebot/backups/` (проверен на непустоту).
3. `STEP 3/8 stage`: `git archive <sha>` во временный каталог (живой
   checkout не трогается), манифест `RELEASE.json` на staged-код.
4. `STEP 4/8 dependencies`: `pip install -r <stage>/requirements.txt`.
5. `STEP 5/8 stop-swap`: стоп `corebot-cp` → `corebot`, затем rsync swap
   с excludes `.git .env data logs RELEASE.json .deployed_sha`.
6. `STEP 6/8 start`: старт `corebot-cp` → `corebot`.
7. `STEP 7/8 readiness-gate`: `/health/live` + `/health/ready` HTTP 200
   в бюджет + паритет live `/version` == target SHA (запрет состояния
   «новый бот + старый CP»).
8. `STEP 8/8 success | rollback`: запись `.deployed_sha` либо авто-откат
   (предыдущий код + `.env`/`data` из бэкапа + рестарт + gate).

## 7) Минимальный smoke после обновления

```bash
sudo systemctl status corebot.service --no-pager -l
journalctl -u corebot.service -n 120 --no-pager
```

И в Telegram:
- `/start`
- открыть `Рассылка`
- открыть `Нейрочаттинг`

Если есть traceback в `journalctl` — фиксировать до обновления следующего VPS.

## 8) Alias одной короткой командой (`cb-update`)

Добавь alias (для root):

```bash
echo "alias cb-update='sudo bash /opt/corebot/app/scripts/update_corebot.sh'" >> ~/.bashrc
source ~/.bashrc
```

Теперь обновление запускается так (SHA обязателен):

```bash
cb-update --sha <полный-SHA>
```

Если работаешь под другим пользователем (не root), добавь alias в его `~/.bashrc`.

> Ветка через `BRANCH=` больше не является путём обновления: используй
> `--sha`/`--tag` (см. §5). Alias ниже — только для короткого вызова скрипта.

## 9) One-command commit + push в GitHub (Windows CMD)

Добавлен скрипт: `scripts/cb_push.cmd`

### Запуск (из папки проекта на локальном ПК)

```cmd
cd /d C:\Users\bond\Desktop\CoreBot
scripts\cb_push.cmd "update text here"
```

По умолчанию пушит в `main`.  
Если нужна другая ветка:

```cmd
scripts\cb_push.cmd "update text here" prod
```

### Что делает скрипт

1. Проверяет, что ты в git-репозитории.
2. Делает `git add -A`.
3. Делает `git commit -m "..."`.
4. Делает `git push origin <branch>`.
5. Показывает `git status -sb`.

## 10) Полный порядок команд (CMD -> GitHub -> VPS)

### A. Локально (CMD на твоем ПК)

```cmd
cd /d C:\Users\bond\Desktop\CoreBot
scripts\cb_push.cmd "your commit message"
```

### B. На VPS (SSH)

#### B1. Нормальный путь (контракт задачи 06)

```bash
cd /opt/corebot/app
git config --global --add safe.directory /opt/corebot/app
git fetch origin
TARGET_SHA="$(git rev-parse origin/main)"
sudo bash /opt/corebot/app/scripts/update_corebot.sh --sha "$TARGET_SHA"
bash scripts/release_status.sh
curl --fail http://127.0.0.1:8081/health/ready
```

Для парка из нескольких VPS тот же скрипт раскатывается через Ansible
(canary 1 → batch): `docs/operations/FLEET_OPERATIONS.md`, §4.

#### B2. Bootstrap (если `No such file or directory` на `update_corebot.sh`)

Одноразовый путь для очень старых checkout'ов без скрипта (после него —
только B1). Код подтягивается во временный каталог и синхронизируется
без `git pull`/`reset --hard` на живом дереве:

```bash
cd /opt/corebot/app
git config --global --add safe.directory /opt/corebot/app
git fetch origin
TARGET_SHA="$(git rev-parse origin/main)"
mkdir -p /tmp/bootstrap && git archive "$TARGET_SHA" | tar -x -C /tmp/bootstrap
rsync -a --delete --checksum --exclude /.git --exclude /.env --exclude /data \
  --exclude /logs /tmp/bootstrap/ /opt/corebot/app/
/opt/corebot/venv/bin/pip install -r requirements.txt
sudo systemctl restart corebot-cp.service
sudo systemctl restart corebot.service
```

После этого скрипт уже появится, и дальше используй только B1.

### C. Проверка на VPS

```bash
sudo systemctl status corebot.service --no-pager -l
journalctl -u corebot.service -n 120 --no-pager
```

Если на шаге B1 видишь `No such file or directory` для `update_corebot.sh`, выполни B2 один раз.
