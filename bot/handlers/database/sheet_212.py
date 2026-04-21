"""
Загрузить с проверкой: предпросмотр отчёта, подтверждение или отмена.
После подтверждения — как «new», плюс класс checked для всех затронутых.
"""
from __future__ import annotations

from pathlib import Path

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup

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


class Sheet212Flow(StatesGroup):
    waiting_file = State()
    preview = State()


def _kb_preview() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Применить", callback_data="sheet212_apply"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="sheet212_cancel"),
            ]
        ]
    )


@router.callback_query(F.data == "db_sheet_212")
async def cb_db_sheet_212(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    await safe_edit_message(
        callback.message,
        "📋 <b>Загрузить с проверкой</b>\n\n"
        "Отправьте <b>TXT</b> со списком @username.\n"
        "Сначала будет <b>отчёт</b> (новые / дубликаты / всего); "
        "импорт выполнится только после «Применить».\n\n"
        "Дубликатам начисляется <code>new</code> и <code>checked</code>.\n\n"
        "Отмена: кнопка ниже.",
        reply_markup=get_cancel_with_back_keyboard("cancel_db_sheet212", "db_sec_sheets"),
    )
    await state.set_state(Sheet212Flow.waiting_file)
    await callback.answer()


@router.callback_query(F.data == "cancel_db_sheet212")
async def cb_cancel_db_sheet212(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    await safe_edit_message(
        callback.message,
        "❌ Отменено.",
        reply_markup=kb_database_sheets(),
    )
    await callback.answer()


@router.message(Sheet212Flow.waiting_file, F.document)
async def sheet212_got_file(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return
    document = message.document
    if not document.file_name.lower().endswith(".txt"):
        await message.answer("❌ Нужен TXT-файл.")
        return
    status_msg = await message.answer("⏳ Чтение файла...")
    try:
        FILES_DIR.mkdir(parents=True, exist_ok=True)
        file_path = FILES_DIR / f"sheet212_{document.file_name}"
        file_info = await message.bot.get_file(document.file_id)
        await message.bot.download_file(file_info.file_path, file_path)
        content = file_path.read_text(encoding="utf-8")
        usernames = parse_usernames_from_txt(content)
        if not usernames:
            await status_msg.edit_text("❌ Не найдено валидных username.")
            await state.clear()
            try:
                file_path.unlink()
            except OSError:
                pass
            return
        async with session_scope() as session:
            initial = set(await ClientRepository.get_all_usernames(session))
        to_add = [u for u in sorted(usernames) if u not in initial]
        dups = [u for u in sorted(usernames) if u in initial]
        await state.update_data(
            sheet212_path=str(file_path),
            sheet212_usernames=list(usernames),
            sheet212_to_add=to_add,
            sheet212_dups=dups,
        )
        await state.set_state(Sheet212Flow.preview)
        await status_msg.edit_text(
            "📊 <b>Предпросмотр импорта</b>\n\n"
            f"• Уникальных в файле: <b>{len(usernames)}</b>\n"
            f"• Будет <b>создано</b> новых записей: <b>{len(to_add)}</b>\n"
            f"• Уже в базе (дубликаты): <b>{len(dups)}</b>\n\n"
            "Нажмите <b>Применить</b> для записи в БД или <b>Отмена</b>.",
            reply_markup=_kb_preview(),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        log.exception(f"sheet212 preview: {e}")
        await status_msg.edit_text(f"❌ Ошибка: {e}")
        await state.clear()


@router.callback_query(StateFilter(Sheet212Flow.preview), F.data == "sheet212_cancel")
async def sheet212_cancel(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔", show_alert=True)
        return
    data = await state.get_data()
    path = data.get("sheet212_path")
    await state.clear()
    if path:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass
    await callback.message.edit_text(
        "❌ Импорт отменён, временный файл удалён.",
        reply_markup=kb_database_sheets(),
    )
    await callback.answer()


@router.callback_query(StateFilter(Sheet212Flow.preview), F.data == "sheet212_apply")
async def sheet212_apply(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔", show_alert=True)
        return
    data = await state.get_data()
    to_add = data.get("sheet212_to_add") or []
    dups = data.get("sheet212_dups") or []
    path = data.get("sheet212_path")
    await state.clear()
    if path:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass
    added = 0
    tagged = 0
    try:
        async with session_scope() as session:
            if to_add:
                session.add_all([Client(username=u) for u in to_add])
                await session.commit()
                added = len(to_add)
            for u in to_add:
                c = await ClientRepository.get_by_username(session, u)
                if c:
                    await ClientClassCounterRepository.increment(session, c.id, "checked", 1)
                    tagged += 1
            for u in dups:
                c = await ClientRepository.get_by_username(session, u)
                if c:
                    await ClientClassCounterRepository.increment(session, c.id, "new", 1)
                    await ClientClassCounterRepository.increment(session, c.id, "checked", 1)
                    tagged += 1
        await callback.message.edit_text(
            "✅ <b>Импорт применён</b>\n\n"
            f"• Создано новых: <b>{added}</b>\n"
            f"• Операций классов (new/checked): <b>{tagged}</b>\n"
            f"• Дубликатов обработано: <b>{len(dups)}</b>",
            reply_markup=kb_database_sheets(),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        log.exception(f"sheet212 apply: {e}")
        await callback.message.edit_text(f"❌ Ошибка: {e}", reply_markup=kb_database_sheets())
    await callback.answer()
