"""Short account navigation screens; existing actions remain in their owners."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.types import BufferedInputFile

from bot.config import is_authorized_user
from bot.config import SESSIONS_DIR
from bot.handlers.accounts.common import safe_edit_message
from bot.keyboards.main import (
    get_account_connection_keyboard,
    get_account_options_keyboard,
    get_accounts_manage_keyboard,
)
from database.repositories import AccountRepository
from database.session import session_scope


router = Router()


async def _account(account_id: int):
    async with session_scope() as session:
        return await AccountRepository.get_by_id(session, account_id)


@router.callback_query(F.data == "accounts_manage")
async def accounts_manage(callback: CallbackQuery, state: FSMContext):
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    await safe_edit_message(
        callback.message,
        "⚙️ <b>Управление аккаунтами</b>\n\n"
        "Список, профиль, проверка и подготовка аккаунтов — отдельные короткие шаги.",
        reply_markup=get_accounts_manage_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "accounts_profile_editor")
async def accounts_profile_editor(callback: CallbackQuery, state: FSMContext):
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    from bot.handlers.accounts.profile_templates import render_profile_editor

    await render_profile_editor(callback)


@router.callback_query(F.data == "accounts_twofa")
async def accounts_twofa(callback: CallbackQuery, state: FSMContext):
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    from bot.handlers.accounts.twofa import render_twofa_menu

    await render_twofa_menu(callback.message)
    await callback.answer()


@router.callback_query(F.data.startswith("account_connection_"))
async def account_connection(callback: CallbackQuery):
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    account_id = int(callback.data.rsplit("_", 1)[-1])
    account = await _account(account_id)
    if account is None:
        await callback.answer("Аккаунт не найден", show_alert=True)
        return
    await safe_edit_message(
        callback.message,
        "🌐 <b>Подключение и проверка</b>\n\n"
        "Проверка авторизации выполняет запрос к Telegram через назначенный SOCKS5.",
        reply_markup=get_account_connection_keyboard(account),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("account_options_"))
async def account_options(callback: CallbackQuery):
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    account_id = int(callback.data.rsplit("_", 1)[-1])
    account = await _account(account_id)
    if account is None:
        await callback.answer("Аккаунт не найден", show_alert=True)
        return
    await safe_edit_message(
        callback.message,
        "⚙️ <b>Дополнительно</b>\n\n"
        "Название в боте, 2FA и удаление аккаунта.",
        reply_markup=get_account_options_keyboard(account_id),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data == "accounts_validity")
async def accounts_validity(callback: CallbackQuery):
    """Export one real authorization result per account without exposing sessions."""
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await callback.answer()
    progress = await callback.message.answer("🔍 Проверяю авторизацию аккаунтов через их прокси…")
    async with session_scope() as session:
        accounts = await AccountRepository.get_all(session)
    from workers.manager import account_worker_for_action

    lines = ["CoreBot V2 — проверка аккаунтов", ""]
    for account in accounts:
        path = SESSIONS_DIR / f"{account.session_name}.session"
        if account.proxy is None:
            result = "НЕ ПРОВЕРЕН: нет SOCKS5"
        elif not path.exists():
            result = "НЕ ПРОВЕРЕН: нет файла сессии"
        else:
            worker = account_worker_for_action(account, path, account.proxy)
            try:
                if not await worker.connect(quiet=True) or not worker.client:
                    result = "НЕ ПРОВЕРЕН: подключение не удалось или сессия занята"
                elif await worker.client.is_user_authorized():
                    result = "ВАЛИДЕН"
                else:
                    result = "НЕ АВТОРИЗОВАН"
            except Exception as exc:
                result = f"ОШИБКА: {type(exc).__name__}"
            finally:
                await worker.disconnect()
        lines.append(
            f"#{account.id} | {account.phone or '-'} | @{account.username or '-'} | "
            f"{account.list_label or '-'} | {result}"
        )
    report = ("\ufeff" + "\n".join(lines) + "\n").encode("utf-8")
    await progress.delete()
    await callback.message.answer_document(
        BufferedInputFile(report, filename="corebot-account-validity.txt"),
        caption=f"Готово: проверено {len(accounts)} аккаунтов. Неудачное подключение не означает бан.",
        reply_markup=get_accounts_manage_keyboard(),
    )
