"""Редактирование тегов аккаунта (только БД)."""
from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.config import OWNER_ID
from bot.handlers.accounts.common import safe_edit_message
from bot.handlers.accounts.states import EditTagsFSM
from bot.keyboards.main import get_context_back_keyboard
from database.repository import db
from database.session import session_scope
from database.repositories import AccountRepository
from utils.logger import log

router = Router()


@router.callback_query(F.data.startswith("account_edit_tags_"))
async def cb_account_edit_tags(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    account_id = int(callback.data.split("_")[-1])

    try:
        async with session_scope() as session:
                account = await AccountRepository.get_by_id(session, account_id)

        if not account:
            await safe_edit_message(
                callback.message,
                "❌ Аккаунт не найден.",
                reply_markup=get_context_back_keyboard("accounts_list"),
            )
            await callback.answer()
            return

        current_tags = account.tags.strip(",") if account.tags else "не заданы"

        await safe_edit_message(
            callback.message,
            f"🏷 <b>Редактирование тегов</b>\n\n"
            f"👤 Аккаунт: <code>{account.username or account.phone}</code>\n"
            f"📋 Текущие теги: <b>{current_tags}</b>\n\n"
            f"Введите теги через запятую.\n"
            f"Например: <code>USA, Warmup, Main</code>\n\n"
            f"Для удаления всех тегов отправьте: <code>-</code>",
            reply_markup=get_context_back_keyboard(f"account_view_{account_id}"),
        )
        await state.update_data(
            account_id=account_id,
            original_message_id=callback.message.message_id,
            original_chat_id=callback.message.chat.id,
        )
        await state.set_state(EditTagsFSM.waiting_for_tags)
        await callback.answer()

    except Exception as e:
        log.error(f"Ошибка при открытии редактирования тегов: {e}")
        await callback.answer("⚠️ Ошибка. Попробуйте ещё раз.")


@router.message(EditTagsFSM.waiting_for_tags)
async def handle_set_tags(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return

    data = await state.get_data()
    account_id = data.get("account_id")

    if not account_id:
        await message.answer("❌ Ошибка: данные потеряны. Попробуйте снова.")
        await state.clear()
        return

    tags_str = (message.text or "").strip()
    if tags_str == "-":
        tags_str = ""

    try:
        async with db.async_session_maker() as session:
            await AccountRepository.set_tags(session, account_id, tags_str)
            account = await AccountRepository.get_by_id(session, account_id)

        kb = get_context_back_keyboard(f"account_view_{account_id}")

        if account.tags:
            tags_display = account.tags.strip(",")
            result_text = (
                f"✅ <b>Теги обновлены</b>\n\n"
                f"👤 Аккаунт: <code>{account.username or account.phone}</code>\n"
                f"🏷 Новые теги: <b>{tags_display}</b>"
            )
        else:
            result_text = (
                f"✅ <b>Теги удалены</b>\n\n"
                f"👤 Аккаунт: <code>{account.username or account.phone}</code>"
            )

        await message.answer(result_text, reply_markup=kb, parse_mode=ParseMode.HTML)

    except Exception as e:
        log.error(f"Ошибка при сохранении тегов: {e}")
        await message.answer("⚠️ Ошибка при сохранении тегов.")

    await state.clear()
