"""
Инструмент: TXT со ссылками t.me → отсортированный список @username + опционально вырезание «подозрительных».
"""
from __future__ import annotations

import uuid
from pathlib import Path

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup

from bot.config import FILES_DIR, OWNER_ID
from bot.keyboards.main import get_cancel_with_back_keyboard, get_clients_keyboard
from utils.logger import log
from utils.username_list_tool import (
    extract_usernames,
    filter_suspicious,
    format_at_lines,
    sort_grouped,
)

router = Router()

_TOOL_DIR = FILES_DIR / "username_tool"
_SESSION_PREFIX = "username_tool_"


class UsernameListToolFSM(StatesGroup):
    waiting_file = State()


def _session_paths(session_id: str) -> tuple[Path, Path, Path]:
    base = _TOOL_DIR / session_id
    return (
        base.with_suffix(".sorted.txt"),
        base.with_suffix(".suspicious.txt"),
        base.with_suffix(".meta.txt"),
    )


def _review_keyboard(session_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Убрать подозрительные из списка",
                    callback_data=f"{_SESSION_PREFIX}yes_{session_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📋 Оставить полный отсортированный список",
                    callback_data=f"{_SESSION_PREFIX}keep_{session_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="❌ Закрыть",
                    callback_data=f"{_SESSION_PREFIX}close_{session_id}",
                ),
            ],
        ]
    )


@router.callback_query(F.data == "clients_username_tool")
async def cb_username_tool_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await state.clear()
    await state.set_state(UsernameListToolFSM.waiting_file)
    await callback.message.edit_text(
        "🧾 <b>Обработка списка t.me</b>\n\n"
        "Пришлите <b>.txt</b> файл (документом): ссылки вида "
        "<code>https://t.me/username</code>, можно с <code>@username</code> "
        "и строками только с ником.\n\n"
        "Бот вернёт:\n"
        "• <b>Отсортированный</b> список <code>@username</code> — похожие ники рядом "
        "(один «корень» до цифр, например все <code>hu…</code> вместе).\n"
        "• Файл <b>кандидатов на удаление</b> — склеенные <b>пачки</b> "
        "(общий префикс 4–6 символов + длинные цифровые хвосты, либо паттерн "
        "«2 буквы + цифры»). Одиночные «креативные» ники не режутся.\n"
        "• Кнопки: убрать кандидатов из итогового списка или оставить как есть.\n\n"
        "❌ Отмена: кнопка ниже или /start",
        reply_markup=get_cancel_with_back_keyboard(
            "cancel_username_tool", "menu_database"
        ),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data == "cancel_username_tool")
async def cb_cancel_username_tool(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(
        "❌ Отменено.",
        reply_markup=get_clients_keyboard(),
    )
    await callback.answer()


@router.message(UsernameListToolFSM.waiting_file, F.document)
async def process_username_tool_file(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return

    document = message.document
    if not document or not document.file_name.lower().endswith(".txt"):
        await message.answer("❌ Нужен файл <code>.txt</code>", parse_mode=ParseMode.HTML)
        return

    status = await message.answer("⏳ Читаю файл…")

    try:
        await _sweep_old_tool_files()
        file = await message.bot.get_file(document.file_id)
        _TOOL_DIR.mkdir(parents=True, exist_ok=True)
        local = _TOOL_DIR / f"in_{message.from_user.id}_{document.file_id}.txt"
        await message.bot.download_file(file.file_path, local)

        content = local.read_text(encoding="utf-8", errors="replace")
        try:
            local.unlink(missing_ok=True)
        except OSError:
            pass

        raw = extract_usernames(content)
        if not raw:
            await status.edit_text(
                "❌ Не найдено ни одного валидного username. "
                "Проверьте, что есть ссылки <code>t.me/…</code> или <code>@…</code>.",
                parse_mode=ParseMode.HTML,
            )
            await state.clear()
            return

        sorted_list = sort_grouped(raw)
        susp = filter_suspicious(sorted_list)
        susp_sorted = sort_grouped(susp)

        session_id = uuid.uuid4().hex[:12]
        sorted_path, susp_path, meta_path = _session_paths(session_id)

        sorted_path.write_text(format_at_lines(sorted_list), encoding="utf-8")
        susp_path.write_text(format_at_lines(susp_sorted), encoding="utf-8")
        meta_path.write_text(
            f"total={len(sorted_list)}\nsuspicious={len(susp)}\n",
            encoding="utf-8",
        )

        await state.update_data(username_tool_session=session_id)
        await state.clear()

        await status.edit_text(
            f"✅ Разобрано: <b>{len(sorted_list)}</b> уникальных username.\n"
            f"🔍 Подозрительных по эвристике: <b>{len(susp)}</b> "
            f"(смотри отдельный файл — решение за вами).\n\n"
            f"Ниже — отсортированный список и кандидаты. Выберите действие кнопками.",
            parse_mode=ParseMode.HTML,
        )

        doc_sorted = BufferedInputFile(
            sorted_path.read_bytes(),
            filename="sorted_at_usernames.txt",
        )
        await message.answer_document(
            doc_sorted,
            caption="📋 Отсортированный список (@username, похожие рядом)",
        )

        if susp_sorted:
            doc_susp = BufferedInputFile(
                susp_path.read_bytes(),
                filename="suspicious_candidates.txt",
            )
            await message.answer_document(
                doc_susp,
                caption="⚠️ Кандидаты: пачки по префиксу/паттерну (проверьте перед удалением)",
            )
        else:
            await message.answer("ℹ️ Явных «подозрительных» по правилам не найдено.")

        await message.answer(
            "Дальше:",
            reply_markup=_review_keyboard(session_id),
        )

    except Exception as e:
        log.exception(f"username_tool: {e}")
        await status.edit_text(f"❌ Ошибка: {e}")
        await state.clear()


@router.callback_query(F.data.startswith(f"{_SESSION_PREFIX}yes_"))
async def cb_username_tool_yes(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔", show_alert=True)
        return
    session_id = callback.data.split("_")[-1]
    sorted_path, susp_path, _ = _session_paths(session_id)
    if not sorted_path.is_file():
        await callback.answer("Сессия устарела — запустите заново", show_alert=True)
        return

    sorted_list = [
        ln.strip().lstrip("@").lower()
        for ln in sorted_path.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    susp_set = set()
    if susp_path.is_file():
        susp_set = {
            ln.strip().lstrip("@").lower()
            for ln in susp_path.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        }

    cleaned = [u for u in sorted_list if u not in susp_set]
    body = format_at_lines(cleaned)
    doc = BufferedInputFile(body.encode("utf-8"), filename="cleaned_at_usernames.txt")
    await callback.message.answer_document(
        doc,
        caption=f"✅ Итог без «подозрительных»: <b>{len(cleaned)}</b> из {len(sorted_list)}",
        parse_mode=ParseMode.HTML,
    )
    _cleanup_session(session_id)
    await callback.message.answer("Готово.", reply_markup=get_clients_keyboard())
    await callback.answer("Готово")


@router.callback_query(F.data.startswith(f"{_SESSION_PREFIX}keep_"))
async def cb_username_tool_keep(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔", show_alert=True)
        return
    session_id = callback.data.split("_")[-1]
    _cleanup_session(session_id)
    await callback.message.answer(
        "Ок — ориентируйтесь на уже отправленный файл "
        "<code>sorted_at_usernames.txt</code>.",
        parse_mode=ParseMode.HTML,
        reply_markup=get_clients_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith(f"{_SESSION_PREFIX}close_"))
async def cb_username_tool_close(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔", show_alert=True)
        return
    session_id = callback.data.split("_")[-1]
    _cleanup_session(session_id)
    await callback.message.answer(
        "Закрыто.",
        reply_markup=get_clients_keyboard(),
    )
    await callback.answer()


def _cleanup_session(session_id: str) -> None:
    for p in _session_paths(session_id):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


async def _sweep_old_tool_files() -> None:
    import time

    if not _TOOL_DIR.is_dir():
        return
    cutoff = time.time() - 86400
    for p in _TOOL_DIR.glob("*"):
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
        except OSError:
            pass


@router.message(UsernameListToolFSM.waiting_file, F.text)
async def username_tool_need_document(message: Message):
    if message.from_user.id != OWNER_ID:
        return
    await message.answer("Пришлите файл .txt <b>документом</b> (не текстом в чат).", parse_mode=ParseMode.HTML)
