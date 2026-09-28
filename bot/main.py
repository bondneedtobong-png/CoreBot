"""
Запуск Control Bot (aiogram 3.x).
"""
import asyncio
import os

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message
from aiogram.exceptions import TelegramBadRequest

from bot.config import is_authorized_user
from bot.config import BOT_TOKEN
from bot.keyboards.main import (
    get_main_keyboard,
    get_proxy_keyboard,
    get_accounts_keyboard,
    get_ai_keyboard,
)
from bot.handlers.accounts import router as accounts_router
from bot.handlers.clients import router as clients_router
from bot.handlers.database import database_router
from bot.handlers.mailing import router as mailing_router
from bot.handlers.neurochat import router as neurochat_router
from bot.handlers.openrouter_key import router as openrouter_key_router
from bot.handlers.ai_provider_menu import router as ai_provider_menu_router
from bot.handlers.comfy_photo import router as comfy_photo_router
from bot.handlers.comfy_identity import router as comfy_identity_router
from bot.handlers.community_link_check import router as community_link_check_router
from bot.handlers.chat_campaign import router as chat_campaign_router
from bot.handlers.fleet_cleanup import router as fleet_cleanup_router
from bot.handlers.managed_reactions import router as managed_reactions_router
from bot.handlers.proxy import router as proxy_router
from bot.handlers.system_status import (
    build_system_status_text,
    get_system_status_keyboard,
    router as system_status_router,
)
from bot.handlers.warmup_menu import router as warmup_menu_router
from bot.handlers.username_list_tool import router as username_list_tool_router
from utils.background_tasks import background_tasks
from utils.logger import log

# Старые сообщения с callback_data «cancel» (до разделения по разделам)
legacy_cancel_router = Router()


@legacy_cancel_router.callback_query(F.data == "cancel")
async def cb_cancel_legacy(callback: CallbackQuery, state: FSMContext):
    """Обработка устаревшей кнопки «Отмена»; новые клавиатуры шлют cancel_accounts / cancel_mailing / cancel_proxy."""
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await state.clear()
    try:
        await callback.message.edit_text(
            "❌ <b>Отменено</b>\n\n"
            "Кнопка из старого сообщения — откройте нужный раздел из меню.",
            reply_markup=get_main_keyboard(),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        log.warning(f"legacy cancel edit_text: {e}")
        await callback.message.answer(
            "❌ Отменено. Откройте раздел из главного меню.",
            reply_markup=get_main_keyboard(),
            parse_mode=ParseMode.HTML,
        )
    await callback.answer()


# ==================== Утилиты ====================

async def safe_edit_message(
    callback: CallbackQuery,
    text: str,
    reply_markup=None,
    parse_mode: ParseMode = ParseMode.HTML,
):
    """
    Безопасно редактирует сообщение — работает и с текстовыми, и с фото-сообщениями.
    Не считает ошибкой ответ Telegram «message is not modified».
    """
    try:
        if callback.message.photo or callback.message.caption is not None:
            await callback.message.edit_caption(
                caption=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
        else:
            await callback.message.edit_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
    except TelegramBadRequest as e:
        desc = (e.message or str(e) or "").lower()
        if "message is not modified" in desc or "not modified" in desc:
            return
        log.warning(f"safe_edit_message TelegramBadRequest: {e}")
    except Exception as e:
        log.warning(f"safe_edit_message fallback: {e}")
        try:
            await callback.message.edit_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
        except TelegramBadRequest as e2:
            desc = (e2.message or str(e2) or "").lower()
            if "message is not modified" in desc or "not modified" in desc:
                return
            raise


def _mask_proxy_url_for_log(proxy_url: str) -> str:
    """Маскировать credentials в proxy-URL для логов: user:pass → ***."""
    try:
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(proxy_url)
        if parts.username or parts.password:
            netloc = parts.hostname or ""
            if parts.port:
                netloc = f"{netloc}:{parts.port}"
            masked = parts._replace(netloc=netloc)
            return urlunsplit(masked) + " (credentials hidden)"
        return proxy_url
    except Exception:
        return "<proxy url hidden>"


async def run_bot():
    """Запуск бота."""
    from bot.config import get_control_bot_proxy
    from aiogram.client.session.aiohttp import AiohttpSession

    log.info("Запуск Control Bot...")

    # 1. Проверка, что aiohttp-socks установлен
    try:
        import aiohttp_socks  # noqa: F401
        log.info("✅ aiohttp-socks установлен")
    except ImportError:
        log.critical("❌ Не установлен пакет aiohttp-socks!")
        log.critical("   Выполните: pip install aiohttp-socks")
        raise SystemExit(1)

    # 2. Получение конфигурации прокси
    proxy_config = get_control_bot_proxy()

    # 3. Создание сессии. aiogram сам создаёт aiohttp-socks connector
    #    для socks5:// через параметр proxy. Никогда не логируем user:pass.
    if proxy_config and proxy_config.get('url'):
        proxy_url = str(proxy_config['url'])
        safe_label = _mask_proxy_url_for_log(proxy_url)
        log.info(f"🌐 Создание сессии с прокси: {safe_label}")
        session = AiohttpSession(proxy=proxy_url)
    else:
        log.info("🌐 Прокси для Control Bot не настроен — используется прямое подключение")
        session = AiohttpSession()

    # 4. Создание бота и диспетчера
    bot = Bot(token=BOT_TOKEN, session=session)
    from workers.manager import worker_manager

    worker_manager.set_notify_bot(bot)
    dp = Dispatcher(storage=MemoryStorage())

    # 5. Проверка авторизации с retry
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            log.info(f"🔌 Попытка подключения {attempt}/{max_retries}...")
            me = await bot.get_me()
            log.info(f"✅ Control Bot авторизован: @{me.username} ({me.first_name})")
            break
        except Exception as e:
            if attempt == max_retries:
                log.error(f"❌ Не удалось подключиться после {max_retries} попыток: {e}")
                await session.close()
                raise
            log.warning(f"⚠️ Попытка {attempt} не удалась, ждём...")
            await asyncio.sleep(5 * attempt)

    # 6. Регистрация роутеров
    dp.include_router(accounts_router)
    dp.include_router(database_router)
    dp.include_router(clients_router)
    dp.include_router(mailing_router)
    dp.include_router(openrouter_key_router)
    dp.include_router(ai_provider_menu_router)
    dp.include_router(comfy_photo_router)
    dp.include_router(comfy_identity_router)
    dp.include_router(community_link_check_router)
    dp.include_router(chat_campaign_router)
    dp.include_router(neurochat_router)
    dp.include_router(proxy_router)
    dp.include_router(warmup_menu_router)
    dp.include_router(username_list_tool_router)
    dp.include_router(system_status_router)
    dp.include_router(fleet_cleanup_router)
    dp.include_router(managed_reactions_router)
    dp.include_router(legacy_cancel_router)

    # 7. Хендлеры
    @dp.message(CommandStart())
    async def cmd_start(message: Message):
        """Обработчик команды /start."""
        user_id = message.from_user.id

        # Проверка доступа (только владелец)
        if not is_authorized_user(user_id):
            await message.answer("⛔ Доступ запрещён. Этот бот предназначен только для владельца.")
            log.warning(f"Попытка доступа от unauthorized пользователя: {user_id}")
            return

        await message.answer(
            "<b>CoreBot V2</b>\n\nВыберите задачу. Статус системы: /status.",
            reply_markup=get_main_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        log.info(f"Команда /start от пользователя {user_id}")

    @dp.message(Command("help"))
    async def cmd_help(message: Message):
        """Обработчик команды /help."""
        if not is_authorized_user(message.from_user.id):
            return

        help_text = (
            "📖 <b>Справка по командам:</b>\n\n"
            "🔹 /start - Главное меню\n"
            "🔹 /help - Эта справка\n"
            "🔹 /status - Статус системы\n"
            "🔹 /start_mailing - Запустить рассылку\n\n"
            "🔹 /chat_campaign - Публикация в своих чатах\n\n"
            "<b>Функционал:</b>\n"
            "• Управление аккаунтами (загрузка Tdata)\n"
            "• Импорт базы клиентов из TXT\n"
            "• Запуск и мониторинг рассылок\n"
            "• Просмотр логов и статистики\n\n"
            "Используйте кнопки в главном меню для навигации."
        )
        await message.answer(help_text, parse_mode=ParseMode.HTML)

    @dp.message(Command("start_mailing"))
    async def cmd_start_mailing(message: Message):
        """Запуск активной рассылки."""
        if not is_authorized_user(message.from_user.id):
            return

        from workers.manager import worker_manager
        from database.session import session_scope
        from database.repositories import MailingRepository

        async with session_scope() as session:
            mailing = await MailingRepository.get_by_id(session, 1)

        if not mailing:
            await message.answer("❌ Нет активной рассылки для запуска.")
            return

        gid = getattr(mailing, "target_group_id", None)
        await worker_manager.load_accounts(group_id=gid)
        await worker_manager.connect_all()

        await message.answer(f"🚀 Запуск рассылки \"{mailing.name or mailing.id}\"...")
        background_tasks.create(
            worker_manager.start_mailing(mailing.id),
            name=f"mailing-{mailing.id}",
        )

    @dp.message(Command("status"))
    async def cmd_status(message: Message):
        """Обработчик команды /status — статус системы (единый билдер)."""
        if not is_authorized_user(message.from_user.id):
            return
        text = await build_system_status_text()
        await message.answer(
            text,
            reply_markup=get_system_status_keyboard(),
            parse_mode=ParseMode.HTML,
        )

    @dp.callback_query(F.data == "menu_accounts")
    async def cb_accounts(callback: CallbackQuery, state: FSMContext):
        """Кнопка управления аккаунтами."""
        if not is_authorized_user(callback.from_user.id):
            await callback.answer("⛔ Доступ запрещён", show_alert=True)
            return

        await state.clear()
        await safe_edit_message(
            callback,
            "👥 <b>Аккаунты</b>\n\nИмпорт, управление и группы.",
            reply_markup=get_accounts_keyboard(),
        )
        await callback.answer()

    @dp.callback_query(F.data == "menu_ai")
    async def cb_ai(callback: CallbackQuery, state: FSMContext):
        if not is_authorized_user(callback.from_user.id):
            await callback.answer("⛔ Доступ запрещён", show_alert=True)
            return
        await state.clear()
        await safe_edit_message(
            callback,
            "🧠 <b>ИИ</b>\n\nУправление нейроответами и настройками моделей.",
            reply_markup=get_ai_keyboard(),
        )
        await callback.answer()

    @dp.callback_query(F.data == "menu_proxy")
    async def cb_proxy(callback: CallbackQuery):
        """Кнопка управления прокси."""
        if not is_authorized_user(callback.from_user.id):
            await callback.answer("⛔ Доступ запрещён", show_alert=True)
            return

        await safe_edit_message(
            callback,
            "🌐 <b>Управление прокси</b>\n\n"
            "Добавляйте прокси и привязывайте их к аккаунтам.\n"
            "Формат листа: host:port@user:pass",
            reply_markup=get_proxy_keyboard(),
        )
        await callback.answer()

    @dp.callback_query(F.data == "menu_back")
    async def cb_back(callback: CallbackQuery):
        """Кнопка назад в главное меню."""
        if not is_authorized_user(callback.from_user.id):
            await callback.answer("⛔ Доступ запрещён", show_alert=True)
            return

        await safe_edit_message(
            callback,
            "<b>CoreBot V2</b>\n\nВыберите задачу. Статус системы: /status.",
            reply_markup=get_main_keyboard(),
        )
        await callback.answer()

    # 8. Прогрев пула воркеров: подключаем все Telethon-сессии заранее,
    #    чтобы ручная отправка из веб-панели и нейрочат работали мгновенно
    #    (без «worker not connected»). Не валим бота, если что-то пошло не так.
    try:
        log.info("🔌 Прогрев пула воркеров: загружаем аккаунты и подключаем сессии…")
        await worker_manager.load_accounts()
        await worker_manager.connect_all(quiet_unauthorized=True)
        connected = sum(1 for w in worker_manager.workers.values() if w.is_connected)
        log.info(f"✅ Подключено воркеров: {connected}/{len(worker_manager.workers)}")
    except Exception as e:
        log.warning(f"⚠️ Прогрев воркеров не удался (продолжаем старт бота): {e}")

    # 9. Запуск polling
    log.info("Бот запущен и ожидает команды...")

    parser_task = None
    if os.getenv("PARSER_EMBEDDED", "1").strip().lower() in {"0", "false", "off", "no"}:
        from workers.parser.task_runner import run_forever as run_parser_forever

        parser_task = asyncio.create_task(
            run_parser_forever(runtime_workers=True), name="bot-parser-loop"
        )
        log.info("Парсер запущен с подключёнными аккаунтами бота")

    proxy_health_task = asyncio.create_task(
        worker_manager.run_proxy_health_loop(), name="bot-proxy-health-loop"
    )

    try:
        await dp.start_polling(bot)
    finally:
        proxy_health_task.cancel()
        await asyncio.gather(proxy_health_task, return_exceptions=True)
        if parser_task is not None:
            parser_task.cancel()
            await asyncio.gather(parser_task, return_exceptions=True)
        # Корректное закрытие сессии (в AiohttpSession нет атрибута `closed`).
        if bot.session:
            try:
                await bot.session.close()
                log.info("✅ Сессия Control Bot закрыта")
            except Exception as e:
                log.warning(f"Ошибка закрытия сессии Control Bot: {e}")
