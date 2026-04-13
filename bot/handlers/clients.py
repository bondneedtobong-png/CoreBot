"""
Хендлеры для управления базой клиентов.
Загрузка TXT, просмотр, очистка.
"""
import re
from pathlib import Path

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery
from sqlalchemy import update

from bot.config import OWNER_ID, FILES_DIR
from bot.handlers.accounts.common import safe_edit_message
from bot.keyboards.main import get_clients_keyboard, get_cancel_with_back_keyboard, get_context_back_keyboard
from bot.keyboards.main import CLIENTS_LIST_PAGE_SIZE, get_clients_list_keyboard
from database.repository import db
from database.session import session_scope
from database.models import Client, ClientStatus
from database.repositories import ClientRepository
from utils.logger import log

router = Router()


class ClientUpload(StatesGroup):
    """Состояния для загрузки базы клиентов."""
    waiting_for_file = State()
    processing = State()


@router.callback_query(F.data == "clients_upload")
async def cb_clients_upload(callback: CallbackQuery, state: FSMContext):
    """Начало загрузки базы клиентов."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await state.clear()
    await safe_edit_message(
        callback.message,
        "📥 <b>Загрузка базы клиентов</b>\n\n"
        "Отправьте TXT-файл со списком @username.\n\n"
        "📝 Формат:\n"
        "• Каждый username с новой строки\n"
        "• Можно с @ или без\n"
        "• Пример:\n"
        "  @username1\n"
        "  username2\n"
        "  @username3\n\n"
        "❌ Отмена: /start",
        reply_markup=get_cancel_with_back_keyboard("cancel_clients_upload", "menu_clients"),
    )
    await state.set_state(ClientUpload.waiting_for_file)
    await callback.answer()


@router.callback_query(F.data == "cancel_clients_upload")
async def cb_cancel_clients_upload(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    await safe_edit_message(
        callback.message,
        "❌ <b>Отменено</b>\n\n"
        "Загрузка базы клиентов отменена.",
        reply_markup=get_clients_keyboard(),
    )
    await callback.answer()


@router.message(ClientUpload.waiting_for_file, F.document)
async def process_clients_txt(message: Message, state: FSMContext):
    """Обработка загруженного TXT с клиентами."""
    if message.from_user.id != OWNER_ID:
        return
    
    await state.set_state(ClientUpload.processing)
    
    document = message.document
    
    # Проверка расширения
    if not document.file_name.lower().endswith('.txt'):
        await message.answer("❌ Пожалуйста, отправьте TXT-файл")
        await state.clear()
        return
    
    status_msg = await message.answer("⏳ Загрузка файла...")
    
    try:
        # Скачивание файла
        file = await message.bot.get_file(document.file_id)
        file_path = FILES_DIR / f"clients_{document.file_name}"
        
        FILES_DIR.mkdir(parents=True, exist_ok=True)
        
        await message.bot.download_file(file.file_path, file_path)
        await status_msg.edit_text("📖 Чтение файла...")
        
        # Чтение и парсинг
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Извлечение username
        usernames = set()
        pattern = r'@?([a-zA-Z0-9_]{5,32})'
        
        for match in re.finditer(pattern, content):
            username = match.group(1).lower()
            # Пропускаем системные
            if not username.startswith('telegram') and not username.startswith('bot'):
                usernames.add(username)
        
        if not usernames:
            await status_msg.edit_text(
                "❌ Не найдено валидных username.\n"
                "Проверьте формат файла."
            )
            await state.clear()
            return
        
        await status_msg.edit_text(f"📊 Найдено username: {len(usernames)}\n\nСохранение в БД...")
        
        # Сохранение в БД
        async with session_scope() as session:
            added = await ClientRepository.create_many(session, list(usernames))
        
        # Очистка файла
        try:
            file_path.unlink()
        except Exception as e:
            log.warning(f"Не удалось удалить файл: {e}")
        
        await status_msg.edit_text(
            f"✅ <b>Готово!</b>\n\n"
            f"📊 Добавлено клиентов: {added}\n"
            f"📁 Файл сохранён: {file_path.name}",
            parse_mode=ParseMode.HTML,
        )
        
    except Exception as e:
        log.error(f"Ошибка обработки файла клиентов: {e}")
        await status_msg.edit_text(f"❌ Ошибка: {e}")
    
    finally:
        await state.clear()


def _clients_list_page_from_data(data: str) -> int:
    if data == "clients_list":
        return 0
    if data.startswith("clients_list_p_"):
        return int(data.rsplit("_", 1)[-1])
    return 0


@router.callback_query(F.data == "clients_page_info")
async def cb_clients_page_info(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔", show_alert=True)
        return
    await callback.answer("Номер страницы · листайте ◀ ▶", show_alert=True)


@router.callback_query(F.data == "clients_list")
@router.callback_query(F.data.startswith("clients_list_p_"))
async def cb_clients_list(callback: CallbackQuery):
    """Показать список клиентов."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    
    page = _clients_list_page_from_data(callback.data)

    async with session_scope() as session:
        from sqlalchemy import select, func
        # Общее количество
        total = await session.execute(select(func.count(Client.id)))
        total_count = total.scalar()
            
        # По статусам
        status_result = await session.execute(
            select(Client.status, func.count(Client.id)).group_by(Client.status)
        )
        status_stats = {row[0].value: row[1] for row in status_result.all()}
            
        # Список клиентов (для пагинации)
        rows = await session.execute(
            select(Client).order_by(Client.id.desc())
        )
        clients = list(rows.scalars().all())
    
    text = "📁 <b>База клиентов</b>\n\n"
    text += f"📊 <b>Всего:</b> {total_count}\n\n"
    text += (
        f"🟢 Новые: {status_stats.get('new', 0)}\n"
        f"✅ Обработанные: {status_stats.get('contacted', 0)}\n"
        f"❌ Невалидные: {status_stats.get('invalid', 0)}\n\n"
    )
    
    total = len(clients)
    total_pages = max(1, (total + CLIENTS_LIST_PAGE_SIZE - 1) // CLIENTS_LIST_PAGE_SIZE) if total else 1
    page = max(0, min(page, total_pages - 1))
    if total:
        start = page * CLIENTS_LIST_PAGE_SIZE + 1
        end = min((page + 1) * CLIENTS_LIST_PAGE_SIZE, total)
        text += f"\n📋 <b>Список клиентов:</b> страница {page + 1}/{total_pages} · строки {start}–{end}\n"
    
    await callback.message.answer(
        text,
        reply_markup=get_clients_list_keyboard(clients, page=page),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data == "clients_clear")
async def cb_clients_clear(callback: CallbackQuery):
    """Очистка базы клиентов."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    
    # Отправляем подтверждение
    await callback.message.answer(
        "⚠️ <b>Подтверждение</b>\n\n"
        "Вы уверены, что хотите очистить всю базу клиентов?\n"
        "Это действие нельзя отменить.\n\n"
        "Нажмите ещё раз для подтверждения.",
        reply_markup=get_confirm_clear_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data == "clients_reset_contacted")
async def cb_clients_reset_contacted(callback: CallbackQuery):
    """Запрос подтверждения массового сброса CONTACTED -> NEW."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await callback.message.answer(
        "♻️ <b>Повторный прогон клиентов</b>\n\n"
        "Сбросить статус всех клиентов <code>CONTACTED</code> обратно в <code>NEW</code>?\n"
        "Это удобно для повторного теста рассылки без переимпорта.\n\n"
        "Нажмите ещё раз для подтверждения.",
        reply_markup=get_confirm_reset_clients_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data == "clients_reset_contacted_confirm")
async def cb_clients_reset_contacted_confirm(callback: CallbackQuery):
    """Подтверждение массового сброса CONTACTED -> NEW."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    async with session_scope() as session:
        result = await session.execute(
            update(Client)
            .where(Client.status == ClientStatus.CONTACTED)
            .values(status=ClientStatus.NEW, last_contacted_at=None)
        )
        await session.commit()
        changed = int(result.rowcount or 0)

    await callback.message.answer(
        f"✅ Готово. Переведено в NEW: <b>{changed}</b> клиентов.",
        parse_mode=ParseMode.HTML,
        reply_markup=get_context_back_keyboard("menu_clients"),
    )
    await callback.answer("Сброшено")


@router.callback_query(F.data == "clients_clear_confirm")
async def cb_clients_clear_confirm(callback: CallbackQuery):
    """Подтверждение очистки базы."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    
    async with session_scope() as session:
        await ClientRepository.clear_all(session)
    
    await callback.message.answer("🗑 База клиентов очищена.")
    await callback.answer()


def get_confirm_clear_keyboard():
    """Клавиатура подтверждения очистки."""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    
    keyboard = [
        [
            InlineKeyboardButton(text="⚠️ Да, очистить", callback_data="clients_clear_confirm"),
        ],
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data="clients_upload"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_confirm_reset_clients_keyboard():
    """Клавиатура подтверждения сброса CONTACTED -> NEW."""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    keyboard = [
        [
            InlineKeyboardButton(
                text="♻️ Да, сбросить",
                callback_data="clients_reset_contacted_confirm",
            ),
        ],
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_clients"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)
