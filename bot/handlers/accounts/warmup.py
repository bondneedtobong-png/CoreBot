"""Управление прогревом аккаунта из карточки."""
from aiogram import F, Router
from aiogram.types import CallbackQuery

from bot.config import OWNER_ID
from bot.handlers.accounts.common import show_account_card
from database.repository import db
from database.repositories import AccountRepository
from utils.logger import log

router = Router()


@router.callback_query(F.data.startswith("account_warmup_toggle_"))
async def cb_account_warmup_toggle(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    account_id = int(callback.data.split("_")[-1])
    try:
        async with db.async_session_maker() as session:
            account = await AccountRepository.get_by_id(session, account_id)
            if not account:
                await callback.answer("Аккаунт не найден", show_alert=True)
                return
            new_enabled = not bool(account.warmup_enabled)
            await AccountRepository.update_warmup_settings(
                session,
                account_id=account_id,
                enabled=new_enabled,
                profile=account.warmup_profile or "safe",
            )
            account = await AccountRepository.get_by_id(session, account_id)
        await show_account_card(
            callback.message,
            account,
            is_authorized=(account.status.value == "active"),
            auth_status="",
            photo_count=-1,
            send_new=False,
            db_only=True,
        )
        await callback.answer("Прогрев обновлён")
    except Exception as e:
        log.error(f"warmup toggle error for account {account_id}: {e}")
        await callback.answer("Ошибка переключения прогрева", show_alert=True)
