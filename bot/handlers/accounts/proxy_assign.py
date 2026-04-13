"""Назначение прокси аккаунту и переподключение Worker."""
from aiogram import F, Router
from aiogram.types import CallbackQuery

from bot.config import OWNER_ID
from bot.handlers.accounts.common import safe_edit_message
from bot.keyboards.main import get_context_back_keyboard, get_proxy_group_assign_keyboard
from database.models import AccountStatus, MailingStatus
from database.repository import db
from database.session import session_scope
from database.repositories import (
    AccountRepository,
    MailingRepository,
    ProxyRepository,
    ProxyGroupRepository,
)
from utils.logger import log

router = Router()


@router.callback_query(F.data.startswith("account_change_proxy_"))
async def cb_account_change_proxy(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    account_id = int(callback.data.split("_")[-1])

    try:
        async with session_scope() as session:
                account = await AccountRepository.get_by_id(session, account_id)
                groups_usage = await ProxyGroupRepository.list_with_usage(session)

        if not account:
            await safe_edit_message(
                callback.message,
                "❌ Аккаунт не найден.",
                reply_markup=get_context_back_keyboard("accounts_list"),
            )
            await callback.answer()
            return

        async with session_scope() as session:
                running = await MailingRepository.get_running(session)

        if running:
            await callback.answer(
                "⚠️ Нельзя сменить прокси во время активной рассылки. Остановите рассылку.",
                show_alert=True,
            )
            return

        if not groups_usage:
            await safe_edit_message(
                callback.message,
                "📭 Нет групп прокси.\n\n"
                "Сначала добавьте прокси массово в группу в разделе «Прокси».",
                reply_markup=get_context_back_keyboard(f"account_view_{account_id}"),
            )
            await callback.answer()
            return

        await safe_edit_message(
            callback.message,
            f"🌐 <b>Выберите группу прокси для аккаунта</b> "
            f"<code>{account.username or account.phone}</code>:",
            reply_markup=get_proxy_group_assign_keyboard(account_id, groups_usage),
        )
        await callback.answer()

    except Exception as e:
        log.error(f"Ошибка при открытии выбора прокси: {e}")
        await callback.answer("⚠️ Ошибка. Попробуйте ещё раз.")


@router.callback_query(F.data.startswith("proxy_group_assign_"))
async def cb_proxy_assign(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    parts = callback.data.split("_")
    account_id = int(parts[3])
    group_id_str = parts[4]
    group_id = None if group_id_str == "none" else int(group_id_str)

    try:
        async with session_scope() as session:
                account = await AccountRepository.get_by_id(session, account_id)
                proxy_id = None
                if group_id:
                    picked = await ProxyGroupRepository.acquire_next_free_proxy(session, group_id)
                    if not picked:
                        await callback.answer("В группе нет свободных прокси", show_alert=True)
                        return
                    proxy_id = picked.id
                    proxy_name = picked.name
                else:
                    proxy_name = "Без прокси"

                await AccountRepository.set_proxy(session, account_id, proxy_id)
                await AccountRepository.update_status(session, account_id, AccountStatus.INACTIVE)

        from workers.manager import worker_manager

        await worker_manager.reconnect_account(account_id)

        async with db.async_session_maker() as session:
            account = await AccountRepository.get_by_id(session, account_id)

        if not account:
            await callback.answer("❌ Аккаунт не найден после смены прокси.", show_alert=True)
            return

        await safe_edit_message(
            callback.message,
            f"✅ <b>Прокси изменён</b>\n\n"
            f"👤 Аккаунт: <code>{account.username or account.phone}</code>\n"
            f"🌐 Новый прокси: {proxy_name}\n\n"
            f"⚠️ Статус аккаунта сброшен на INACTIVE.\n"
            f"Откройте карточку аккаунта для повторной проверки авторизации.",
            reply_markup=get_context_back_keyboard(f"account_view_{account_id}"),
        )
        await callback.answer()

    except Exception as e:
        log.error(f"Ошибка при назначении прокси: {e}")
        await callback.answer("⚠️ Ошибка при назначении прокси.", show_alert=True)
