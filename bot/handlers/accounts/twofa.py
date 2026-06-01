"""Установка 2FA через WorkerManager (Telethon)."""
from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.config import OWNER_ID
from bot.handlers.accounts.common import safe_edit_message
from bot.handlers.accounts.states import Set2FAFSM
from bot.keyboards.main import get_cancel_with_back_keyboard
from utils.logger import log

router = Router()


@router.callback_query(F.data.startswith("account_set_2fa_"))
async def cb_set_2fa(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    account_id = int(callback.data.split("_")[-1])
    await state.update_data(account_id=account_id)
    await state.set_state(Set2FAFSM.waiting_for_password)

    await safe_edit_message(
        callback.message,
        "🔐 <b>Установка 2FA</b>\n\n"
        "Введите пароль для двухфакторной авторизации:\n\n"
        "⚠️ <b>Внимание!</b> После установки 2FA все остальные сессии будут завершены.\n\n"
        "❌ Отмена: /start",
        reply_markup=get_cancel_with_back_keyboard(
            "cancel_accounts", f"account_view_{account_id}"
        ),
    )
    await callback.answer()


@router.message(Set2FAFSM.waiting_for_password)
async def process_2fa_password(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return

    password = (message.text or "").strip()
    if password.lower() in ("/start", "отмена", "cancel"):
        await state.clear()
        await message.answer("❌ Установка 2FA отменена.")
        return

    await state.update_data(password=password)
    await state.set_state(Set2FAFSM.waiting_for_password_confirm)

    data = await state.get_data()
    account_id = data.get("account_id")
    await message.answer(
        "🔐 <b>Подтверждение пароля</b>\n\n"
        "Введите пароль ещё раз для подтверждения:",
        reply_markup=get_cancel_with_back_keyboard(
            "cancel_accounts", f"account_view_{account_id}"
        ),
        parse_mode=ParseMode.HTML,
    )


@router.message(Set2FAFSM.waiting_for_password_confirm)
async def process_2fa_confirm(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return

    confirm_password = (message.text or "").strip()
    if confirm_password.lower() in ("/start", "отмена", "cancel"):
        await state.clear()
        await message.answer("❌ Установка 2FA отменена.")
        return

    data = await state.get_data()
    account_id = data.get("account_id")
    password = data.get("password")

    if password != confirm_password:
        await message.answer("❌ Пароли не совпадают. Попробуйте ещё раз:")
        await state.set_state(Set2FAFSM.waiting_for_password)
        return

    status_msg = await message.answer("⏳ Установка 2FA...")

    try:
        from telethon import functions

        from workers.manager import worker_manager

        # Только нужный аккаунт: не вызываем connect_all() — иначе в логах и сети
        # поднимаются все загруженные аккаунты.
        await worker_manager.ensure_workers_for_account_ids([account_id])

        worker = worker_manager.workers.get(account_id)
        if not worker:
            await safe_edit_message(status_msg, "❌ Аккаунт не найден.")
            return

        if not worker.is_connected:
            ok_connect = await worker.connect()
            if not ok_connect or not worker.is_connected:
                await safe_edit_message(
                    status_msg,
                    "❌ Не удалось подключить аккаунт. Проверьте сессию и прокси.",
                )
                return

        # Telethon: в новых слоях API — edit_2fa(), а не UpdatePasswordSettingsRequest(new_password=...)
        ok = await worker.client.edit_2fa(
            new_password=password,
            hint="Восстановление через поддержку",
        )
        if not ok:
            await safe_edit_message(
                status_msg,
                "❌ Не удалось установить пароль. Если 2FA уже включена, сначала укажите текущий пароль в Telegram.",
            )
            return

        await worker.client(functions.auth.ResetAuthorizationsRequest())

        await safe_edit_message(
            status_msg,
            "✅ <b>2FA установлена!</b>\n\n"
            "Пароль двухфакторной авторизации установлен.\n"
            "Все остальные сессии завершены.\n\n"
            "⚠️ Не забудьте сохранить пароль!",
        )

        log.info(f"Аккаунт {account_id}: 2FA установлена")

        await worker.disconnect()

    except Exception as e:
        log.error(f"Ошибка установки 2FA: {e}")
        await safe_edit_message(status_msg, f"❌ Ошибка при установке 2FA: {e}")

    finally:
        await state.clear()
