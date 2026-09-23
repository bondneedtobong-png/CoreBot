"""Проверка TData ZIP без импорта (задача 12): ZIP → TDATA_CHECK-группа → отчёт.

Границы: Account НЕ создаётся, proxy к рабочему аккаунту НЕ привязывается,
сессии в production ``data/sessions`` НЕ пишутся (только системный tmp
с гарантированным cleanup). Любой Telegram-connect — только через proxy
из выбранной TDATA_CHECK-группы.
"""

from __future__ import annotations

import shutil
import tempfile
from io import BytesIO
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot.config import OWNER_ID
from bot.handlers.accounts.common import safe_edit_message
from bot.keyboards.main import (
    get_accounts_keyboard,
    get_cancel_with_back_keyboard,
    get_tdata_check_group_keyboard,
)
from database.repositories import ProxyGroupRepository, ProxyRepository
from database.session import session_scope
from services.tdata_check import limits as check_limits
from services.tdata_check.checker import check_proxy_from_orm, run_check_archive
from services.tdata_check.zip_safety import (
    ZipSafetyError,
    find_check_roots,
    safe_extract_zip,
)
from utils.logger import log

router = Router()

_STATUS_EMOJI = {
    "ok": "✅",
    "proxy_required": "⛔",
    "proxy_failed": "🔌",
    "unauthorized": "🔓",
    "session_revoked": "♻️",
    "account_deactivated": "🚫",
    "flood_wait": "⏳",
    "spam_restriction": "⚠️",
    "conversion_failed": "📦",
    "structure_invalid": "📁",
    "archive_invalid": "🗜",
    "unknown": "❓",
}


class TDataCheckFSM(StatesGroup):
    """Проверка TData: ZIP → check-группа → отчёт (без создания Account)."""

    waiting_for_zip = State()
    waiting_for_group = State()


@router.callback_query(F.data == "accounts_check_tdata")
async def cb_tdata_check_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    await safe_edit_message(
        callback.message,
        "🔍 <b>Проверка TData (без импорта)</b>\n\n"
        "Отправьте ZIP-архив с папками tdata.\n\n"
        "Каждая папка будет авторизована через прокси из выбранной "
        "TDATA_CHECK-группы. Аккаунты НЕ создаются, сессии НЕ сохраняются.\n\n"
        "❌ Отмена: /start",
        reply_markup=get_cancel_with_back_keyboard("cancel_accounts", "menu_accounts"),
    )
    await state.set_state(TDataCheckFSM.waiting_for_zip)
    await callback.answer()


@router.message(TDataCheckFSM.waiting_for_zip, F.document)
async def process_check_zip(message: Message, state: FSMContext, bot: Bot):
    if message.from_user.id != OWNER_ID:
        return
    document = message.document
    if not document.file_name.lower().endswith(".zip"):
        await message.answer("❌ Пожалуйста, отправьте ZIP-архив")
        await state.clear()
        return
    if document.file_size and document.file_size > check_limits.MAX_ARCHIVE_BYTES:
        await message.answer(
            f"❌ Архив больше лимита ({check_limits.MAX_ARCHIVE_BYTES // 1024 // 1024} МБ)."
        )
        await state.clear()
        return

    buf = BytesIO()
    await bot.download(document, destination=buf)
    data = buf.getvalue()

    probe_dir = Path(tempfile.mkdtemp(prefix="tdata-check-probe-"))
    try:
        safe_extract_zip(data, probe_dir, max_bytes=check_limits.MAX_ARCHIVE_BYTES)
        roots = find_check_roots(probe_dir)
    except ZipSafetyError as exc:
        shutil.rmtree(probe_dir, ignore_errors=True)
        await message.answer(
            f"❌ Архив отклонён: <code>{exc.code}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=get_accounts_keyboard(),
        )
        await state.clear()
        return
    shutil.rmtree(probe_dir, ignore_errors=True)

    if not roots:
        await message.answer(
            "❌ В архиве нет валидных папок tdata.",
            reply_markup=get_accounts_keyboard(),
        )
        await state.clear()
        return

    # Байты — в tmp-файл (не в data/*), путь — в state.
    staging = Path(tempfile.mkdtemp(prefix="tdata-check-bot-"))
    (staging / "upload.zip").write_bytes(data)
    await state.update_data(
        staged_zip=str(staging / "upload.zip"), staged_dir=str(staging)
    )

    async with session_scope() as session:
        check_groups = await ProxyGroupRepository.get_by_purpose(session, "TDATA_CHECK")
        usage = await ProxyGroupRepository.list_with_usage(session)
    check_ids = {g.id for g in check_groups}
    check_usage = [row for row in usage if row[0].id in check_ids]

    await message.answer(
        f"📂 Найдено TData папок: <b>{len(roots)}</b>\n\n"
        "🔍 <b>Выберите TDATA_CHECK-группу прокси:</b>\n"
        "Проверка каждой папки пойдёт только через прокси из неё.",
        reply_markup=get_tdata_check_group_keyboard(check_usage),
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(TDataCheckFSM.waiting_for_group)


@router.callback_query(F.data.startswith("tdata_check_group_"))
async def process_check_group(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    data = await state.get_data()
    staged_zip = data.get("staged_zip")
    staged_dir = data.get("staged_dir")
    if not staged_zip or not Path(staged_zip).exists():
        await callback.answer("Файл потерян, загрузите ZIP заново", show_alert=True)
        await state.clear()
        return
    group_id = int(callback.data.rsplit("_", 1)[-1])
    raw = Path(staged_zip).read_bytes()

    await safe_edit_message(callback.message, "🔍 Проверка через TDATA_CHECK пул…")
    await callback.answer()

    try:
        async with session_scope() as session:
            group = await ProxyGroupRepository.get_by_id(session, group_id)
            if (
                group is None
                or (getattr(group, "purpose", "") or "").upper() != "TDATA_CHECK"
            ):
                await callback.message.answer(
                    "❌ Нужна группа с назначением TDATA_CHECK.",
                    reply_markup=get_accounts_keyboard(),
                )
                return
            proxies = await ProxyRepository.get_active_in_group(session, group_id)
        if not proxies:
            await callback.message.answer(
                "⛔ В группе нет рабочих прокси — проверка невозможна "
                "(прямой коннект запрещён).",
                reply_markup=get_accounts_keyboard(),
            )
            return
        run = await run_check_archive(
            raw, proxies=[check_proxy_from_orm(p) for p in proxies]
        )
        await callback.message.answer(
            _render_report(run),
            reply_markup=get_accounts_keyboard(),
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        log.error(f"TData check failed: {exc}")
        await callback.message.answer(
            "❌ Ошибка проверки, попробуйте позже.",
            reply_markup=get_accounts_keyboard(),
        )
    finally:
        shutil.rmtree(staged_dir, ignore_errors=True) if staged_dir else None
        await state.clear()


@router.callback_query(F.data == "tdata_check_no_groups")
async def cb_check_no_groups(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await callback.answer(
        "Сначала создайте TDATA_CHECK пул: Прокси → Массово в группу → назначение TData-check",
        show_alert=True,
    )


def _render_report(run) -> str:
    """Краткий отчёт run: без credentials, паролей и session-путей."""
    lines = [
        "🔍 <b>Проверка TData завершена</b>",
        "",
        f"Всего: <b>{run.total}</b> · OK: <b>{run.ok_count}</b> · "
        f"Проблемы: <b>{run.failed_count}</b>",
    ]
    if run.truncated:
        lines.append(
            f"⚠️ Обработаны первые {run.total} (лимит {check_limits.MAX_FOLDERS})"
        )
    for item in run.items[: check_limits.MAX_FOLDERS]:
        emoji = _STATUS_EMOJI.get(item.status, "❓")
        who = item.username and f"@{item.username}" or (item.phone or item.relpath)
        name = f"{item.first_name or ''} {item.last_name or ''}".strip()
        profile = f" {name}" if name else ""
        country = f" [{item.country}]" if item.country else ""
        proxy = f" via {item.proxy_label}" if item.proxy_label else ""
        lines.append(
            f"\n{emoji} <code>{item.item_id}</code> {who}{profile}{country}{proxy}"
        )
        lines.append(f"   статус: <code>{item.status}</code>")
        if item.error_detail:
            lines.append(f"   {item.error_detail[:160]}")
        if item.retry_after:
            lines.append(f"   retry after: {item.retry_after}s")
    return "\n".join(lines)


__all__ = ["router"]
