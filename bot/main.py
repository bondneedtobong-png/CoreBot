"""
Запуск Control Bot (aiogram 3.x).
"""
import asyncio

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message
from aiogram.exceptions import TelegramBadRequest

from bot.config import BOT_TOKEN, OWNER_ID
from bot.keyboards.main import (
    get_main_keyboard,
    get_proxy_keyboard,
    get_accounts_keyboard,
    get_mailing_keyboard,
)
from bot.handlers.accounts import router as accounts_router
from bot.handlers.clients import router as clients_router
from bot.handlers.database import database_router
from bot.handlers.mailing import router as mailing_router
from bot.handlers.neurochat import router as neurochat_router
from bot.handlers.openrouter_key import router as openrouter_key_router
from bot.handlers.proxy import router as proxy_router
from bot.handlers.warmup_menu import router as warmup_menu_router
from bot.handlers.username_list_tool import router as username_list_tool_router
from utils.logger import log

# Старые сообщения с callback_data «cancel» (до разделения по разделам)
legacy_cancel_router = Router()


@legacy_cancel_router.callback_query(F.data == "cancel")
async def cb_cancel_legacy(callback: CallbackQuery, state: FSMContext):
    """Обработка устаревшей кнопки «Отмена»; новые клавиатуры шлют cancel_accounts / cancel_mailing / cancel_proxy."""
    if callback.from_user.id != OWNER_ID:
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


async def run_bot():
    """Запуск бота."""
    from bot.config import get_control_bot_proxy
    from aiogram.client.session.aiohttp import AiohttpSession
    
    log.info("Запуск Control Bot...")

    # 1. Проверка, что aiohttp-socks установлен
    try:
        import aiohttp_socks
        log.info("✅ aiohttp-socks установлен")
    except ImportError:
        log.critical("❌ Не установлен пакет aiohttp-socks!")
        log.critical("   Выполните: pip install aiohttp-socks")
        raise SystemExit(1)

    # 2. Получение конфигурации прокси
    proxy_config = get_control_bot_proxy()

    # 3. Создание сессии — максимально просто
    if proxy_config and proxy_config.get('url'):
        proxy_url = proxy_config['url']
        log.info(f"🌐 Создание сессии с прокси: {proxy_url}")
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
    dp.include_router(neurochat_router)
    dp.include_router(proxy_router)
    dp.include_router(warmup_menu_router)
    dp.include_router(username_list_tool_router)
    dp.include_router(legacy_cancel_router)

    # 7. Хендлеры
    @dp.message(CommandStart())
    async def cmd_start(message: Message):
        """Обработчик команды /start."""
        user_id = message.from_user.id

        # Проверка доступа (только владелец)
        if user_id != OWNER_ID:
            await message.answer("⛔ Доступ запрещён. Этот бот предназначен только для владельца.")
            log.warning(f"Попытка доступа от unauthorized пользователя: {user_id}")
            return

        await message.answer(
            "👋 <b>Добро пожаловать в CoreBot!</b>\n\n"
            "Это система для массовой рассылки сообщений в Telegram.\n\n"
            "📋 <b>Главное меню:</b>",
            reply_markup=get_main_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        log.info(f"Команда /start от пользователя {user_id}")

    @dp.message(Command("help"))
    async def cmd_help(message: Message):
        """Обработчик команды /help."""
        if message.from_user.id != OWNER_ID:
            return

        help_text = (
            "📖 <b>Справка по командам:</b>\n\n"
            "🔹 /start - Главное меню\n"
            "🔹 /help - Эта справка\n"
            "🔹 /status - Статус системы\n"
            "🔹 /start_mailing - Запустить рассылку\n\n"
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
        if message.from_user.id != OWNER_ID:
            return

        from workers.manager import worker_manager
        from database.session import session_scope
        from database.models import MailingStatus
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
        asyncio.create_task(worker_manager.start_mailing(mailing.id))

    @dp.message(Command("status"))
    async def cmd_status(message: Message):
        """Обработчик команды /status - статус системы."""
        if message.from_user.id != OWNER_ID:
            return

        from database.session import session_scope
        from database.models import Account, AccountStatus, Client, ClientStatus, Mailing, MailingStatus
        from sqlalchemy import select, func

        async with session_scope() as session:
            accounts_result = await session.execute(
                select(Account.status, func.count(Account.id)).group_by(Account.status)
            )
            accounts_stats = {row[0].value: row[1] for row in accounts_result.all()}

            clients_result = await session.execute(
                select(Client.status, func.count(Client.id)).group_by(Client.status)
            )
            clients_stats = {row[0].value: row[1] for row in clients_result.all()}

            running_mailing = await session.execute(
                select(Mailing).where(Mailing.status == MailingStatus.RUNNING)
            )
            running = running_mailing.scalar_one_or_none()

        status_text = (
            "📊 <b>Статус системы:</b>\n\n"
            f"👥 <b>Аккаунты:</b>\n"
            f"  • Активные: {accounts_stats.get('active', 0)}\n"
            f"  • Неактивные: {accounts_stats.get('inactive', 0)}\n"
            f"  • FloodWait: {accounts_stats.get('flood_wait', 0)}\n"
            f"  • Забаненные: {accounts_stats.get('banned', 0)}\n\n"
            f"📁 <b>Клиенты:</b>\n"
            f"  • Новые: {clients_stats.get('new', 0)}\n"
            f"  • Обработанные: {clients_stats.get('contacted', 0)}\n"
            f"  • Невалидные: {clients_stats.get('invalid', 0)}\n\n"
        )

        if running:
            status_text += (
                f"🚀 <b>Активная рассылка:</b>\n"
                f"  • ID: {running.id}\n"
                f"  • Отправлено: {running.messages_sent}\n"
                f"  • Ошибок: {running.messages_failed}\n"
            )
        else:
            status_text += "🚀 <b>Активная рассылка:</b> Нет\n"

        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🏠 В главное меню", callback_data="menu_back")]
            ]
        )

        await message.answer(status_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)

    @dp.callback_query(F.data == "menu_accounts")
    async def cb_accounts(callback: CallbackQuery, state: FSMContext):
        """Кнопка управления аккаунтами."""
        if callback.from_user.id != OWNER_ID:
            await callback.answer("⛔ Доступ запрещён", show_alert=True)
            return

        await state.clear()
        await safe_edit_message(
            callback,
            "👥 <b>Управление аккаунтами</b>\n\n"
            "Здесь вы можете загрузить аккаунты через Tdata.\n\n"
            "📁 Отправьте ZIP-архив с папкой tdata,\n"
            "или выберите действие:",
            reply_markup=get_accounts_keyboard(),
        )
        await callback.answer()

    @dp.callback_query(F.data == "menu_proxy")
    async def cb_proxy(callback: CallbackQuery):
        """Кнопка управления прокси."""
        if callback.from_user.id != OWNER_ID:
            await callback.answer("⛔ Доступ запрещён", show_alert=True)
            return

        await safe_edit_message(
            callback,
            "🌐 <b>Управление прокси</b>\n\n"
            "Добавляйте прокси и привязывайте их к аккаунтам.\n"
            "Формат: user:pass@host:port",
            reply_markup=get_proxy_keyboard(),
        )
        await callback.answer()

    @dp.callback_query(F.data == "menu_back")
    async def cb_back(callback: CallbackQuery):
        """Кнопка назад в главное меню."""
        if callback.from_user.id != OWNER_ID:
            await callback.answer("⛔ Доступ запрещён", show_alert=True)
            return

        await safe_edit_message(
            callback,
            "👋 <b>Добро пожаловать в CoreBot!</b>\n\n"
            "Это система для массовой рассылки сообщений в Telegram.\n\n"
            "📋 <b>Главное меню:</b>",
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

    try:
        await dp.start_polling(bot)
    finally:
        # Корректное закрытие сессии (в AiohttpSession нет атрибута `closed`).
        if bot.session:
            try:
                await bot.session.close()
                log.info("✅ Сессия Control Bot закрыта")
            except Exception as e:
                log.warning(f"Ошибка закрытия сессии Control Bot: {e}")
