# Развёрнутое описание

## RU
CoreBot — система массовых рассылок в Telegram через пул userbot-аккаунтов. Управление из одного Telegram-бота: импорт сессий, прокси, группировка, мониторинг статусов, редактирование профилей. Рассылки поддерживают рандомизацию текста, плейсхолдеры, задержки, ротацию и аудиторию по классовой системе (new/pulse/alive/accept/decline/stop/bl).

Нейрочат на базе OpenRouter ведёт диалоги за аккаунты. Модель и system-промпт настраиваются глобально или per-рассылка, LLM-команды в ответах классифицируют клиентов (`[ACCEPT]`, `[DECLINE]`, `[STOP]`, `[SEND_LINK]`). Ручные ответы из веб-панели идут через ту же Telethon-сессию — контекст диалога не теряется, а Telegram не видит «второго устройства».

Веб-панель на FastAPI и SPA (vanilla JS, без сборщика) даёт оператору live-просмотр диалогов через SSE, отправку ручных сообщений, CRUD для аккаунтов, рассылок, клиентов, прокси и групп, дашборд с метриками, мягкое архивирование переписок и настройки инстанса. Есть ролевая модель (super_admin / tenant_viewer) и JWT-авторизация.

Технически: aiogram 3 для Control Bot, Telethon для воркеров, SQLAlchemy 2 (async + sync) с авто-миграциями, SQLite в WAL-режиме, graceful shutdown. В продакшене бот и панель работают в отдельных systemd-юнитах; панель слушает только 127.0.0.1, доступ через SSH-туннель или nginx+HTTPS. Ключ OpenRouter хранится зашифрованно через Fernet.

## EN
CoreBot is an automated Telegram mailing system via a pool of userbot accounts. The owner manages accounts from a single Telegram bot: session import, proxy assignment, grouping, status monitoring, and bulk profile editing. Mailings support text randomization, placeholders, fine-grained delays, account rotation, and class-based audience targeting (new/pulse/alive/accept/decline/stop/bl).

AI chat powered by OpenRouter lets accounts conduct dialogs with LLM responses. Model and system prompt are configurable globally or per-mailing, and LLM commands in responses automatically classify clients (`[ACCEPT]`, `[DECLINE]`, `[STOP]`, `[SEND_LINK]`). Manual replies from the web panel go through the same Telethon session as AI chat — dialog context is preserved and Telegram doesn't see a "second device".

Web panel on FastAPI and SPA (vanilla JS, no bundler) gives the operator live dialog view via SSE, manual message sending, CRUD for accounts, mailings, clients, proxies and groups, business dashboard with metrics, soft archive of conversations, and instance settings management. It has a role model (super_admin / tenant_viewer) and JWT authentication.

Technically: aiogram 3 for Control Bot, Telethon for workers, SQLAlchemy 2 (async + sync) with auto-migrations, SQLite in WAL mode, graceful shutdown. In production the bot and panel run in separate systemd units; the panel listens only on 127.0.0.1, accessed via SSH tunnel or nginx+HTTPS. The OpenRouter key is stored encrypted with Fernet.
