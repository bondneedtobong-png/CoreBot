# RUNBOOK: как подключать коллег к CoreBot (подробно, для первого деплоя)

Этот документ — практическая инструкция для вашей модели работы:

- у каждого коллеги свой VPS;
- вы админ и деплоите сами;
- коллегам не выдаётся исходный код;
- у каждого отдельный бот, отдельная база, отдельные сессии;
- стек: `venv + systemd`.

ОС по инструкции: **Ubuntu 22.04/24.04 LTS**.

---

## 0) Что вы получаете на выходе

После выполнения инструкции у коллеги будет:

1. запущенный `corebot.service` (основной бот);
2. опционально `corebot-cp.service` (локальная веб-панель + ingest на `127.0.0.1:8081`);
3. изолированные данные в `/opt/corebot/app/data/`;
4. доступ к управлению ботом только у владельца `OWNER_ID`.

---

## 1) Главный принцип изоляции (чтобы ничего не смешивалось)

Для каждого коллеги — **свой VPS** и на нём:

- свой `.env` с уникальным `BOT_TOKEN` и `OWNER_ID`;
- своя БД (`corebot.db`);
- свой каталог сессий (`data/sessions/`);
- свои systemd-сервисы.

Если так делать всегда, сессии и клиенты между коллегами не пересекаются.

---

## 2) Что запросить у коллеги заранее (шаблон сообщения)

Скопируйте и отправьте коллеге:

```text
Для запуска вашего личного CoreBot пришлите:
1) BOT_TOKEN (от @BotFather, новый бот под вас)
2) ваш Telegram ID (число, не username)
3) SSH-доступ к вашему VPS:
   - IP
   - порт SSH
   - пользователь
   - способ входа (ssh key / пароль)
4) ОС на VPS (желательно Ubuntu 22.04/24.04)
5) (Опционально) прокси для control bot
6) (Опционально) ваш OPENROUTER_API_KEY для нейрочата
```

Важно: `API_ID/API_HASH` можете использовать ваши общие.

---

## 3) Карточка инстанса (ведите учёт на 1 коллегу = 1 запись)

Создайте у себя таблицу (Notion/Google Sheet/файл):

- `tenant_name` (имя коллеги);
- `vps_ip`;
- `ssh_user`;
- `bot_username`;
- `BOT_TOKEN` (секретно);
- `OWNER_ID`;
- `deploy_date`;
- `corebot_version` (коммит/тег);
- `status` (active/paused/offboarded);
- `backup_last_date`.

Это экономит много времени в поддержке.

---

## 4) Первый деплой (пошагово, без пропусков)

### Шаг 4.1. Подключиться к VPS и обновить систему

```bash
ssh <user>@<VPS_IP>
sudo -i
apt update && apt upgrade -y
apt install -y git ufw
```

### Шаг 4.2. Установить Python 3.11+

Ubuntu 24.04:

```bash
apt install -y python3 python3-venv python3-pip
python3 --version
```

Ubuntu 22.04 (если `python3` < 3.11):

```bash
apt install -y software-properties-common
add-apt-repository -y ppa:deadsnakes/ppa
apt update
apt install -y python3.11 python3.11-venv python3.11-dev
python3.11 --version
```

### Шаг 4.3. Создать системного пользователя приложения

```bash
useradd -r -m -s /bin/bash -d /opt/corebot corebot
mkdir -p /opt/corebot
chown -R corebot:corebot /opt/corebot
```

### Шаг 4.4. Настроить firewall

```bash
ufw allow OpenSSH
ufw --force enable
ufw status
```

Не открывайте `8081` наружу. Панель будет локальной.

### Шаг 4.5. Развернуть код

Вариант A (git):

```bash
sudo -u corebot -H bash -c '
cd /opt/corebot
git clone https://github.com/bondneedtobong-png/CoreBot app
'
```

Вариант B (архив):

- загрузить архив проекта в `/opt/corebot`;
- распаковать в `/opt/corebot/app`;
- убедиться в владельце:

```bash
chown -R corebot:corebot /opt/corebot/app
```

### Шаг 4.6. Создать virtualenv и поставить зависимости

```bash
sudo -u corebot -H bash -c '
cd /opt/corebot
python3.11 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r app/requirements.txt
'
```

Если на сервере только `python3.11`, замените `python3` на `python3.11`.

### Шаг 4.7. Создать `.env` и заполнить секреты

```bash
sudo -u corebot bash -c '
cd /opt/corebot/app
bash scripts/init_env.sh .env
'
```

По умолчанию логика такая:

- `API_ID` / `API_HASH` — общий Telegram app для всей команды (заполняется один раз в шаблоне).
- `BOT_TOKEN` / `OWNER_ID` — индивидуально для каждого инстанса.

Рекомендуемые строки (лучше вставить сразу):

```env
DATABASE_URL=sqlite+aiosqlite:////opt/corebot/app/data/corebot.db
CP_DATABASE_URL=sqlite:////opt/corebot/app/data/control_plane.db
CP_AGENT_ENABLED=0
CP_JWT_SECRET=<очень_длинный_секрет>
CP_BOOTSTRAP_ADMIN_USERNAME=admin
CP_BOOTSTRAP_ADMIN_PASSWORD=<сложный_пароль>
```

### Шаг 4.8. Проверить права

```bash
chown -R corebot:corebot /opt/corebot/app
```

---

## 5) Настройка systemd

### 5.1. Control Plane service (рекомендуется для будущей панели)

Создайте файл `/etc/systemd/system/corebot-cp.service`:

```ini
[Unit]
Description=CoreBot Control Plane (FastAPI)
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

Запуск:

```bash
systemctl daemon-reload
systemctl enable --now corebot-cp.service
systemctl status corebot-cp.service
curl -s http://127.0.0.1:8081/health
```

Если health вернул `{"ok": true}` — всё хорошо.

### 5.2. Основной бот service

Создайте файл `/etc/systemd/system/corebot.service`:

```ini
[Unit]
Description=CoreBot Telegram (main.py)
After=network.target
After=corebot-cp.service

[Service]
Type=simple
User=corebot
Group=corebot
WorkingDirectory=/opt/corebot/app
EnvironmentFile=/opt/corebot/app/.env
ExecStart=/opt/corebot/venv/bin/python main.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Запуск:

```bash
systemctl daemon-reload
systemctl enable --now corebot.service
systemctl status corebot.service
```

Важно: запускайте именно `main.py` (корень проекта), а не `python -m bot.main`.
`main.py` выполняет инициализацию БД (`db.connect()`), миграции и корректный bootstrap всех модулей.

Логи в реальном времени:

```bash
journalctl -u corebot.service -f
```

---

## 6) Проверка после установки (обязательный чек)

1. В Telegram открыть нового бота и написать `/start` с аккаунта владельца.
2. Проверить, что бот отвечает и меню открывается.
3. Проверить, что чужой Telegram ID (не `OWNER_ID`) не получает админ-доступ.
4. Проверить, что в логах нет ошибок по конфигу/БД.
5. Зафиксировать в своей карточке инстанса:
   - дата запуска;
   - версия;
   - статус `active`.

---

## 7) Как включить локальную веб-панель (без открытия порта в интернет)

Панель уже доступна на VPS: `http://127.0.0.1:8081/panel/`.

С вашего компьютера:

```bash
ssh -L 8081:127.0.0.1:8081 <user>@<VPS_IP>
```

Потом в браузере:

`http://127.0.0.1:8081/panel/`

Логин/пароль берутся из:

- `CP_BOOTSTRAP_ADMIN_USERNAME`
- `CP_BOOTSTRAP_ADMIN_PASSWORD`

После первого входа пароль лучше сменить.

---

## 8) Как подключить телеметрию агента (если нужна)

По умолчанию в `.env` стоит `CP_AGENT_ENABLED=0`.

Чтобы включить:

1. Поднимите `corebot-cp.service`.
2. В панели создайте/получите токен агента.
3. Вставьте в `.env`:

```env
CP_AGENT_ENABLED=1
CP_AGENT_TOKEN=<токен_агента>
CP_INGEST_URL=http://127.0.0.1:8081/ingest/batch
```

4. Перезапустите бот:

```bash
systemctl restart corebot.service
```

Если видите `401` на ingest — значит токен неверный/устарел.

---

## 9) Регулярная поддержка (что делать раз в неделю)

1. Проверить жив ли сервис:

```bash
systemctl is-active corebot.service
systemctl is-active corebot-cp.service
```

2. Просмотреть ошибки за сутки:

```bash
journalctl -u corebot.service --since "24 hours ago" | tail -n 200
```

3. Проверить размер `data/` и свободное место:

```bash
du -sh /opt/corebot/app/data
df -h
```

4. Сделать бэкап.

---

## 10) Обновление версии без простоя надолго

Подробная отдельная инструкция: `docs/VPS_UPDATE_GUIDE.md`.

```bash
sudo -u corebot bash -c 'cd /opt/corebot/app && git pull'
sudo -u corebot /opt/corebot/venv/bin/pip install -r /opt/corebot/app/requirements.txt
systemctl restart corebot-cp.service
systemctl restart corebot.service
systemctl status corebot.service
```

После рестарта проверяйте `/start` в Telegram.

---

## 11) Бэкапы (минимальный рабочий стандарт)

Бэкапьте:

- `/opt/corebot/app/data/`
- `/opt/corebot/app/.env` (в защищённое место)

Пример ручного бэкапа:

```bash
tar -czvf /opt/corebot/corebot-data-$(date +%F).tar.gz -C /opt/corebot/app data
```

Рекомендуется cron раз в сутки.

### 11.1 Восстановление данных при переносе на новый VPS (локальный ПК -> VPS)

Минимальный набор для переноса:

- `/opt/corebot/app/.env`
- `/opt/corebot/app/data/corebot.db`
- `/opt/corebot/app/data/sessions/` (критично: файлы сессий аккаунтов)
- `/opt/corebot/app/data/neuro/` (промпты нейрочата)
- `/opt/corebot/app/data/files/` (загрузки/служебные файлы, если использовались)

Команды с локального Windows (CMD):

```cmd
set VPS=<IP_СЕРВЕРА>
scp "C:\Users\bond\Desktop\CoreBot\.env" root@%VPS%:/opt/corebot/app/.env
scp "C:\Users\bond\Desktop\CoreBot\data\corebot.db" root@%VPS%:/opt/corebot/app/data/corebot.db
scp -r "C:\Users\bond\Desktop\CoreBot\data\sessions" root@%VPS%:/opt/corebot/app/data/
scp -r "C:\Users\bond\Desktop\CoreBot\data\neuro" root@%VPS%:/opt/corebot/app/data/
scp -r "C:\Users\bond\Desktop\CoreBot\data\files" root@%VPS%:/opt/corebot/app/data/
```

После копирования на VPS:

```bash
systemctl stop corebot.service
chown -R corebot:corebot /opt/corebot/app
chmod 600 /opt/corebot/app/.env
find /opt/corebot/app/data -type d -exec chmod 755 {} \;
find /opt/corebot/app/data -type f -exec chmod 644 {} \;
systemctl start corebot.service
systemctl status corebot.service --no-pager -l
```

Проверка БД:

```bash
sqlite3 /opt/corebot/app/data/corebot.db "SELECT 'accounts', count(*) FROM accounts;"
sqlite3 /opt/corebot/app/data/corebot.db "SELECT 'proxies', count(*) FROM proxies;"
```

---

## 12) Частые проблемы и быстрые решения

| Проблема | Признак | Что делать |
|---|---|---|
| Неверный `BOT_TOKEN` | бот не стартует, ошибки авторизации | проверить `.env`, затем `systemctl restart corebot.service` |
| Неверный `OWNER_ID` | владелец не видит админ-функции | подставить правильный numeric ID |
| Нет прав на `data/` | ошибки `Permission denied` | `chown -R corebot:corebot /opt/corebot/app` |
| CP не стартует | `corebot-cp.service failed` | `journalctl -u corebot-cp.service -n 100`, проверить `CP_DATABASE_URL` |
| Ошибка telemtry `401` | события не уходят | перевыпустить `CP_AGENT_TOKEN`, вставить в `.env` |
| После обновления сломался запуск | service падает сразу | откатить код, сверить зависимости, проверить логи |

---

## 13) Аварийный чек (когда «ничего не работает»)

Выполнить по порядку:

```bash
systemctl status corebot.service --no-pager
journalctl -u corebot.service -n 100 --no-pager
cat /opt/corebot/app/.env | sed -n '1,80p'
ls -la /opt/corebot/app/data
```

Проверьте:

- есть ли `BOT_TOKEN`, `OWNER_ID`, `API_ID`, `API_HASH`;
- правильный ли путь в `DATABASE_URL`;
- существует ли каталог `data/`.

Если надо «мягко перезапустить всё»:

```bash
systemctl restart corebot-cp.service
systemctl restart corebot.service
```

---

## 14) Процедура подключения нового коллеги (короткая операционная)

1. Получить от коллеги 3 вещи: `BOT_TOKEN`, `OWNER_ID`, SSH.
2. Повторить разделы 4 и 5 на его VPS.
3. Проверить `/start` с его аккаунта.
4. Выдать коллеге ссылку на бота (username).
5. Записать инстанс в вашу карточку учёта.

Среднее время после набитой руки: 20–40 минут.

---

## 15) Offboarding (если коллега уходит)

1. Остановить сервисы:

```bash
systemctl disable --now corebot.service
systemctl disable --now corebot-cp.service
```

2. Сделать финальный бэкап `data/`.
3. Передать архив владельцу (если требуется).
4. Удалить секреты (`.env`) с сервера или отозвать доступ.

---

## 16) Финальный чеклист админа

- [ ] Данные коллеги получены: `BOT_TOKEN`, `OWNER_ID`, SSH
- [ ] Установлен Python 3.11+, создан `corebot`
- [ ] Код + venv + зависимости установлены
- [ ] `.env` заполнен и защищён (`chmod 600`)
- [ ] `corebot-cp.service` и `corebot.service` запущены
- [ ] `/start` проверен владельцем
- [ ] Бэкап настроен
- [ ] Инстанс внесён в вашу таблицу учёта
