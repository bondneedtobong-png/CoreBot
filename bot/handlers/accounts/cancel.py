"""Отмена сценариев аккаунтов (callback cancel_accounts — не пересекается с рассылкой/прокси)."""
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from bot.config import is_authorized_user
from bot.handlers.accounts.common import safe_edit_message
from bot.keyboards.main import get_accounts_keyboard

router = Router()


@router.callback_query(F.data == "cancel_accounts")
async def cb_cancel_accounts(callback: CallbackQuery, state: FSMContext):
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    data = await state.get_data()
    extract_dir = data.get("extract_dir")
    if extract_dir:
        from pathlib import Path
        from bot.handlers.accounts.tdata_v2 import _discard_extract

        _discard_extract(Path(extract_dir))
    await state.clear()
    await safe_edit_message(
        callback.message,
        "❌ <b>Отменено</b>\n\n"
        "Операция отменена.",
        reply_markup=get_accounts_keyboard(),
    )
    await callback.answer()
