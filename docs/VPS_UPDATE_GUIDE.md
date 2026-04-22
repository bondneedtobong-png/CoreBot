# Инструкция по обновлению CoreBot на VPS (через git)

## 1) Важный факт про авто-синк

Автоматического подтягивания кода из GitHub **нет**.  
Обновление делается вручную командами (или скриптом ниже).

## 2) Порядок обновления (строго)

1. Обновить код из git.  
2. Обновить зависимости Python.  
3. Перезапустить `systemd` сервис и проверить статус/логи.

## 3) Обновление вручную (готово к копированию)

```bash
cd /opt/corebot/app

# Фикс "detected dubious ownership" (без ручной настройки каждый раз)
git config --global --add safe.directory /opt/corebot/app

# 1) Код
git fetch origin
git checkout main
git pull --ff-only origin main

# 2) Зависимости
/opt/corebot/venv/bin/pip install -r requirements.txt

# 3) Перезапуск + проверка
sudo systemctl restart corebot.service
sudo systemctl status corebot.service --no-pager -l
journalctl -u corebot.service -n 150 --no-pager
```

Если используешь ветку не `main`, замени ее в `checkout/pull`.

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
sudo bash /opt/corebot/app/scripts/update_corebot.sh
```

По умолчанию:
- `APP_DIR=/opt/corebot/app`
- `VENV_PY=/opt/corebot/venv/bin/python`
- `SERVICE=corebot.service`
- `BRANCH=main`

С кастомной веткой:

```bash
sudo BRANCH=prod bash /opt/corebot/app/scripts/update_corebot.sh
```

## 6) Что делает скрипт

1. Проверяет, что каталог репозитория существует.  
2. Автоматически добавляет `safe.directory` (fix dubious ownership).  
3. Делает `git fetch` + `git checkout` + `git pull --ff-only`.  
4. Обновляет зависимости через `venv` python: `python -m pip install -r requirements.txt`.  
5. Чистит pip-кэш (`python -m pip cache purge`, мягко, без падения если не вышло).  
6. Перезапускает сервис и показывает `status` + последние логи.

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

Теперь обновление запускается так:

```bash
cb-update
```

Если нужно обновлять из другой ветки:

```bash
BRANCH=prod cb-update
```

Если работаешь под другим пользователем (не root), добавь alias в его `~/.bashrc`.

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

```bash
cd /opt/corebot/app
git config --global --add safe.directory /opt/corebot/app
sudo bash /opt/corebot/app/scripts/update_corebot.sh
```

### C. Проверка на VPS

```bash
sudo systemctl status corebot.service --no-pager -l
journalctl -u corebot.service -n 120 --no-pager
```

Если на шаге B видишь `No such file or directory` для `update_corebot.sh`, значит этот файл еще не доехал до GitHub (забыл сделать `cb_push` локально).
