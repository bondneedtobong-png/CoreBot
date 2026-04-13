"""Управление фото профиля userbot: список, добавление, удаление по номеру."""
from pathlib import Path

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.config import AVATARS_TEMP_DIR, OWNER_ID, SESSIONS_DIR
from bot.handlers.accounts.common import safe_edit_message
from bot.handlers.accounts.states import AccountPhotoManagement
from bot.keyboards.main import (
    get_accounts_keyboard,
    get_confirm_photo_delete_keyboard,
    get_context_back_keyboard,
    get_photo_management_keyboard,
)
from database.repository import db
from database.repositories import AccountRepository
from utils.logger import log

router = Router()


def format_profile_photos_caption(username: str | None, photos: list[dict]) -> str:
    if username:
        uname_display = username if username.startswith("@") else f"@{username}"
    else:
        uname_display = "(username не задан)"

    if not photos:
        return (
            f"🖼 <b>Фото профиля аккаунта</b> {uname_display}\n\n"
            f"У аккаунта нет фотографий профиля."
        )

    lines = [f"🖼 <b>Фото профиля аккаунта</b> {uname_display}\n"]
    for i, p in enumerate(photos):
        n = i + 1
        pid = p.get("photo_id", "?")
        if i == 0 or p.get("is_main"):
            lines.append(f"{n}. <b>Главная аватарка</b> (ID: <code>{pid}</code>)")
        else:
            lines.append(f"{n}. Фото #{n} (ID: <code>{pid}</code>)")

    lines.append("")
    lines.append(f"Всего: {len(photos)} фото")
    return "\n".join(lines)


async def show_photo_management_screen(
    message: Message,
    *,
    account_id: int,
    reply_markup=None,
    send_new: bool = False,
) -> None:
    from workers.manager import Worker

    kb = reply_markup or get_photo_management_keyboard(account_id)
    temp_worker = None

    try:
        async with db.async_session_maker() as session:
            account = await AccountRepository.get_by_id(session, account_id)

        if not account:
            text = "❌ Аккаунт не найден."
            if send_new:
                await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=get_context_back_keyboard("accounts_list"))
            else:
                await safe_edit_message(message, text, reply_markup=get_context_back_keyboard("accounts_list"))
            return

        session_path = SESSIONS_DIR / f"{account.session_name}.session"
        if not session_path.exists():
            text = "❌ Файл сессии не найден."
            if send_new:
                await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=get_context_back_keyboard("accounts_list"))
            else:
                await safe_edit_message(message, text, reply_markup=get_context_back_keyboard("accounts_list"))
            return

        temp_worker = Worker(account, session_path, account.proxy)
        connected = await temp_worker.connect()

        if not connected or not temp_worker.client:
            text = "❌ Не удалось подключить аккаунт (проверьте сессию и прокси)."
            if send_new:
                await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=get_photo_management_keyboard(account_id))
            else:
                await safe_edit_message(message, text, reply_markup=get_photo_management_keyboard(account_id))
            return

        if not await temp_worker.client.is_user_authorized():
            text = "❌ Сессия не авторизована — нельзя управлять фото профиля."
            if send_new:
                await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=get_photo_management_keyboard(account_id))
            else:
                await safe_edit_message(message, text, reply_markup=get_photo_management_keyboard(account_id))
            return

        photos = await temp_worker.get_profile_photos(limit=100)
        caption = format_profile_photos_caption(account.username, photos)

        if send_new:
            await message.answer(caption, parse_mode=ParseMode.HTML, reply_markup=kb)
        else:
            await safe_edit_message(message, caption, reply_markup=kb)

    except Exception as e:
        log.error(f"Экран управления фото аккаунта {account_id}: {e}")
        err = f"❌ Ошибка: {e}"
        if send_new:
            await message.answer(err, reply_markup=get_photo_management_keyboard(account_id))
        else:
            await safe_edit_message(message, err, reply_markup=get_photo_management_keyboard(account_id))
    finally:
        if temp_worker:
            try:
                await temp_worker.disconnect()
            except Exception:
                pass


@router.callback_query(F.data.startswith("account_manage_photos_"))
async def cb_account_manage_photos(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    account_id = int(callback.data.rsplit("_", 1)[-1])
    await state.clear()
    await show_photo_management_screen(callback.message, account_id=account_id, send_new=False)
    await callback.answer()


@router.callback_query(F.data.startswith("account_photo_add_"))
async def cb_account_photo_add(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    account_id = int(callback.data.rsplit("_", 1)[-1])
    await state.set_state(AccountPhotoManagement.waiting_for_new_photo)
    await state.update_data(account_id=account_id)

    await safe_edit_message(
        callback.message,
        "➕ <b>Новая аватарка</b>\n\n"
        "Отправьте <b>фото</b> или <b>изображение документом</b> (JPG, PNG).\n"
        "Оно будет загружено в профиль аккаунта и станет <b>главной</b> аватаркой.\n\n"
        "Кнопка «Отмена» вернёт к списку фото.",
        reply_markup=get_confirm_photo_delete_keyboard(account_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("account_photo_del_prompt_"))
async def cb_account_photo_del_prompt(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    account_id = int(callback.data.rsplit("_", 1)[-1])
    await state.set_state(AccountPhotoManagement.waiting_for_photo_number_to_delete)
    await state.update_data(account_id=account_id)

    await safe_edit_message(
        callback.message,
        "🗑 <b>Удаление фото по номеру</b>\n\n"
        "Введите номер фото из списка (например: <code>1</code>, <code>2</code>).\n"
        "Номер <code>1</code> — текущая главная аватарка.\n\n"
        "«Отмена» — вернуться к списку без удаления.",
        reply_markup=get_confirm_photo_delete_keyboard(account_id),
    )
    await callback.answer()


def _is_image_document(message: Message) -> bool:
    doc = message.document
    if not doc:
        return False
    mime = (doc.mime_type or "").lower()
    if mime.startswith("image/"):
        return True
    name = (doc.file_name or "").lower()
    return name.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))


async def _download_telegram_image_to_avatars_temp(message: Message, account_id: int) -> Path | None:
    AVATARS_TEMP_DIR.mkdir(parents=True, exist_ok=True)

    if message.photo:
        photo = message.photo[-1]
        file_id = photo.file_id
        unique = photo.file_unique_id
        ext = ".jpg"
    elif message.document and _is_image_document(message):
        doc = message.document
        file_id = doc.file_id
        unique = doc.file_unique_id
        name = doc.file_name or ""
        ext = Path(name).suffix.lower() if Path(name).suffix else ".jpg"
        if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
            ext = ".jpg"
    else:
        return None

    dest = AVATARS_TEMP_DIR / f"up_{account_id}_{unique}{ext}"

    try:
        file_info = await message.bot.get_file(file_id)
        await message.bot.download_file(file_info.file_path, destination=dest)
        return dest
    except Exception as e:
        log.error(f"Скачивание файла для аватарки: {e}")
        return None


@router.message(StateFilter(AccountPhotoManagement.waiting_for_new_photo), F.photo)
@router.message(StateFilter(AccountPhotoManagement.waiting_for_new_photo), F.document)
async def process_account_new_profile_photo(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return

    if message.document and not _is_image_document(message):
        await message.answer("❌ Пришлите изображение (фото или файл-картинку).")
        return

    data = await state.get_data()
    account_id = data.get("account_id")
    if not account_id:
        await message.answer("❌ Сессия устарела. Откройте «Управление аватарками» снова.")
        await state.clear()
        return

    temp_path: Path | None = None
    temp_worker = None
    status_msg = await message.answer("⏳ Загрузка файла и отправка в профиль аккаунта...")

    try:
        temp_path = await _download_telegram_image_to_avatars_temp(message, account_id)
        if not temp_path or not temp_path.exists():
            await safe_edit_message(status_msg, "❌ Не удалось сохранить файл. Попробуйте другое изображение.")
            return

        async with db.async_session_maker() as session:
            account = await AccountRepository.get_by_id(session, account_id)

        if not account:
            await safe_edit_message(status_msg, "❌ Аккаунт не найден.")
            await state.clear()
            return

        from workers.manager import Worker
        from telethon.errors import FloodWaitError, PhotoInvalidDimensionsError

        session_path = SESSIONS_DIR / f"{account.session_name}.session"
        if not session_path.exists():
            await safe_edit_message(status_msg, "❌ Файл сессии не найден.")
            await state.clear()
            return

        temp_worker = Worker(account, session_path, account.proxy)
        connected = await temp_worker.connect()
        if not connected or not temp_worker.client:
            await safe_edit_message(status_msg, "❌ Не удалось подключить аккаунт.")
            await state.clear()
            return

        await safe_edit_message(status_msg, "📤 Загрузка в Telegram (аккаунт через прокси)...")

        try:
            ok = await temp_worker.set_profile_photo(str(temp_path))
        except FloodWaitError as e:
            await safe_edit_message(status_msg, f"⏳ FloodWait: подождите {e.seconds} сек и попробуйте снова.")
            await state.clear()
            return
        except PhotoInvalidDimensionsError:
            await safe_edit_message(status_msg, "❌ Фото не подходит по размерам. Попробуйте другое изображение.")
            return
        except Exception as api_err:
            err_text = str(api_err).lower()
            if "crop" in err_text and "small" in err_text:
                await safe_edit_message(status_msg, "❌ Фото слишком маленькое (нужно хотя бы ~200×200 px).")
            elif "content" in err_text or "save" in err_text:
                await safe_edit_message(status_msg, "❌ Формат не поддерживается. Используйте JPG или PNG.")
            else:
                await safe_edit_message(status_msg, f"❌ Ошибка Telegram API: {api_err}")
            return

        if not ok:
            await safe_edit_message(status_msg, "❌ Не удалось установить фото профиля.")
            return

        async with db.async_session_maker() as session:
            await AccountRepository.set_avatar(session, account_id, None)

        await state.clear()
        await safe_edit_message(status_msg, "✅ <b>Аватарка добавлена</b> (стала главной).", parse_mode=ParseMode.HTML)
        await show_photo_management_screen(message, account_id=account_id, send_new=True)

    except Exception as e:
        log.error(f"Ошибка добавления фото профиля: {e}")
        await safe_edit_message(status_msg, f"❌ Ошибка: {e}")
    finally:
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        if temp_worker:
            try:
                await temp_worker.disconnect()
            except Exception:
                pass


@router.message(StateFilter(AccountPhotoManagement.waiting_for_new_photo), F.text)
async def process_account_photo_waiting_text(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return

    t = (message.text or "").strip().lower()
    if t in ("/start", "отмена", "cancel"):
        data = await state.get_data()
        aid = data.get("account_id")
        await state.clear()
        if aid:
            await show_photo_management_screen(message, account_id=aid, send_new=True)
        else:
            await message.answer("Операция отменена.", reply_markup=get_accounts_keyboard())
        return

    await message.answer(
        "📎 Пришлите <b>фото</b> или <b>картинку файлом</b>, а не текст.\n"
        "Или нажмите «Отмена» под предыдущим сообщением.",
        parse_mode=ParseMode.HTML,
    )


@router.message(StateFilter(AccountPhotoManagement.waiting_for_photo_number_to_delete), F.text)
async def process_account_photo_delete_by_number(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return

    raw = (message.text or "").strip()
    low = raw.lower()

    if low in ("/start", "отмена", "cancel"):
        data = await state.get_data()
        aid = data.get("account_id")
        await state.clear()
        if aid:
            await show_photo_management_screen(message, account_id=aid, send_new=True)
        else:
            await message.answer("Операция отменена.", reply_markup=get_accounts_keyboard())
        return

    data = await state.get_data()
    account_id = data.get("account_id")
    if not account_id:
        await message.answer("❌ Сессия устарела. Откройте управление фото снова.")
        await state.clear()
        return

    if not raw.isdigit():
        await message.answer(
            "❌ Введите целое число — номер строки из списка (например <code>2</code>).",
            parse_mode=ParseMode.HTML,
        )
        return

    n = int(raw)
    if n < 1:
        await message.answer("❌ Номер должен быть не меньше 1.")
        return

    from workers.manager import Worker

    temp_worker = None
    status_msg = await message.answer("⏳ Удаление фото...")

    try:
        async with db.async_session_maker() as session:
            account = await AccountRepository.get_by_id(session, account_id)

        if not account:
            await safe_edit_message(status_msg, "❌ Аккаунт не найден.")
            await state.clear()
            return

        session_path = SESSIONS_DIR / f"{account.session_name}.session"
        if not session_path.exists():
            await safe_edit_message(status_msg, "❌ Файл сессии не найден.")
            await state.clear()
            return

        temp_worker = Worker(account, session_path, account.proxy)
        connected = await temp_worker.connect()
        if not connected or not temp_worker.client:
            await safe_edit_message(status_msg, "❌ Не удалось подключить аккаунт.")
            await state.clear()
            return

        photos = await temp_worker.get_profile_photos(limit=100)
        if not photos:
            await safe_edit_message(status_msg, "ℹ️ У аккаунта уже нет фотографий профиля.")
            await state.clear()
            await show_photo_management_screen(message, account_id=account_id, send_new=True)
            return

        if n > len(photos):
            await safe_edit_message(
                status_msg,
                f"❌ Номера <b>{n}</b> нет в списке (всего {len(photos)}).",
                parse_mode=ParseMode.HTML,
            )
            return

        target = photos[n - 1]
        photo_id = target["photo_id"]
        deleted_main = n == 1

        deleted = await temp_worker.delete_profile_photo(photo_id)
        if not deleted:
            await safe_edit_message(
                status_msg,
                "❌ Не удалось удалить фото (возможно, устарел список — откройте экран снова).",
            )
            return

        if deleted_main:
            async with db.async_session_maker() as session:
                await AccountRepository.set_avatar(session, account_id, None)

        await state.clear()
        await safe_edit_message(status_msg, f"✅ Фото №<b>{n}</b> удалено.", parse_mode=ParseMode.HTML)
        await show_photo_management_screen(message, account_id=account_id, send_new=True)

    except Exception as e:
        log.error(f"Ошибка удаления фото профиля: {e}")
        await safe_edit_message(status_msg, f"❌ Ошибка: {e}")
    finally:
        if temp_worker:
            try:
                await temp_worker.disconnect()
            except Exception:
                pass
