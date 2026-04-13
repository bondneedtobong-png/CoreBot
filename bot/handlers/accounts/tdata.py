"""Загрузка ZIP с Tdata: прокси → название в списке → конвертация в .session."""
import os
import shutil
import zipfile
from pathlib import Path

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup

from bot.config import OWNER_ID, SESSIONS_DIR, TDATA_TEMP_DIR
from bot.handlers.accounts.common import safe_edit_message
from bot.handlers.accounts.states import AccountUpload
from bot.keyboards.main import (
    get_accounts_keyboard,
    get_cancel_with_back_keyboard,
    get_proxy_group_select_keyboard,
)
from database.session import session_scope
from database.models import AccountStatus
from database.repositories import AccountRepository, ProxyGroupRepository
from utils.logger import log
from workers.manager import verify_session_via_proxy
from workers.session_converter import convert_tdata_to_session, find_tdata_roots

router = Router()


def _upload_tdata_prompt_text(*, bulk_geo: bool = False) -> str:
    if bulk_geo:
        return (
            "📦 <b>Массовый залив аккаунтов (Tdata ZIP)</b>\n\n"
            "Отправьте ZIP-архив с несколькими папками tdata.\n\n"
            "⚠️ Рекомендуется заливать одновременно аккаунты одного GEO,\n"
            "чтобы снизить риск ограничений.\n\n"
            "Далее вы выберете группу прокси, и бот по очереди авторизует\n"
            "каждый аккаунт через прокси из выбранной группы.\n\n"
            "❌ Отмена: /start"
        )
    return (
        "📥 <b>Загрузка аккаунтов</b>\n\n"
        "Отправьте ZIP-архив с папкой tdata.\n\n"
        "📁 Архив должен содержать папку с файлами:\n"
        "• data/\n"
        "• settings\n"
        "• и другие файлы Tdata\n\n"
        "❌ Отмена: /start"
    )


def _labels_from_user_input(raw: str, n_accounts: int) -> list[str]:
    """Одна подпись или несколько с суффиксами ·1, ·2 … (лимит 64 символа)."""
    base = (raw or "").strip()
    if not base:
        base = "Аккаунт"
    if n_accounts <= 1:
        return [base[:64]]
    out: list[str] = []
    for i in range(1, n_accounts + 1):
        suffix = f" ·{i}"
        room = 64 - len(suffix)
        b = base[: max(1, room)]
        out.append((b + suffix)[:64])
    return out


@router.callback_query(F.data == "accounts_upload")
async def cb_accounts_upload(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await state.clear()
    await state.update_data(upload_mode="single")
    await safe_edit_message(
        callback.message,
        _upload_tdata_prompt_text(bulk_geo=False),
        reply_markup=get_cancel_with_back_keyboard("cancel_accounts", "menu_accounts"),
    )
    await state.set_state(AccountUpload.waiting_for_file)
    await callback.answer()


@router.callback_query(F.data == "accounts_upload_bulk_geo")
async def cb_accounts_upload_bulk_geo(callback: CallbackQuery, state: FSMContext):
    """Отдельный сценарий массового залива с предупреждением по GEO."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await state.clear()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить и загрузить ZIP",
                    callback_data="accounts_upload_bulk_geo_confirm",
                )
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_accounts")],
        ]
    )
    await safe_edit_message(
        callback.message,
        "⚠️ <b>Массовый залив Tdata</b>\n\n"
        "Рекомендуется загружать одновременно аккаунты одного GEO.\n"
        "Для массового сценария выбор группы прокси обязателен.\n\n"
        "После загрузки ZIP бот по очереди конвертирует и авторизует аккаунты "
        "через прокси из выбранной группы.",
        reply_markup=kb,
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data == "accounts_upload_bulk_geo_confirm")
async def cb_accounts_upload_bulk_geo_confirm(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await state.clear()
    await state.update_data(upload_mode="bulk_geo")
    await safe_edit_message(
        callback.message,
        _upload_tdata_prompt_text(bulk_geo=True),
        reply_markup=get_cancel_with_back_keyboard("cancel_accounts", "menu_accounts"),
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(AccountUpload.waiting_for_file)
    await callback.answer()


@router.message(AccountUpload.waiting_for_file, F.document)
async def process_tdata_zip(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return

    document = message.document
    if not document.file_name.lower().endswith(".zip"):
        await message.answer("❌ Пожалуйста, отправьте ZIP-архив")
        await state.clear()
        return

    status_msg = await message.answer("⏳ Загрузка файла...")

    try:
        file = await message.bot.get_file(document.file_id)
        file_path = TDATA_TEMP_DIR / document.file_name
        TDATA_TEMP_DIR.mkdir(parents=True, exist_ok=True)
        await message.bot.download_file(file.file_path, file_path)
        await safe_edit_message(status_msg, "📦 Файл загружен. Распаковка...")

        extract_dir = TDATA_TEMP_DIR / f"extracted_{document.file_name[:-4]}"
        extract_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(file_path, "r") as zip_ref:
            zip_ref.extractall(extract_dir)

        await safe_edit_message(status_msg, "🔍 Поиск папок Tdata...")
        tdata_roots = find_tdata_roots(extract_dir)

        if not tdata_roots:
            await safe_edit_message(
                status_msg,
                "❌ Не найдено папок с Tdata.\n\n"
                "📁 Ожидаемая структура:\n"
                "• tdata/\n"
                "  • <hex_hash>/ (папка с длинным именем)\n"
                "  • settings\n"
                "  • key_datas\n\n"
                "Убедитесь, что архив содержит папку tdata с файлами settings и key_datas.",
            )
            await state.clear()
            return

        upload_data = await state.get_data()
        upload_mode = upload_data.get("upload_mode", "single")
        bulk_geo = upload_mode == "bulk_geo"

        await state.update_data(extract_dir=str(extract_dir))
        await state.update_data(tdata_count=len(tdata_roots))

        async with session_scope() as session:
                groups_usage = await ProxyGroupRepository.list_with_usage(session)

        await safe_edit_message(
            status_msg,
            f"📂 Найдено Tdata аккаунтов: {len(tdata_roots)}\n\n"
            "🌐 <b>Выберите группу прокси для автоназначения:</b>\n"
            + (
                "Бот выдаст следующий свободный прокси из выбранной группы."
                if not bulk_geo
                else (
                    "В массовом режиме прокси-группа обязательна. "
                    "Бот по очереди авторизует каждый аккаунт через назначенный прокси."
                )
            ),
            reply_markup=get_proxy_group_select_keyboard(
                groups_usage,
                none_callback=(
                    "proxy_group_select_bulk_none_blocked"
                    if bulk_geo
                    else "proxy_group_select_none"
                ),
                cancel_callback="accounts_upload",
            ),
        )
        await state.set_state(AccountUpload.waiting_for_proxy)

    except Exception as e:
        log.error(f"Ошибка обработки Tdata: {e}")
        await safe_edit_message(status_msg, f"❌ Ошибка: {e}")
        await state.clear()


@router.callback_query(F.data.startswith("proxy_group_select_"))
async def process_proxy_select(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    group_id_str = callback.data.split("_")[-1]
    data = await state.get_data()
    extract_dir = data.get("extract_dir")
    tdata_count = data.get("tdata_count", 1)
    upload_mode = data.get("upload_mode", "single")

    if not extract_dir:
        await callback.message.answer("❌ Ошибка: данные потеряны. Загрузите файл заново.")
        await state.clear()
        await callback.answer()
        return

    group_id = None if group_id_str == "none" else int(group_id_str)
    if upload_mode == "bulk_geo" and group_id is None:
        await callback.answer(
            "В массовом режиме нужно выбрать группу прокси",
            show_alert=True,
        )
        return
    group = None
    if group_id:
        async with session_scope() as session:
            group = await ProxyGroupRepository.get_by_id(session, group_id)
    proxy_info = f"Группа {group.name}" if group else "Без прокси"

    await state.update_data(proxy_group_id=group_id)
    await state.set_state(AccountUpload.waiting_for_list_label)

    await safe_edit_message(
        callback.message,
        f"📂 В архиве: <b>{tdata_count}</b> Tdata\n"
        f"🌐 Прокси: <b>{proxy_info}</b>\n\n"
        "🏷 <b>Введите название для списка аккаунтов</b> — так аккаунт будет "
        "отображаться в боте (не меняет профиль Telegram).\n\n"
        "До 64 символов. Если в архиве <b>несколько</b> Tdata, к названию добавятся "
        "суффиксы <code>·1</code>, <code>·2</code>, …\n\n"
        "❌ Отмена: /start",
        reply_markup=get_cancel_with_back_keyboard("accounts_upload", "menu_accounts"),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data == "proxy_group_select_bulk_none_blocked")
async def cb_proxy_group_select_bulk_none_blocked(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await callback.answer(
        "Для массового залива нельзя выбрать «без прокси». Выберите группу прокси.",
        show_alert=True,
    )


async def _run_tdata_conversion(
    message: Message,
    state: FSMContext,
    *,
    extract_dir: str,
    group_id: int | None,
    group,
    list_labels: list[str],
) -> None:
    await state.set_state(AccountUpload.processing)
    converted = 0
    errors = 0

    try:
        tdata_roots = find_tdata_roots(Path(extract_dir))
        log.info(f"📂 Найдено {len(tdata_roots)} Tdata корней для конвертации")
        log.info(
            f"🔄 Конвертация с прокси: "
            f"{'Группа ' + group.name if group else 'без прокси'}"
        )

        for idx, tdata_path in enumerate(tdata_roots, 1):
            acc_label = list_labels[idx - 1] if idx <= len(list_labels) else list_labels[-1]
            try:
                log.info(f"🔄 Конвертация аккаунта {idx}/{len(tdata_roots)}...")
                result = await convert_tdata_to_session(tdata_path=tdata_path, sessions_dir=SESSIONS_DIR)

                if result and result.get("success"):
                    session_name = result.get("session_name")
                    phone = result.get("phone", "unknown")
                    username = result.get("username")
                    first_name = result.get("first_name", "")
                    last_name = result.get("last_name", "")

                    async with session_scope() as session:
                        assigned_proxy_id = None
                        picked_proxy = None
                        if group_id is not None:
                            picked_proxy = await ProxyGroupRepository.acquire_next_free_proxy(
                                session, group_id
                            )
                            if not picked_proxy:
                                raise RuntimeError(
                                    f"В группе {group.name if group else group_id} закончились свободные прокси"
                                )
                            assigned_proxy_id = picked_proxy.id

                        session_file = SESSIONS_DIR / f"{session_name}.session"
                        if picked_proxy is not None:
                            ok_proxy, proxy_err = await verify_session_via_proxy(
                                session_file, picked_proxy
                            )
                            if ok_proxy:
                                log.info(
                                    f"✅ Прокси для нового аккаунта: {picked_proxy.name} "
                                    f"({picked_proxy.host}:{picked_proxy.port}) — "
                                    f"сессия {session_name} авторизована через прокси"
                                )
                            else:
                                log.warning(
                                    f"⚠️ Прокси назначен в БД ({picked_proxy.name}), "
                                    f"но проверка через прокси не прошла: {proxy_err}"
                                )
                        else:
                            log.info(
                                f"ℹ️ Новый аккаунт без прокси — сессия {session_name}, "
                                f"статус active после успешной конвертации"
                            )

                        existing = await AccountRepository.get_by_session_name(session, session_name)
                        if not existing:
                            await AccountRepository.create(
                                session,
                                phone=phone,
                                session_name=session_name,
                                username=username,
                                first_name=first_name,
                                last_name=last_name,
                                proxy_id=assigned_proxy_id,
                                status=AccountStatus.ACTIVE,
                                list_label=acc_label,
                            )

                    converted += 1
                    log.info(
                        f"Аккаунт {idx}/{len(tdata_roots)} конвертирован: {username or phone} "
                        f"(в списке: {acc_label})"
                    )
                else:
                    errors += 1
                    log.warning(
                        f"Аккаунт {idx}/{len(tdata_roots)} не сконвертирован: {result.get('error')}"
                    )

            except Exception as e:
                errors += 1
                log.error(f"Ошибка конвертации аккаунта {idx}: {e}")

        try:
            shutil.rmtree(Path(extract_dir))
            fp = TDATA_TEMP_DIR / f"extracted_{os.path.basename(extract_dir)[10:]}.zip"
            if fp.exists():
                os.remove(fp)
        except Exception as e:
            log.warning(f"Не удалось очистить временные файлы: {e}")

        await message.answer(
            f"✅ <b>Готово!</b>\n\n"
            f"📊 Результаты:\n"
            f"• Сконвертировано: {converted}\n"
            f"• Ошибок: {errors}\n\n"
            f"Сессии сохранены в {SESSIONS_DIR}",
            reply_markup=get_accounts_keyboard(),
            parse_mode=ParseMode.HTML,
        )

    except Exception as e:
        log.error(f"Ошибка конвертации: {e}")
        await message.answer(f"❌ Ошибка: {e}", reply_markup=get_accounts_keyboard())

    finally:
        await state.clear()


@router.message(AccountUpload.waiting_for_list_label)
async def process_tdata_list_label(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return

    text = (message.text or "").strip()
    if text.lower() in ("/start", "отмена", "cancel"):
        await state.clear()
        await message.answer("❌ Загрузка отменена.")
        return
    if not text:
        await message.answer("Введите название для отображения в списке (текстом).")
        return

    data = await state.get_data()
    extract_dir = data.get("extract_dir")
    group_id = data.get("proxy_group_id")
    if extract_dir is None:
        await state.clear()
        await message.answer("❌ Данные сессии потеряны. Загрузите ZIP заново.")
        return

    group = None
    if group_id is not None:
        async with session_scope() as session:
            group = await ProxyGroupRepository.get_by_id(session, group_id)

    tdata_roots = find_tdata_roots(Path(extract_dir))
    if not tdata_roots:
        await state.clear()
        await message.answer("❌ Папки Tdata не найдены. Загрузите архив заново.")
        return

    labels = _labels_from_user_input(text, len(tdata_roots))
    status_msg = await message.answer("⏳ Конвертация и проверка прокси…")
    await _run_tdata_conversion(
        message,
        state,
        extract_dir=extract_dir,
        group_id=group_id,
        group=group,
        list_labels=labels,
    )
    try:
        await status_msg.delete()
    except Exception:
        pass
