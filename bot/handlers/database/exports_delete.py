"""
Реализация ранее-заглушечных функций меню «База данных»:
  • экспорт списков клиентов по классам (Свежак/ЧС/Живые) в .txt;
  • выгрузка логов: снимок аккаунтов и переписка нейрочата за период;
  • удаление юзеров (из списка / по классу-статусу) с подтверждением.
"""
from __future__ import annotations

import io

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.config import OWNER_ID
from bot.keyboards.database_menu import kb_database_delete
from database.models import ClientStatus
from database.session import session_scope
from services.database import client_delete, client_export
from utils.logger import log

router = Router()


def _owner(uid: int) -> bool:
    return uid == OWNER_ID


def _send_txt(text: str, filename: str) -> BufferedInputFile:
    bio = io.BytesIO(text.encode("utf-8"))
    return BufferedInputFile(bio.getvalue(), filename=filename)


# ==================== Экспорт классов в .txt ====================

_EXPORT = {
    "db_ex_fresh": ("Свежак", "fresh", client_export.export_fresh),
    "db_ex_bl": ("Чёрный список", "blacklist", client_export.export_blacklist),
    "db_ex_alive": ("Живые люди", "alive", client_export.export_alive),
}


@router.callback_query(F.data.in_(tuple(_EXPORT)))
async def db_export_classes(callback: CallbackQuery, state: FSMContext):
    if not _owner(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    title, slug, fn = _EXPORT[callback.data]
    async with session_scope() as session:
        clients = await fn(session)
    body = client_export.render_client_list_txt(clients, title)
    await callback.message.answer_document(
        _send_txt(body, f"clients_{slug}.txt"),
        caption=f"📤 {title}: <b>{len(clients)}</b>",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer(f"Готово: {len(clients)}")


# ==================== Выгрузка логов: аккаунты ====================


@router.callback_query(F.data == "db_log_accounts")
async def db_log_accounts(callback: CallbackQuery):
    if not _owner(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    async with session_scope() as session:
        report = await client_export.export_accounts_report(session)
    await callback.message.answer_document(
        _send_txt(report, "accounts_report.txt"),
        caption="📤 Снимок аккаунтов",
    )
    await callback.answer("Готово")


# ==================== Выгрузка логов: переписка за период ====================


def _kb_chats_windows() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Сутки", callback_data="db_chats_1d"),
                InlineKeyboardButton(text="Неделя", callback_data="db_chats_7d"),
            ],
            [
                InlineKeyboardButton(text="Месяц", callback_data="db_chats_30d"),
                InlineKeyboardButton(text="Вся", callback_data="db_chats_all"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="db_sec_logs")],
        ]
    )


@router.callback_query(F.data == "db_log_chats")
async def db_log_chats_menu(callback: CallbackQuery):
    if not _owner(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await callback.message.edit_text(
        "💬 <b>Переписка нейрочата</b>\n\nВыберите период выгрузки:",
        reply_markup=_kb_chats_windows(),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data.in_(("db_chats_1d", "db_chats_7d", "db_chats_30d", "db_chats_all")))
async def db_chats_export(callback: CallbackQuery):
    if not _owner(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    since = client_export.window_since(callback.data or "")
    async with session_scope() as session:
        body = await client_export.export_chats(session, since=since)
    await callback.message.answer_document(
        _send_txt(body, "neuro_chats.txt"),
        caption="📤 Переписка нейрочата",
    )
    await callback.answer("Готово")


# ==================== Удаление: из списка @username ====================


class DbDeleteFSM(StatesGroup):
    waiting_usernames = State()


def _kb_confirm_list(n: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"⚠️ Удалить {n}", callback_data="db_del_confirm_list")],
            [InlineKeyboardButton(text="⬅️ Отмена", callback_data="db_del_cancel")],
        ]
    )


@router.callback_query(F.data == "db_del_list")
async def db_del_list_start(callback: CallbackQuery, state: FSMContext):
    if not _owner(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.set_state(DbDeleteFSM.waiting_usernames)
    await callback.message.edit_text(
        "🗑 <b>Удаление из списка</b>\n\n"
        "Пришлите @username по одному в строке (или через запятую/пробел). "
        "Будут удалены совпавшие клиенты и все их CRM-данные.\n\n"
        "/cancel — отмена.",
        reply_markup=kb_database_delete(),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.message(DbDeleteFSM.waiting_usernames, F.text)
async def db_del_list_collect(message: Message, state: FSMContext):
    if not _owner(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if raw.lower() in ("/cancel", "cancel", "отмена"):
        await state.clear()
        await message.answer("Отменено.", reply_markup=kb_database_delete())
        return
    usernames = [p for p in raw.replace(",", " ").replace("\n", " ").split() if p.strip()]
    async with session_scope() as session:
        ids = await client_delete.find_client_ids_by_usernames(session, usernames)
    if not ids:
        await state.clear()
        await message.answer(
            "Совпадений не найдено — ничего не удалено.",
            reply_markup=kb_database_delete(),
        )
        return
    await state.update_data(del_ids=ids)
    await message.answer(
        f"Найдено клиентов: <b>{len(ids)}</b> (из {len(usernames)} в списке).\n"
        "Подтвердите удаление:",
        reply_markup=_kb_confirm_list(len(ids)),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data == "db_del_confirm_list")
async def db_del_confirm_list(callback: CallbackQuery, state: FSMContext):
    if not _owner(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    data = await state.get_data()
    ids = data.get("del_ids") or []
    await state.clear()
    if not ids:
        await callback.message.edit_text(
            "Список устарел — повторите.", reply_markup=kb_database_delete()
        )
        await callback.answer()
        return
    try:
        async with session_scope() as session:
            n = await client_delete.delete_clients(session, ids)
        log.info(f"DB delete (list): удалено клиентов={n}")
        await callback.message.edit_text(
            f"✅ Удалено клиентов: <b>{n}</b>.",
            reply_markup=kb_database_delete(),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        log.exception(f"db_del_confirm_list: {e}")
        await callback.message.edit_text(
            f"Ошибка удаления: {e}", reply_markup=kb_database_delete()
        )
    await callback.answer()


@router.callback_query(F.data == "db_del_cancel")
async def db_del_cancel(callback: CallbackQuery, state: FSMContext):
    if not _owner(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(
        "<b>Удаление юзеров</b>", reply_markup=kb_database_delete(), parse_mode=ParseMode.HTML
    )
    await callback.answer("Отменено")


# ==================== Удаление: из базы по классу/статусу ====================

# key -> (подпись, вид, значение)
_DEL_TARGETS = {
    "invalid": ("Невалидные (status=invalid)", "status", ClientStatus.INVALID),
    "blocked": ("Заблокировавшие (status=blocked)", "status", ClientStatus.BLOCKED),
    "bl": ("Чёрный список (класс bl)", "class", "bl"),
    "stop": ("Стоп (класс stop)", "class", "stop"),
}


def _kb_delete_targets() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"db_del_t_{key}")]
        for key, (label, _, _) in _DEL_TARGETS.items()
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="db_m_2133")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_confirm_target(key: str, n: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"⚠️ Удалить {n}", callback_data=f"db_del_do_{key}")],
            [InlineKeyboardButton(text="⬅️ Отмена", callback_data="db_del_db")],
        ]
    )


async def _resolve_target_ids(session, key: str) -> list[int]:
    _label, kind, val = _DEL_TARGETS[key]
    if kind == "status":
        return await client_delete.find_client_ids_by_status(session, val)
    return await client_delete.find_client_ids_by_class(session, val)


@router.callback_query(F.data == "db_del_db")
async def db_del_db_menu(callback: CallbackQuery, state: FSMContext):
    if not _owner(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(
        "🗑 <b>Удаление из базы</b>\n\nВыберите категорию для удаления:",
        reply_markup=_kb_delete_targets(),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("db_del_t_"))
async def db_del_target_preview(callback: CallbackQuery):
    if not _owner(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    key = (callback.data or "").removeprefix("db_del_t_")
    if key not in _DEL_TARGETS:
        await callback.answer("Неизвестно", show_alert=True)
        return
    label = _DEL_TARGETS[key][0]
    async with session_scope() as session:
        ids = await _resolve_target_ids(session, key)
    if not ids:
        await callback.message.edit_text(
            f"<b>{label}</b>\n\nНет подходящих клиентов.",
            reply_markup=_kb_delete_targets(),
            parse_mode=ParseMode.HTML,
        )
        await callback.answer()
        return
    await callback.message.edit_text(
        f"<b>{label}</b>\n\nБудет удалено клиентов: <b>{len(ids)}</b> "
        "(вместе с их CRM-данными). Подтвердите:",
        reply_markup=_kb_confirm_target(key, len(ids)),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("db_del_do_"))
async def db_del_target_do(callback: CallbackQuery):
    if not _owner(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    key = (callback.data or "").removeprefix("db_del_do_")
    if key not in _DEL_TARGETS:
        await callback.answer("Неизвестно", show_alert=True)
        return
    label = _DEL_TARGETS[key][0]
    try:
        async with session_scope() as session:
            ids = await _resolve_target_ids(session, key)
            n = await client_delete.delete_clients(session, ids)
        log.info(f"DB delete (target={key}): удалено клиентов={n}")
        await callback.message.edit_text(
            f"✅ <b>{label}</b>: удалено клиентов <b>{n}</b>.",
            reply_markup=kb_database_delete(),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        log.exception(f"db_del_target_do: {e}")
        await callback.message.edit_text(
            f"Ошибка удаления: {e}", reply_markup=kb_database_delete()
        )
    await callback.answer()
