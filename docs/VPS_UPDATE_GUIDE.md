# Инструкция по обновлению CoreBot на VPS (через git)

Эта инструкция описывает, как безопасно обновлять CoreBot:

- на своем VPS;
- на VPS коллег;
- с минимальным простоем и понятной проверкой результата.

## 1) Что за что отвечает

- `git fetch/pull` — подтягивает новый код из репозитория.
- `pip install -r requirements.txt` — ставит/обновляет Python-зависимости под новую версию кода.
- `systemctl restart corebot` — перезапускает бота, чтобы он начал работать на новой версии.
- `systemctl status` и `journalctl` — проверка, что бот запустился без ошибок.
- `.env` — секреты и настройки инстанса; в git не хранится, не перезаписывается при обновлении.

## 2) Обязательные условия перед обновлением

- Бот запущен как `systemd`-сервис (например, `corebot.service`).
- Проект на VPS развернут в git-клоне (например, `/opt/CoreBot`).
- Есть виртуальное окружение (например, `.venv`).
- Все ручные правки на сервере либо закоммичены, либо удалены.

Важно: не редактируйте рабочий код прямо на VPS, иначе `git pull` может конфликтовать.

## 3) Базовое обновление одного VPS

```bash
cd /opt/CoreBot
git fetch origin
git checkout main
git pull --ff-only origin main

source .venv/bin/activate
pip install -r requirements.txt

sudo systemctl restart corebot
sudo systemctl status corebot --no-pager -l
journalctl -u corebot -n 100 --no-pager
```

Если прод-ветка не `main` (например `prod`), подставьте ее вместо `main`.

## 4) Что проверить после обновления

- Сервис в статусе `active (running)`.
- В логах нет traceback и фатальных ошибок.
- Бот отвечает на `/start`.
- Критичные кнопки/сценарии работают (минимум smoke-check в Telegram).

## 5) Обновление VPS коллег (одним скриптом)

На вашей машине можно хранить скрипт, который обновляет все серверы по SSH.

Пример `deploy-all.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

HOSTS=("vps-me" "vps-colleague-1" "vps-colleague-2")

for h in "${HOSTS[@]}"; do
  echo "=== DEPLOY: $h ==="
  ssh "$h" '
    set -euo pipefail
    cd /opt/CoreBot
    git fetch origin
    git checkout main
    git pull --ff-only origin main
    source .venv/bin/activate
    pip install -r requirements.txt
    sudo systemctl restart corebot
    sudo systemctl is-active corebot
  '
done
```

Запуск:

```bash
bash deploy-all.sh
```

## 6) Удобно: локальный deploy-скрипт на каждом VPS

Можно положить на каждый VPS файл `/opt/CoreBot/deploy.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

cd /opt/CoreBot
source .venv/bin/activate
git fetch origin
git checkout main
git pull --ff-only origin main
pip install -r requirements.txt
sudo systemctl restart corebot
sudo systemctl status corebot --no-pager -l
```

Тогда обновление инстанса — одной командой:

```bash
bash /opt/CoreBot/deploy.sh
```

## 7) Рекомендованный порядок релиза

1. Обновить свой VPS.
2. Проверить основные сценарии в Telegram.
3. Обновить VPS коллег.
4. Быстро проверить каждый инстанс (`status` + последние логи).

## 8) Типовые проблемы

- `git pull` ругается на локальные изменения:  
  значит код правили на сервере; сохраните правки в commit/stash или уберите.
- Сервис не поднимается после обновления:  
  сразу смотрите `journalctl -u corebot -n 200`.
- Модуль не найден/ошибка импорта:  
  чаще всего забыли `pip install -r requirements.txt` в активированном venv.

## 9) Безопасность и дисциплина

- Не храните токены в git; только в `.env` на VPS.
- Не давайте доступ к прод-серверам без SSH-ключей и ограничений по пользователям.
- На всех VPS держите одинаковую версию Python и одинаковый процесс деплоя.
