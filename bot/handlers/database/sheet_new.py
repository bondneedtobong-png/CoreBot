"""Загрузить new — импорт @username с отчётом по дубликатам."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot.config import OWNER_ID, FILES_DIR
from bot.handlers.accounts.common import safe_edit_message
from bot.handlers.database.sheet_import_common import parse_usernames_from_txt
from bot.keyboards.database_menu import kb_database_sheets
from bot.keyboards.main import get_cancel_with_back_keyboard
from database.crm_repositories import ClientClassCounterRepository
from database.models import Client
from database.repositories import ClientRepository
from database.session import session_scope
from utils.logger import log

router = Router()


class SheetNewUpload(StatesGroup):
    waiting_for_file = State()


@router.callback_query(F.data == "db_sheet_211")
async def cb_db_sheet_211(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    await safe_edit_message(
        callback.message,
        "📥 <b>Загрузить new</b>\n\n"
        "Отправьте TXT со списком @username (как в базе клиентов).\n\n"
        "• Новые пользователи добавляются в базу.\n"
        "• Уже существующие в БД получают +1 к классу <code>new</code>.\n\n"
        "Отмена: кнопка ниже.",
        reply_markup=get_cancel_with_back_keyboard("cancel_db_sheet211", "db_sec_sheets"),
    )
    await state.set_state(SheetNewUpload.waiting_for_file)
    await callback.answer()


@router.callback_query(F.data == "cancel_db_sheet211")
async def cb_cancel_db_sheet_211(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    await safe_edit_message(
        callback.message,
        "❌ Импорт отменён.",
        reply_markup=kb_database_sheets(),
    )
    await callback.answer()


@router.message(SheetNewUpload.waiting_for_file, F.document)
async def process_sheet_new_txt(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return
    document = message.document
    if not document.file_name.lower().endswith(".txt"):
        await message.answer("❌ Нужен TXT-файл.")
        return
    status_msg = await message.answer("⏳ Загрузка...")
    try:
        file = await message.bot.get_file(document.file_id)
        FILES_DIR.mkdir(parents=True, exist_ok=True)
        file_path = FILES_DIR / f"sheet_new_{document.file_name}"
        await message.bot.download_file(file.file_path, file_path)
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        usernames = parse_usernames_from_txt(content)
        if not usernames:
            await status_msg.edit_text("❌ Не найдено валидных username.")
            await state.clear()
            return
        added = 0
        dup_tagged = 0
        async with session_scope() as session:
            initial = set(await ClientRepository.get_all_usernames(session))
            to_add = [u for u in sorted(usernames) if u not in initial]
            dups = [u for u in sorted(usernames) if u in initial]
            if to_add:
                session.add_all([Client(username=u) for u in to_add])
                await session.commit()
                added = len(to_add)
            for u in dups:
                c = await ClientRepository.get_by_username(session, u)
                if c:
                    await ClientClassCounterRepository.increment(session, c.id, "new", 1)
                    dup_tagged += 1
        try:
            file_path.unlink()
        except OSError:
            pass
        await status_msg.edit_text(
            "✅ <b>Импорт завершён</b>\n\n"
            f"• Добавлено новых: <b>{added}</b>\n"
            f"• Дубликаты (класс <code>new</code> +1): <b>{dup_tagged}</b>\n"
            f"• Уникальных username в файле: <b>{len(usernames)}</b>",
            parse_mode=ParseMode.HTML,
        )
        await state.clear()
    except Exception as e:
        log.exception(f"sheet_new import: {e}")
        await status_msg.edit_text(f"❌ Ошибка: {e}")
        await state.clear()
