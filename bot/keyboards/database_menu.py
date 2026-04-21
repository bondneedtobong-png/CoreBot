"""
Клавиатуры модуля «База данных» (навигация по разделам).
"""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def kb_database_root() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📑 Листы", callback_data="db_sec_sheets"),
                InlineKeyboardButton(text="📜 Логи", callback_data="db_sec_logs"),
            ],
            [
                InlineKeyboardButton(text="📈 Статистика", callback_data="db_sec_stats"),
            ],
            [InlineKeyboardButton(text="⬅️ В главное меню", callback_data="menu_back")],
        ]
    )


def kb_database_sheets() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Загрузить new", callback_data="db_sheet_211")],
            [InlineKeyboardButton(text="Загрузить с проверкой", callback_data="db_sheet_212")],
            [InlineKeyboardButton(text="Управление", callback_data="db_sheet_213")],
            [InlineKeyboardButton(text="Полная детализация", callback_data="db_sheet_214")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_database")],
        ]
    )


def kb_database_sheet_manage() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Текущий список клиентов", callback_data="db_m_2131")],
            [InlineKeyboardButton(text="Выгрузить данные", callback_data="db_m_2132")],
            [InlineKeyboardButton(text="Удаление юзеров", callback_data="db_m_2133")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="db_sec_sheets")],
        ]
    )


def kb_database_export_groups() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Свежак", callback_data="db_ex_fresh")],
            [InlineKeyboardButton(text="Чёрный список", callback_data="db_ex_bl")],
            [InlineKeyboardButton(text="Живые люди", callback_data="db_ex_alive")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="db_sheet_213")],
        ]
    )


def kb_database_delete() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Из списка", callback_data="db_del_list")],
            [InlineKeyboardButton(text="Из базы", callback_data="db_del_db")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="db_sheet_213")],
        ]
    )


def kb_database_detail() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Скачать", callback_data="db_dl2141")],
            [InlineKeyboardButton(text="Поиск по классам", callback_data="db_search2142")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="db_sec_sheets")],
        ]
    )


def kb_database_logs() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Дебаг", callback_data="db_log_debug_menu")],
            [InlineKeyboardButton(text="Аккаунты", callback_data="db_log_accounts")],
            [InlineKeyboardButton(text="Переписка", callback_data="db_log_chats")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_database")],
        ]
    )


def kb_database_debug_windows() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="10 мин", callback_data="db_dbg_10m"),
                InlineKeyboardButton(text="30 мин", callback_data="db_dbg_30m"),
            ],
            [
                InlineKeyboardButton(text="2 ч", callback_data="db_dbg_2h"),
                InlineKeyboardButton(text="Сутки", callback_data="db_dbg_1d"),
            ],
            [
                InlineKeyboardButton(text="Неделя", callback_data="db_dbg_7d"),
                InlineKeyboardButton(text="Месяц", callback_data="db_dbg_30d"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="db_sec_logs")],
        ]
    )


def kb_database_stats() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="По клиентам", callback_data="db_st_231")],
            [InlineKeyboardButton(text="По аккаунтам", callback_data="db_st_232")],
            [InlineKeyboardButton(text="По переписке", callback_data="db_st_233")],
            [InlineKeyboardButton(text="Сохранить бэкап", callback_data="db_st_234")],
            [InlineKeyboardButton(text="Загрузить бэкап", callback_data="db_st_235")],
            [InlineKeyboardButton(text="Объединить бэкап", callback_data="db_st_236")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_database")],
        ]
    )
