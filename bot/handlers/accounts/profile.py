"""Меню и FSM: имя, bio, username аккаунта (Telethon Worker)."""
from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.config import OWNER_ID, SESSIONS_DIR
from bot.handlers.accounts.common import build_account_card_text, safe_edit_message
from bot.handlers.accounts.states import EditProfileFSM
from bot.keyboards.main import (
    get_account_card_keyboard,
    get_cancel_with_back_keyboard,
    get_edit_profile_keyboard,
)
from database.repository import db
from database.repositories import AccountRepository
from utils.logger import log

router = Router()


@router.callback_query(F.data.startswith("account_edit_profile_"))
async def cb_edit_profile(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await state.clear()
    account_id = int(callback.data.split("_")[-1])
    await safe_edit_message(
        callback.message,
        "✏️ <b>Имя, bio и username</b>\n\n"
        "Выберите поле для изменения.\n"
        "Фотографии профиля — в разделе «🖼 Управление аватарками» в карточке аккаунта.",
        reply_markup=get_edit_profile_keyboard(account_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("account_edit_name_"))
async def cb_edit_name(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    account_id = int(callback.data.split("_")[-1])
    await state.update_data(
        account_id=account_id,
        original_message_id=callback.message.message_id,
        original_chat_id=callback.message.chat.id,
    )
    await state.set_state(EditProfileFSM.waiting_for_name)
    await safe_edit_message(
        callback.message,
        "✏️ <b>Изменение имени</b>\n\n"
        "Введите имя и фамилию в одном сообщении:\n"
        "Формат: <code>Имя Фамилия</code>\n"
        "(фамилию можно не указывать)\n\n"
        "❌ Отмена: /start",
        reply_markup=get_cancel_with_back_keyboard(
            "cancel_accounts", f"account_edit_profile_{account_id}"
        ),
    )
    await callback.answer()


@router.message(EditProfileFSM.waiting_for_name)
async def process_edit_name(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return

    full_name = (message.text or "").strip()
    if full_name.lower() in ("/start", "отмена", "cancel"):
        await state.clear()
        await message.answer("❌ Редактирование отменено.", parse_mode=ParseMode.HTML)
        return

    parts = full_name.split(maxsplit=1)
    first_name = parts[0]
    last_name = parts[1] if len(parts) > 1 else None

    data = await state.get_data()
    account_id = data.get("account_id")
    if not account_id:
        await message.answer("❌ Ошибка: данные потеряны. Попробуйте снова.")
        await state.clear()
        return

    temp_worker = None
    try:
        async with db.async_session_maker() as session:
            account = await AccountRepository.get_by_id(session, account_id)

        if not account:
            await message.answer("❌ Аккаунт не найден.")
            await state.clear()
            return

        from workers.manager import Worker

        session_path = SESSIONS_DIR / f"{account.session_name}.session"
        if not session_path.exists():
            await message.answer("❌ Файл сессии не найден.")
            await state.clear()
            return

        temp_worker = Worker(account, session_path, account.proxy)
        connected = await temp_worker.connect()
        if not connected or not temp_worker.client:
            await message.answer("❌ Не удалось подключить аккаунт.")
            await state.clear()
            return

        from telethon.tl.functions.account import UpdateProfileRequest

        await temp_worker.client(UpdateProfileRequest(first_name=first_name, last_name=last_name))
        log.info(f"Аккаунт {account_id}: имя обновлено на '{first_name} {last_name or ''}'")

        async with db.async_session_maker() as session:
            await AccountRepository.update_profile(session, account_id, first_name=first_name, last_name=last_name)
            account = await AccountRepository.get_by_id(session, account_id)

        await state.clear()
        await message.answer(
            "✅ <b>Имя обновлено!</b>\n\n" + build_account_card_text(account, True, "✅ Авторизован"),
            reply_markup=get_account_card_keyboard(account, True),
            parse_mode=ParseMode.HTML,
        )

    except Exception as e:
        log.error(f"Ошибка обновления имени: {e}")
        await message.answer(f"❌ Ошибка: {e}")
    finally:
        if temp_worker:
            try:
                await temp_worker.disconnect()
            except Exception:
                pass


@router.callback_query(F.data.startswith("account_edit_bio_"))
async def cb_edit_bio(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    account_id = int(callback.data.split("_")[-1])
    await state.update_data(
        account_id=account_id,
        original_message_id=callback.message.message_id,
        original_chat_id=callback.message.chat.id,
    )
    await state.set_state(EditProfileFSM.waiting_for_bio)
    await safe_edit_message(
        callback.message,
        "📝 <b>Изменение Bio</b>\n\n"
        "Введите новое описание (Bio):\n\n"
        "❌ Отмена: /start",
        reply_markup=get_cancel_with_back_keyboard(
            "cancel_accounts", f"account_edit_profile_{account_id}"
        ),
    )
    await callback.answer()


@router.message(EditProfileFSM.waiting_for_bio)
async def process_edit_bio(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return

    new_bio = (message.text or "").strip()
    if new_bio.lower() in ("/start", "отмена", "cancel"):
        await state.clear()
        await message.answer("❌ Редактирование отменено.", parse_mode=ParseMode.HTML)
        return

    data = await state.get_data()
    account_id = data.get("account_id")
    if not account_id:
        await message.answer("❌ Ошибка: данные потеряны. Попробуйте снова.")
        await state.clear()
        return

    temp_worker = None
    try:
        async with db.async_session_maker() as session:
            account = await AccountRepository.get_by_id(session, account_id)

        if not account:
            await message.answer("❌ Аккаунт не найден.")
            await state.clear()
            return

        from workers.manager import Worker

        session_path = SESSIONS_DIR / f"{account.session_name}.session"
        if not session_path.exists():
            await message.answer("❌ Файл сессии не найден.")
            await state.clear()
            return

        temp_worker = Worker(account, session_path, account.proxy)
        connected = await temp_worker.connect()
        if not connected or not temp_worker.client:
            await message.answer("❌ Не удалось подключить аккаунт.")
            await state.clear()
            return

        from telethon.tl.functions.account import UpdateProfileRequest

        await temp_worker.client(UpdateProfileRequest(about=new_bio))
        log.info(f"Аккаунт {account_id}: Bio обновлено через API")

        async with db.async_session_maker() as session:
            await AccountRepository.update_profile(session, account_id, bio=new_bio)
            account = await AccountRepository.get_by_id(session, account_id)

        await state.clear()
        await message.answer(
            "✅ <b>Bio обновлено!</b>\n\n" + build_account_card_text(account, True, "✅ Авторизован"),
            reply_markup=get_account_card_keyboard(account, True),
            parse_mode=ParseMode.HTML,
        )

    except Exception as e:
        log.error(f"Ошибка обновления Bio: {e}")
        await message.answer(f"❌ Ошибка: {e}")
    finally:
        if temp_worker:
            try:
                await temp_worker.disconnect()
            except Exception:
                pass


@router.callback_query(F.data.startswith("account_edit_username_"))
async def cb_edit_username(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    account_id = int(callback.data.split("_")[-1])
    await state.update_data(
        account_id=account_id,
        original_message_id=callback.message.message_id,
        original_chat_id=callback.message.chat.id,
    )
    await state.set_state(EditProfileFSM.waiting_for_username)
    await safe_edit_message(
        callback.message,
        "📛 <b>Изменение Username</b>\n\n"
        "Введите новый @username (без @):\n\n"
        "❌ Отмена: /start",
        reply_markup=get_cancel_with_back_keyboard(
            "cancel_accounts", f"account_edit_profile_{account_id}"
        ),
    )
    await callback.answer()


@router.message(EditProfileFSM.waiting_for_username)
async def process_edit_username(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return

    new_username = (message.text or "").strip().lstrip("@")
    if new_username.lower() in ("/start", "отмена", "cancel"):
        await state.clear()
        await message.answer("❌ Редактирование отменено.", parse_mode=ParseMode.HTML)
        return

    data = await state.get_data()
    account_id = data.get("account_id")
    if not account_id:
        await message.answer("❌ Ошибка: данные потеряны. Попробуйте снова.")
        await state.clear()
        return

    temp_worker = None
    try:
        async with db.async_session_maker() as session:
            account = await AccountRepository.get_by_id(session, account_id)

        if not account:
            await message.answer("❌ Аккаунт не найден.")
            await state.clear()
            return

        from workers.manager import Worker
        from telethon.errors import FloodWaitError, UsernameInvalidError, UsernameOccupiedError
        from telethon.tl.functions.account import UpdateUsernameRequest

        session_path = SESSIONS_DIR / f"{account.session_name}.session"
        if not session_path.exists():
            await message.answer("❌ Файл сессии не найден.")
            await state.clear()
            return

        temp_worker = Worker(account, session_path, account.proxy)
        connected = await temp_worker.connect()
        if not connected or not temp_worker.client:
            await message.answer("❌ Не удалось подключить аккаунт.")
            await state.clear()
            return

        try:
            await temp_worker.client(UpdateUsernameRequest(username=new_username))
            log.info(f"Аккаунт {account_id}: username обновлён на '@{new_username}'")
        except UsernameOccupiedError:
            await message.answer(
                f"❌ Username <b>@{new_username}</b> уже занят.\n\nПопробуйте другой вариант.",
                parse_mode=ParseMode.HTML,
            )
            await state.clear()
            return
        except UsernameInvalidError:
            await message.answer(
                f"❌ Username <b>@{new_username}</b> недопустим.\n\n"
                "Разрешены: буквы, цифры, подчёркивание. Минимум 5 символов.",
                parse_mode=ParseMode.HTML,
            )
            await state.clear()
            return
        except FloodWaitError as e:
            await message.answer(
                f"⏳ FloodWait: подождите {e.seconds} сек и попробуйте снова.",
                parse_mode=ParseMode.HTML,
            )
            await state.clear()
            return

        async with db.async_session_maker() as session:
            await AccountRepository.update_username(session, account_id, new_username)
            account = await AccountRepository.get_by_id(session, account_id)

        await state.clear()
        await message.answer(
            "✅ <b>Username обновлён!</b>\n\n" + build_account_card_text(account, True, "✅ Авторизован"),
            reply_markup=get_account_card_keyboard(account, True),
            parse_mode=ParseMode.HTML,
        )

    except Exception as e:
        log.error(f"Ошибка обновления username: {e}")
        await message.answer(f"❌ Ошибка: {e}")
    finally:
        if temp_worker:
            try:
                await temp_worker.disconnect()
            except Exception:
                pass
