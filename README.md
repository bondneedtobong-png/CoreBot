# CoreBot

Первый рабочий срез V2 и его границы: [docs/COREBOT_V2_MVP.md](docs/COREBOT_V2_MVP.md). Текущие ограничения и исправления: [docs/KNOWN_ISSUES.md](docs/KNOWN_ISSUES.md). Порядок дальнейшей разработки: [docs/ROADMAP.md](docs/ROADMAP.md).

Сверка всех 16 категорий GramGPT с текущим кодом: [матрица и этапы](docs/GRAMGPT_IMPLEMENTATION_MATRIX.md), [статус каждого подпункта](docs/GRAMGPT_SUBFEATURE_CHECKLIST.md).

Управление сетью userbot-аккаунтов Telegram: рассылки, нейрочат (LLM), парсинг аудитории, ручные ответы и аналитика. Self-hosted: один Python-процесс бота + FastAPI-панель на одном VPS, SQLite, без облаков.

> Python 3.11+ · SQLite (WAL) · aiogram 3 + Telethon + FastAPI · systemd

## Что внутри

1. **Control Bot** (aiogram 3) — Telegram-бот владельца: аккаунты, прокси, клиенты, рассылки, нейрочат. Точка входа `main.py`.
2. **Workers** (Telethon) — userbot-аккаунты: отправка, входящие, ручные ответы. Пул в `workers/manager.py`.
3. **Control Plane** (FastAPI + SPA) — веб-панель оператора: диалоги live (SSE), CRUD, аналитика. Панель сама Telegram не трогает — всё через очереди `outbound_queue` / `bot_commands`, исполняет процесс бота.

```
Telegram ─┬─ Control Bot (aiogram) ─┐
          └─ Workers (Telethon) ────┴─► SQLite (data/corebot.db, WAL) ◄─► Control Plane (FastAPI+SPA) ──► Browser (SSH-туннель)
```

## Стек

Python 3.11+ · aiogram ≥3.3 · Telethon ≥1.34 · FastAPI/uvicorn · SQLAlchemy 2 + aiosqlite · OpenRouter (LLM, default `openrouter/free`) · vanilla JS SPA (без сборки) · loguru · pytest/ruff. Полные версии — `requirements.txt`.

Ключевое поведение:
- Аккаунт без рабочего прокси **не подключается**. Это исключает прямое подключение при ошибке настройки, но не гарантирует отсутствия ограничений Telegram.
- Перед каждой отправкой действует общий сохраняемый шлюз: состояние аккаунта, явные ограничения и дневной бюджет. Нейрокомментинг создаёт редактируемый черновик для управляемого обсуждения и ставит отправку в очередь только после одобрения.
- Нейрочат: иерархия `global → mailing local → account/filter`, deny-причины в логах (`global_disabled`, `mailing_local_disabled`, `worker_disconnected`, `client_class_bl/stop`).
- Классы-клиенты: `pulse` (каждое входящее), `alive` (окно 60 мин), `accept/decline/stop/bl`.
- Ручные ответы из веба: `outbound_queue` + `OutboundConsumer`. Повторная подготовка соединения допускается до вызова Telegram; после начала отправки неизвестный исход требует проверки оператором.
- Ровно **один** parser-loop: `PARSER_EMBEDDED=0` запускает его в боте на уже подключённых аккаунтах. Режим `1` запускает парсер в панели и подходит, когда эти сессии не заняты ботом.

## Структура

```
main.py  start_corebot.bat  .env.example
bot/            Control Bot (handlers/, keyboards/)
workers/        manager, outbound_consumer, bot_command_consumer, neuro_incoming (thin), parser/
services/neurochat/  manager, incoming_service, llm, post_actions, engagement, admin, monitor
services/database/   CRM-сервисы (классы, fleet_cleanup, export)
database/       models, repository (авто-миграции), repositories, sqlite_pragmas
control_plane/  FastAPI: routes/, business/ (accounts/dialogs/mailings/clients/groups/proxies/...), services/
web-panel/      SPA: index.html, main.js, styles.css
utils/ tools/ scripts/ tests/ skills/corebot-vps-deploy/
data/ logs/     (не в git) БД, сессии, логи
```

## Быстрый старт

```bash
python -m venv .venv
.venv\Scripts\activate          # Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # заполнить API_ID, API_HASH, BOT_TOKEN, OWNER_ID
```

```bat
start_corebot.bat               # Windows: бот + панель + открыть UI
start_corebot.bat --check       # только проверка окружения
```

Вручную: `python main.py` + `uvicorn control_plane.main:app --host 127.0.0.1 --port 8081 --no-access-log`, UI `http://127.0.0.1:8081/panel/`. Рабочие пути семи приоритетных сценариев: [docs/PROTOTYPE_READINESS.md](docs/PROTOTYPE_READINESS.md).

Локальная генерация вымышленных персонажей и фото через ComfyUI: [docs/COMFYUI_CHARACTERS.md](docs/COMFYUI_CHARACTERS.md). На Windows ComfyUI запускается по требованию бота.

Полно: **[DEPLOY.md](DEPLOY.md)** (локально + VPS через скилл), **[AGENTS.md](AGENTS.md)** (архитектура и правила для кодера).

## Конфиг (минимум)

`API_ID`, `API_HASH` (my.telegram.org), `BOT_TOKEN` (BotFather), `OWNER_ID` (@userinfobot). Панель: `CP_JWT_SECRET` (64 hex), `CP_BOOTSTRAP_ADMIN_USERNAME/PASSWORD`, `BOT_DATABASE_URL`. Проверка: `python -m tools.validate_config --mode local|production`. Шаблон — `.env.example`.

## Тесты / gate

```bash
python -m pytest tests/ -q
python -m compileall -q bot control_plane database workers services utils tools scripts tests main.py
python -m ruff check .
node --check web-panel/main.js
python -m tools.validate_skill
python -m tools.validate_config --mode production   # как на VPS
```

CI (`.github/workflows/ci.yml`) крутит то же самое. `ruff format --check` — только curated-список из шапки `pyproject.toml`, legacy не реформатить.

## Безопасность

Доступ к боту — только `OWNER_ID`. Панель — только `127.0.0.1:8081` (SSH-туннель или nginx+HTTPS), наружу порт не открывать. `.env`, `data/*.db`, `data/sessions/` не коммитить. Userbot-автоматизация нарушает Telegram ToS — один аккаунт = один прокси, лимиты и задержки обязательны.
