"""Список аккаунтов, карточка, перепроверка авторизации."""
from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.config import OWNER_ID, SESSIONS_DIR
from bot.handlers.accounts.common import build_account_card_text, safe_edit_message, show_account_card
from bot.handlers.accounts.states import AccountListLabelFSM
from bot.keyboards.main import (
    ACCOUNTS_LIST_PAGE_SIZE,
    get_account_card_keyboard,
    get_accounts_list_keyboard,
    get_cancel_with_back_keyboard,
    get_context_back_keyboard,
)
from database.models import AccountStatus
from database.repository import db
from database.session import session_scope
from database.repositories import AccountRepository
from utils.logger import log

router = Router()


def _accounts_list_page_from_data(data: str) -> int:
    if data == "accounts_list":
        return 0
    if data.startswith("accounts_list_p_"):
        return int(data.rsplit("_", 1)[-1])
    return 0


@router.callback_query(F.data == "accounts_list_page_info")
async def cb_accounts_list_page_info(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔", show_alert=True)
        return
    await callback.answer("Номер страницы · листайте ◀ ▶", show_alert=True)


@router.callback_query(F.data == "accounts_list")
@router.callback_query(F.data.startswith("accounts_list_p_"))
async def cb_accounts_list(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await callback.answer()

    page = _accounts_list_page_from_data(callback.data)

    try:
        async with session_scope() as session:
                accounts = await AccountRepository.get_all(session)

        if not accounts:
            await safe_edit_message(
                callback.message,
                "📋 <b>Список аккаунтов</b>\n\n"
                "Аккаунтов пока нет.\n"
                "Загрузите Tdata для добавления.",
                reply_markup=get_context_back_keyboard("menu_accounts"),
            )
            return

        total = len(accounts)
        total_pages = max(1, (total + ACCOUNTS_LIST_PAGE_SIZE - 1) // ACCOUNTS_LIST_PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))
        start = page * ACCOUNTS_LIST_PAGE_SIZE + 1
        end = min((page + 1) * ACCOUNTS_LIST_PAGE_SIZE, total)

        await safe_edit_message(
            callback.message,
            "📋 <b>Список аккаунтов:</b>\n\n"
            f"Всего: {total}\n"
            f"Страница {page + 1} из {total_pages} · строки {start}–{end}\n\n"
            "Нажмите на аккаунт для просмотра деталей:",
            reply_markup=get_accounts_list_keyboard(accounts, page=page),
        )

    except Exception as e:
        log.error(f"Ошибка при получении списка аккаунтов: {e}")
        await safe_edit_message(
            callback.message,
            "⚠️ <b>Возникла проблема с базой данных</b>\n\n"
            "Попробуйте ещё раз через несколько секунд.",
            reply_markup=get_context_back_keyboard("accounts_list"),
        )


@router.callback_query(F.data.startswith("account_view_"))
async def cb_account_view(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await state.clear()
    await callback.answer()

    account_id: int | None = None
    try:
        account_id = int(callback.data.split("_")[-1])

        async with db.async_session_maker() as session:
            account = await AccountRepository.get_by_id(session, account_id)

        if not account:
            await safe_edit_message(
                callback.message,
                "❌ Аккаунт не найден.",
                reply_markup=get_context_back_keyboard("accounts_list"),
            )
            return

        # Только данные из БД — без Telethon.connect() при открытии карточки.
        await safe_edit_message(
            callback.message,
            build_account_card_text(
                account,
                is_authorized=False,
                auth_status="",
                photo_count=-1,
                db_only=True,
            ),
            reply_markup=get_account_card_keyboard(account, None),
            parse_mode=ParseMode.HTML,
        )

    except Exception as e:
        log.error(f"Ошибка при открытии карточки аккаунта {account_id}: {e}")
        await safe_edit_message(
            callback.message,
            "⚠️ <b>Ошибка при загрузке карточки</b>\n\n"
            f"Аккаунт #{account_id}\n\n"
            "Попробуйте ещё раз или проверьте логи.",
            reply_markup=get_context_back_keyboard("accounts_list"),
        )


@router.callback_query(F.data.startswith("account_list_label_"))
async def cb_account_list_label_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    account_id = int(callback.data.split("_")[-1])
    await state.update_data(account_id=account_id)
    await state.set_state(AccountListLabelFSM.waiting_for_label)

    async with db.async_session_maker() as session:
        account = await AccountRepository.get_by_id(session, account_id)
    cur = ""
    if account and getattr(account, "list_label", None) and str(account.list_label).strip():
        cur = f"\n\nСейчас: <code>{str(account.list_label).strip()}</code>"

    await safe_edit_message(
        callback.message,
        "🏷 <b>Название в списке</b>\n\n"
        "Короткое имя только в этом боте (профиль Telegram не меняется).\n\n"
        "До 64 символов. Чтобы снова показывать username или телефон — отправьте "
        "<code>-</code> или <code>сброс</code>."
        f"{cur}\n\n"
        "❌ Отмена: /start",
        reply_markup=get_cancel_with_back_keyboard(
            "cancel_accounts", f"account_view_{account_id}"
        ),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.message(AccountListLabelFSM.waiting_for_label)
async def process_account_list_label(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return

    text = (message.text or "").strip()
    if text.lower() in ("/start", "отмена", "cancel"):
        await state.clear()
        await message.answer("❌ Отменено.")
        return

    data = await state.get_data()
    account_id = data.get("account_id")
    if not account_id:
        await state.clear()
        await message.answer("❌ Сессия сценария сброшена. Откройте карточку снова.")
        return

    if text in ("-", "—", "сброс", "clear"):
        new_label = None
    else:
        new_label = text[:64]

    try:
        async with session_scope() as session:
            await AccountRepository.update_list_label(session, account_id, new_label)
            account = await AccountRepository.get_by_id(session, account_id)

        if not account:
            await message.answer("❌ Аккаунт не найден.")
            await state.clear()
            return

        await message.answer(
            "✅ <b>Название в списке обновлено.</b>\n\n"
            + build_account_card_text(account, False, "", photo_count=-1, db_only=True),
            reply_markup=get_account_card_keyboard(account, None),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        log.error(f"list_label: {e}")
        await message.answer(f"❌ Ошибка: {e}")
    finally:
        await state.clear()


@router.callback_query(F.data.startswith("account_recheck_auth_"))
async def cb_account_recheck_auth(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await callback.answer()

    account_id = int(callback.data.split("_")[-1])
    status_msg = await callback.message.answer("🔄 Проверка авторизации...")

    try:
        async with db.async_session_maker() as session:
            account = await AccountRepository.get_by_id(session, account_id)

        if not account:
            await safe_edit_message(status_msg, "❌ Аккаунт не найден.", reply_markup=get_context_back_keyboard("accounts_list"))
            return

        from workers.manager import Worker

        session_path = SESSIONS_DIR / f"{account.session_name}.session"
        is_authorized = False
        auth_status = "❓ Не проверено"

        if session_path.exists():
            temp_worker = Worker(account, session_path, account.proxy)
            connected = await temp_worker.connect()

            if connected and temp_worker.client:
                is_authorized = await temp_worker.client.is_user_authorized()
                auth_status = "✅ Авторизован" if is_authorized else "❌ Не авторизован"

                async with session_scope() as session:
                        if is_authorized and account.status == AccountStatus.INACTIVE:
                            await AccountRepository.update_status(session, account.id, AccountStatus.ACTIVE)
                            account.status = AccountStatus.ACTIVE
                        elif not is_authorized and account.status != AccountStatus.INACTIVE:
                            await AccountRepository.update_status(session, account.id, AccountStatus.INACTIVE)
                            account.status = AccountStatus.INACTIVE

                await temp_worker.disconnect()
            else:
                auth_status = "❌ Не удалось подключиться"
        else:
            auth_status = "❌ Файл сессии не найден"

        await show_account_card(status_msg, account, is_authorized, auth_status, photo_count=-1, send_new=False)

    except Exception as e:
        log.error(f"Ошибка при перепроверке авторизации аккаунта {account_id}: {e}")
        await safe_edit_message(
            status_msg if status_msg else callback.message,
            f"❌ Ошибка проверки: {e}",
            reply_markup=get_context_back_keyboard("accounts_list"),
        )
