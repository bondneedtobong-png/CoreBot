"""
Модуль «База данных»: навигация и заглушки подразделов.
2131 — текущий дашборд клиентов (legacy) до полной миграции листов.
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from bot.config import is_authorized_user
from bot.handlers.clients import render_clients_dashboard
from bot.keyboards.database_menu import (
    kb_database_debug_windows,
    kb_database_delete,
    kb_database_detail,
    kb_database_export_groups,
    kb_database_logs,
    kb_database_root,
    kb_database_sheet_manage,
    kb_database_sheets,
    kb_database_stats,
)

router = Router()


def _database_root_keyboard() -> InlineKeyboardMarkup:
    """Короткие пути из нового меню с сохранением прежних разделов."""
    old_rows = kb_database_root().inline_keyboard
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📥 Загрузить базу", callback_data="db_sheet_211")],
            [InlineKeyboardButton(text="📤 Выгрузить базу", callback_data="db_exp_menu")],
            *old_rows,
        ]
    )


def _database_export_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Простой TXT", callback_data="db_exp_txt")],
            [InlineKeyboardButton(text="Продвинутый Excel", callback_data="db_exp_xlsx_menu")],
            [InlineKeyboardButton(text="Прежние списки", callback_data="db_exp_legacy")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_database")],
        ]
    )


def _owner_only(callback: CallbackQuery) -> bool:
    if not is_authorized_user(callback.from_user.id):
        return False
    return True


@router.callback_query(F.data == "menu_database")
async def cb_menu_database(callback: CallbackQuery, state: FSMContext):
    if not _owner_only(callback):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(
        "🗄 <b>База данных</b>\n\n"
        "Единый центр: пользователи (username + user_id), классы-счётчики, теги, "
        "история взаимодействий, бэкапы.\n\n"
        "Выберите раздел:",
        reply_markup=_database_root_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data == "menu_clients")
async def cb_menu_clients_compat(callback: CallbackQuery, state: FSMContext):
    """Совместимость со старыми клавиатурами «Клиенты»."""
    await cb_menu_database(callback, state)


@router.callback_query(F.data == "db_sec_sheets")
async def db_sec_sheets(callback: CallbackQuery, state: FSMContext):
    if not _owner_only(callback):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(
        "📑 <b>Листы</b>\n\n"
        "Импорт и управление списками пользователей.",
        reply_markup=kb_database_sheets(),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data == "db_sec_logs")
async def db_sec_logs(callback: CallbackQuery, state: FSMContext):
    if not _owner_only(callback):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(
        "📜 <b>Логи</b>\n\n"
        "Дебаг логов приложения и выгрузки по переписке/аккаунтам.",
        reply_markup=kb_database_logs(),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data == "db_sec_stats")
async def db_sec_stats(callback: CallbackQuery, state: FSMContext):
    if not _owner_only(callback):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(
        "📈 <b>Статистика</b>\n\n"
        "Метрики и бэкапы базы.",
        reply_markup=kb_database_stats(),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data == "db_sheet_213")
async def db_sheet_213(callback: CallbackQuery, state: FSMContext):
    if not _owner_only(callback):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(
        "<b>Управление</b>\n\n"
        "Списки, выгрузки по группам, удаление.",
        reply_markup=kb_database_sheet_manage(),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data == "db_m_2131")
async def db_m_2131(callback: CallbackQuery, state: FSMContext):
    if not _owner_only(callback):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    await render_clients_dashboard(callback)
    await callback.answer()


@router.callback_query(F.data == "db_m_2132")
async def db_m_2132(callback: CallbackQuery, state: FSMContext):
    if not _owner_only(callback):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await callback.message.edit_text(
        "<b>Выгрузить базу</b>\n\nВыберите формат:",
        reply_markup=_database_export_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data == "db_exp_menu")
async def db_exp_menu(callback: CallbackQuery, state: FSMContext):
    await db_m_2132(callback, state)


@router.callback_query(F.data == "db_exp_legacy")
async def db_exp_legacy(callback: CallbackQuery):
    if not _owner_only(callback):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await callback.message.edit_text(
        "<b>Прежние списки TXT</b>\n\nВыберите группу:",
        reply_markup=kb_database_export_groups(),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data == "db_m_2133")
async def db_m_2133(callback: CallbackQuery, state: FSMContext):
    if not _owner_only(callback):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await callback.message.edit_text(
        "<b>Удаление юзеров</b>",
        reply_markup=kb_database_delete(),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data == "db_sheet_214")
async def db_sheet_214(callback: CallbackQuery, state: FSMContext):
    if not _owner_only(callback):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await callback.message.edit_text(
        "<b>Полная детализация</b>",
        reply_markup=kb_database_detail(),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data == "db_log_debug_menu")
async def db_log_debug_menu(callback: CallbackQuery, state: FSMContext):
    if not _owner_only(callback):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await callback.message.edit_text(
        "<b>Дебаг</b>\n\nВыгрузка логов приложения за период:",
        reply_markup=kb_database_debug_windows(),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()




