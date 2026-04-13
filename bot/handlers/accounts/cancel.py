"""Отмена сценариев аккаунтов (callback cancel_accounts — не пересекается с рассылкой/прокси)."""
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from bot.config import OWNER_ID
from bot.handlers.accounts.common import safe_edit_message
from bot.keyboards.main import get_accounts_keyboard

router = Router()


@router.callback_query(F.data == "cancel_accounts")
async def cb_cancel_accounts(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await state.clear()
    await safe_edit_message(
        callback.message,
        "❌ <b>Отменено</b>\n\n"
        "Операция отменена.",
        reply_markup=get_accounts_keyboard(),
    )
    await callback.answer()
