"""Смена membership и удаление аккаунта (подтверждение + фактическое удаление)."""
import os
from pathlib import Path

from aiogram import F, Router
from aiogram.types import CallbackQuery

from bot.config import OWNER_ID, SESSIONS_DIR
from bot.handlers.accounts.common import safe_edit_message
from bot.keyboards.main import get_account_card_keyboard, get_accounts_keyboard, get_confirm_delete_keyboard
from database.models import Membership
from database.repository import db
from database.session import session_scope
from database.repositories import AccountRepository
from utils.logger import log

router = Router()


@router.callback_query(F.data.startswith("account_change_membership_"))
async def cb_change_membership(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    account_id = int(callback.data.split("_")[-1])

    async with session_scope() as session:
            account = await AccountRepository.get_by_id(session, account_id)

            if not account:
                await callback.message.answer("❌ Аккаунт не найден.")
                await callback.answer()
                return

            membership_order = [Membership.READY, Membership.WARMUP, Membership.TEST]
            current_index = membership_order.index(account.membership)
            new_membership = membership_order[(current_index + 1) % len(membership_order)]

            await AccountRepository.set_membership(session, account_id, new_membership)
            account.membership = new_membership

    membership_emoji = {"READY": "✅", "WARMUP": "🔥", "TEST": "🧪"}.get(new_membership.value, "⚪")
    membership_name = {"READY": "Готов", "WARMUP": "Прогрев", "TEST": "Тест"}.get(
        new_membership.value, new_membership.value
    )

    await safe_edit_message(
        callback.message,
        f"✅ <b>Принадлежность изменена</b>\n\n"
        f"Новый статус: {membership_emoji} {membership_name}",
        reply_markup=get_account_card_keyboard(account),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("account_delete_confirm_"))
async def cb_delete_confirm(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    account_id = int(callback.data.split("_")[-1])

    await safe_edit_message(
        callback.message,
        "⚠️ <b>Подтверждение удаления</b>\n\n"
        f"Вы уверены, что хотите удалить аккаунт #{account_id}?\n\n"
        "🗑 Будет удалено:\n"
        "• Запись из базы данных\n"
        "• Файл сессии .session\n\n"
        "Это действие нельзя отменить.",
        reply_markup=get_confirm_delete_keyboard(account_id),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^account_delete_\d+$"))
async def cb_delete_account(callback: CallbackQuery):
    """
    Окончательное удаление. Callback строго `account_delete_{id}`, без `confirm`,
    чтобы не пересекаться с account_delete_confirm_{id}.
    """
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    account_id = int(callback.data.split("_")[-1])
    deleted = False
    account_label = ""

    async with session_scope() as session:
            account = await AccountRepository.get_by_id(session, account_id)

            if account:
                account_label = account.username or account.phone
                session_path = SESSIONS_DIR / f"{account.session_name}.session"
                try:
                    if session_path.exists():
                        os.remove(session_path)
                        log.info(f"Удалён файл сессии: {session_path}")
                except Exception as e:
                    log.warning(f"Не удалось удалить сессию: {e}")

                await AccountRepository.delete(session, account_id)
                log.info(f"Удалён аккаунт {account_id} ({account_label})")
                deleted = True

    from workers.manager import worker_manager

    if account_id in worker_manager.workers:
        worker = worker_manager.workers[account_id]
        await worker.disconnect()
        del worker_manager.workers[account_id]

    if deleted:
        await safe_edit_message(
            callback.message,
            f"🗑 Аккаунт #{account_id} ({account_label}) удалён.",
            reply_markup=get_accounts_keyboard(),
        )
    else:
        await safe_edit_message(
            callback.message,
            "❌ Аккаунт не найден или уже был удалён.",
            reply_markup=get_accounts_keyboard(),
        )

    await callback.answer()
