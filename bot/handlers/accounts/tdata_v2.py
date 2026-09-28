"""Import one or many TData folders through the same bot flow."""
from __future__ import annotations

import shutil
from html import escape
from pathlib import Path
from uuid import uuid4

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.config import is_authorized_user
from bot.config import SESSIONS_DIR, TDATA_TEMP_DIR
from bot.handlers.accounts.common import safe_edit_message
from bot.handlers.accounts.states import AccountUpload
from bot.keyboards.main import get_accounts_keyboard, get_cancel_with_back_keyboard, get_proxy_group_select_keyboard
from database.models import AccountStatus, ProxyGroupPurpose, ProxyType
from database.repositories import AccountRepository, ProxyGroupRepository
from database.session import session_scope
from utils.logger import log
from utils.phone_geo import infer_country_from_phone
from utils.safe_zip import extract_zip_safely
from workers.session_converter import convert_tdata_to_session, find_tdata_roots
from workers.session_lease import proxy_pool_import_lease

router = Router()


def _labels(raw: str, count: int) -> list[str]:
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(lines) == count:
        return [line[:64] for line in lines]
    if len(lines) != 1:
        raise ValueError(f"Введите одно общее название или ровно {count} названий, по одному в строке")
    base = lines[0]
    if count == 1:
        return [base[:64]]
    return [f"{base[:64 - len(f' ·{index}')]} ·{index}" for index in range(1, count + 1)]


def _discard_extract(path: Path) -> None:
    if path.is_dir() and path.parent.resolve() == TDATA_TEMP_DIR.resolve() and path.name.startswith("extracted_"):
        shutil.rmtree(path)


@router.callback_query(F.data.in_({"accounts_upload", "accounts_upload_bulk_geo", "accounts_upload_bulk_geo_confirm"}))
async def start_import(callback: CallbackQuery, state: FSMContext):
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    old = await state.get_data()
    if old.get("extract_dir"):
        _discard_extract(Path(old["extract_dir"]))
    await state.clear()
    await state.set_state(AccountUpload.waiting_for_file)
    await safe_edit_message(
        callback.message,
        "📥 <b>Импорт аккаунтов</b>\n\n"
        "Пришлите ZIP с одной или несколькими папками, в названии которых есть "
        "<code>tdata</code>. После загрузки выберите группу SOCKS5-прокси.",
        reply_markup=get_cancel_with_back_keyboard("cancel_accounts", "menu_accounts"),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.message(AccountUpload.waiting_for_file, F.document)
async def receive_zip(message: Message, state: FSMContext):
    if not is_authorized_user(message.from_user.id):
        return
    doc = message.document
    if not (doc.file_name or "").lower().endswith(".zip"):
        await message.answer("Пришлите ZIP-архив.", reply_markup=get_cancel_with_back_keyboard("cancel_accounts", "menu_accounts"))
        return
    if doc.file_size and doc.file_size > 100_000_000:
        await message.answer("ZIP больше 100 МБ. Разделите архив.")
        return
    status = await message.answer("⏳ Проверяю архив…")
    TDATA_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    upload_id = uuid4().hex
    archive = TDATA_TEMP_DIR / f"{upload_id}.zip"
    extract_dir = TDATA_TEMP_DIR / f"extracted_{upload_id}"
    try:
        remote = await message.bot.get_file(doc.file_id)
        await message.bot.download_file(remote.file_path, archive)
        extract_zip_safely(archive, extract_dir)
        roots = find_tdata_roots(extract_dir)
        if not roots:
            raise ValueError("Папки tdata не найдены")
        async with session_scope() as session:
            groups = [
                item for item in await ProxyGroupRepository.list_with_usage(session)
                if item[0].purpose == ProxyGroupPurpose.ACCOUNT_RUNTIME.value
            ]
        await state.update_data(extract_dir=str(extract_dir), tdata_count=len(roots))
        await state.set_state(AccountUpload.waiting_for_proxy)
        await safe_edit_message(
            status,
            f"📂 Найдено аккаунтов: <b>{len(roots)}</b>\n\n"
            "Выберите группу рабочих SOCKS5-прокси. Каждому новому аккаунту будет назначен свободный прокси.",
            reply_markup=get_proxy_group_select_keyboard(
                groups, none_callback=None, cancel_callback="accounts_upload", back_callback="menu_accounts"
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        log.warning("TData ZIP rejected: {}", exc)
        _discard_extract(extract_dir)
        await safe_edit_message(status, f"❌ {escape(str(exc))}", reply_markup=get_cancel_with_back_keyboard("cancel_accounts", "menu_accounts"))
    finally:
        archive.unlink(missing_ok=True)


@router.callback_query(F.data.startswith("proxy_group_select_"), AccountUpload.waiting_for_proxy)
async def select_proxy_group(callback: CallbackQuery, state: FSMContext):
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    try:
        group_id = int(callback.data.rsplit("_", 1)[-1])
    except ValueError:
        await callback.answer("Выберите группу SOCKS5-прокси", show_alert=True)
        return
    data = await state.get_data()
    extract_dir = Path(data.get("extract_dir", ""))
    if not extract_dir.is_dir():
        await state.clear()
        await callback.answer("Загрузите ZIP заново", show_alert=True)
        return
    async with session_scope() as session:
        group = await ProxyGroupRepository.get_by_id(session, group_id)
    if not group or group.purpose != ProxyGroupPurpose.ACCOUNT_RUNTIME.value:
        await callback.answer("Нужна рабочая группа SOCKS5", show_alert=True)
        return
    await state.set_state(AccountUpload.processing)
    await callback.answer()
    await safe_edit_message(callback.message, "⏳ Подключаю и проверяю аккаунты…")
    imported: list[dict] = []
    errors: list[str] = []
    try:
        roots = find_tdata_roots(extract_dir)
        for index, root in enumerate(roots, 1):
            lease = None
            try:
                lease = proxy_pool_import_lease(SESSIONS_DIR, group_id).acquire()
                async with session_scope() as session:
                    proxy = await ProxyGroupRepository.acquire_next_free_proxy(session, group_id)
                    if not proxy or proxy.proxy_type != ProxyType.SOCKS5 or not proxy.is_active or not proxy.is_working:
                        raise RuntimeError("Нет свободного рабочего SOCKS5-прокси")
                    proxy_id = proxy.id
                    session.expunge(proxy)
                result = await convert_tdata_to_session(root, SESSIONS_DIR, proxy=proxy)
                if not result or not result.get("success"):
                    raise RuntimeError((result or {}).get("error") or "Проверка сессии не прошла")
                phone = result.get("phone") or "unknown"
                session_name = result["session_name"]
                async with session_scope() as session:
                    existing = await AccountRepository.get_by_session_name(session, session_name)
                    if existing is None:
                        existing = await AccountRepository.get_by_phone(session, phone)
                    if existing is None:
                        account = await AccountRepository.create(
                            session, phone=phone, session_name=session_name,
                            username=result.get("username"), first_name=result.get("first_name"),
                            last_name=result.get("last_name"), proxy_id=proxy_id,
                            status=AccountStatus.ACTIVE, import_source="tdata_bot_v2",
                        )
                        is_new = True
                    else:
                        account = existing
                        is_new = False
                imported.append({
                    "id": account.id, "phone": phone, "name": " ".join(
                        filter(None, (result.get("first_name"), result.get("last_name")))
                    ) or "—", "region": infer_country_from_phone(phone), "new": is_new,
                })
            except Exception as exc:
                log.error("TData import {}/{} failed: {}", index, len(roots), exc)
                errors.append(f"#{index}: {type(exc).__name__}")
            finally:
                if lease is not None:
                    lease.release()
    finally:
        _discard_extract(extract_dir)

    new_accounts = [item for item in imported if item["new"]]
    lines = [f"✅ Обработано: {len(imported)} · новых: {len(new_accounts)} · ошибок: {len(errors)}", ""]
    for item in imported[:20]:
        suffix = "" if item["new"] else " (уже был в списке)"
        lines.append(
            f"• {escape(item['phone'])} · {escape(item['region'])} · {escape(item['name'])}{suffix}"
        )
    if len(imported) > 20:
        lines.append(f"…ещё {len(imported) - 20}")
    if errors:
        lines.extend(("", "Ошибки: " + ", ".join(errors[:10])))
    if new_accounts:
        lines.extend(("", "🏷 Пришлите одно общее название или по одному названию для каждого нового аккаунта, каждое с новой строки."))
        await state.update_data(imported_account_ids=[item["id"] for item in new_accounts])
        await state.set_state(AccountUpload.waiting_for_list_label)
        keyboard = get_cancel_with_back_keyboard("cancel_accounts", "accounts_manage")
    else:
        await state.clear()
        keyboard = get_accounts_keyboard()
    await safe_edit_message(callback.message, "\n".join(lines), reply_markup=keyboard, parse_mode=ParseMode.HTML)


@router.message(AccountUpload.waiting_for_list_label)
async def receive_labels(message: Message, state: FSMContext):
    if not is_authorized_user(message.from_user.id):
        return
    data = await state.get_data()
    account_ids = data.get("imported_account_ids") or []
    if not account_ids:
        await state.clear()
        await message.answer("Список новых аккаунтов потерян. Откройте список аккаунтов.", reply_markup=get_accounts_keyboard())
        return
    try:
        labels = _labels(message.text or "", len(account_ids))
    except ValueError as exc:
        await message.answer(str(exc), reply_markup=get_cancel_with_back_keyboard("cancel_accounts", "accounts_manage"))
        return
    async with session_scope() as session:
        for account_id, label in zip(account_ids, labels):
            await AccountRepository.update_list_label(session, account_id, label)
    await state.clear()
    await message.answer(f"✅ Названия сохранены для {len(account_ids)} аккаунтов.", reply_markup=get_accounts_keyboard())
