"""Выгрузка клиентской базы в TXT и XLSX через меню владельца."""
from __future__ import annotations

import io
import re
from hashlib import sha256
from html import escape

import xlsxwriter
from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select

from bot.config import is_authorized_user
from database.models import Client, ClientClassCounter
from database.session import session_scope

router = Router()

_USERNAME = re.compile(r"[A-Za-z0-9_]{5,32}\Z")
_PAGE_SIZE = 8
_MAX_SELECTED_CLASSES = 20
_MAX_CLIENTS = 50_000
_MAX_FILE_BYTES = 25 * 1024 * 1024


def _normalized_username(raw: str | None) -> str | None:
    value = (raw or "").strip().removeprefix("@").lower()
    return value if _USERNAME.fullmatch(value) else None


async def _simple_txt(session) -> tuple[bytes, int]:
    seen: set[str] = set()
    lines: list[str] = []
    result = await session.stream(select(Client.username).order_by(Client.id))
    async for (raw,) in result:
        username = _normalized_username(raw)
        if username is None or username in seen:
            continue
        if len(lines) >= _MAX_CLIENTS:
            raise ValueError(f"Больше {_MAX_CLIENTS} username. Сузьте базу перед выгрузкой.")
        seen.add(username)
        lines.append(f"@{username}")
    body = ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
    if len(body) > _MAX_FILE_BYTES:
        raise ValueError("TXT превышает допустимый размер файла.")
    return body, len(lines)


async def _available_classes(session) -> list[tuple[str, int]]:
    rows = await session.execute(
        select(ClientClassCounter.class_key, func.count(ClientClassCounter.client_id))
        .where(ClientClassCounter.count > 0)
        .group_by(ClientClassCounter.class_key)
        .order_by(ClientClassCounter.class_key)
    )
    return [(key, count) for key, count in rows if key]


def _class_token(key: str) -> str:
    return sha256(key.encode("utf-8")).hexdigest()[:20]


def _class_keyboard(
    classes: list[tuple[str, int]], selected: set[str], page: int
) -> InlineKeyboardMarkup:
    max_page = max(0, (len(classes) - 1) // _PAGE_SIZE)
    page = min(max(page, 0), max_page)
    rows = []
    for key, count in classes[page * _PAGE_SIZE : (page + 1) * _PAGE_SIZE]:
        label = key if len(key) <= 25 else f"{key[:22]}…"
        rows.append([
            InlineKeyboardButton(
                text=f"{'✅' if key in selected else '▫️'} {label} ({count})",
                callback_data=f"db_exp_xlsx_toggle_{_class_token(key)}",
            )
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"db_exp_xlsx_page_{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{max_page + 1}", callback_data="db_exp_xlsx_nop"))
    if page < max_page:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"db_exp_xlsx_page_{page + 1}"))
    rows.append(nav)
    rows.extend([
        [InlineKeyboardButton(text=f"📥 Скачать XLSX ({len(selected)})", callback_data="db_exp_xlsx_do")],
        [InlineKeyboardButton(text="⬅️ К форматам", callback_data="db_exp_menu")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_classes(callback: CallbackQuery, state: FSMContext, *, page: int) -> None:
    async with session_scope() as session:
        classes = await _available_classes(session)
    if not classes:
        await callback.message.edit_text(
            "<b>Продвинутый Excel</b>\n\nВ базе пока нет классов с положительным счётчиком.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⬅️ К форматам", callback_data="db_exp_menu")
            ]]),
            parse_mode=ParseMode.HTML,
        )
        await callback.answer()
        return
    current = {key for key, _count in classes}
    data = await state.get_data()
    selected = set(data.get("db_exp_selected", [])) & current
    page = min(max(0, page), (len(classes) - 1) // _PAGE_SIZE)
    await state.update_data(db_exp_selected=sorted(selected), db_exp_page=page)
    chosen = ", ".join(escape(key) for key in sorted(selected)) or "пока нет"
    await callback.message.edit_text(
        "<b>Продвинутый Excel</b>\n\n"
        "Выберите один или несколько классов. В файл попадут клиенты с положительным "
        "счётчиком <b>хотя бы одного</b> выбранного класса. "
        "Таблица покажет значения только выбранных классов.\n\n"
        f"Выбрано: {chosen}",
        reply_markup=_class_keyboard(classes, selected, page),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


async def _advanced_xlsx(session, selected: list[str]) -> tuple[bytes, int]:
    """Одна строка на клиента; выбранные классы — числовые колонки."""
    output = io.BytesIO()
    book = xlsxwriter.Workbook(output, {"in_memory": True, "strings_to_formulas": False, "strings_to_urls": False})
    sheet = book.add_worksheet("Клиенты")
    header = book.add_format({"bold": True, "bg_color": "#E9EEF5"})
    sheet.freeze_panes(1, 0)
    sheet.set_column(0, 0, 12)
    sheet.set_column(1, 1, 36)
    sheet.set_column(2, 2, 16)
    sheet.set_column(3, 2 + len(selected), 20)
    for col, label in enumerate(("ID в БД", "Username", "Статус", *selected)):
        sheet.write_string(0, col, label, header)

    query = (
        select(Client.id, Client.username, Client.status, ClientClassCounter.class_key, ClientClassCounter.count)
        .join(ClientClassCounter, ClientClassCounter.client_id == Client.id)
        .where(ClientClassCounter.class_key.in_(selected), ClientClassCounter.count > 0)
        .order_by(Client.id, ClientClassCounter.class_key)
    )
    row_number = 1
    current_id = None
    username = None
    status = None
    counters: dict[str, int] = {}

    def flush() -> None:
        nonlocal row_number
        if current_id is None:
            return
        if row_number > _MAX_CLIENTS:
            raise ValueError(f"В выборке больше {_MAX_CLIENTS} клиентов. Выберите меньше классов.")
        sheet.write_number(row_number, 0, current_id)
        sheet.write_string(row_number, 1, f"@{username}" if username else "")
        sheet.write_string(row_number, 2, status.value if hasattr(status, "value") else str(status))
        for col, key in enumerate(selected, start=3):
            sheet.write_number(row_number, col, counters.get(key, 0))
        row_number += 1

    try:
        result = await session.stream(query)
        async for client_id, raw_username, client_status, class_key, count in result:
            if current_id != client_id:
                flush()
                current_id = client_id
                username = _normalized_username(raw_username)
                status = client_status
                counters = {}
            counters[class_key] = count
        flush()
        sheet.autofilter(0, 0, max(0, row_number - 1), 2 + len(selected))
    finally:
        book.close()
    body = output.getvalue()
    if len(body) > _MAX_FILE_BYTES:
        raise ValueError("XLSX превышает допустимый размер файла. Выберите меньше классов.")
    return body, row_number - 1


@router.callback_query(F.data == "db_exp_txt")
async def db_exp_txt(callback: CallbackQuery):
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    try:
        async with session_scope() as session:
            body, count = await _simple_txt(session)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.message.answer_document(
        BufferedInputFile(body, filename="clients_usernames.txt"),
        caption=f"Простой TXT: {count} уникальных @username",
    )
    await callback.answer("Готово")


@router.callback_query(F.data == "db_exp_xlsx_menu")
async def db_exp_xlsx_menu(callback: CallbackQuery, state: FSMContext):
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.update_data(db_exp_selected=[], db_exp_page=0)
    await _show_classes(callback, state, page=0)


@router.callback_query(F.data.startswith("db_exp_xlsx_page_"))
async def db_exp_xlsx_page(callback: CallbackQuery, state: FSMContext):
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    try:
        page = int((callback.data or "").removeprefix("db_exp_xlsx_page_"))
    except ValueError:
        await callback.answer("Неверная страница", show_alert=True)
        return
    await _show_classes(callback, state, page=page)


@router.callback_query(F.data.startswith("db_exp_xlsx_toggle_"))
async def db_exp_xlsx_toggle(callback: CallbackQuery, state: FSMContext):
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    token = (callback.data or "").removeprefix("db_exp_xlsx_toggle_")
    async with session_scope() as session:
        classes = await _available_classes(session)
    keys = [key for key, _count in classes if _class_token(key) == token]
    if len(keys) != 1:
        await callback.answer("Класс больше недоступен", show_alert=True)
        return
    key = keys[0]
    data = await state.get_data()
    selected = set(data.get("db_exp_selected", []))
    if key in selected:
        selected.remove(key)
    elif len(selected) >= _MAX_SELECTED_CLASSES:
        await callback.answer(f"Можно выбрать до {_MAX_SELECTED_CLASSES} классов", show_alert=True)
        return
    else:
        selected.add(key)
    await state.update_data(db_exp_selected=sorted(selected))
    await _show_classes(callback, state, page=data.get("db_exp_page", 0))


@router.callback_query(F.data == "db_exp_xlsx_do")
async def db_exp_xlsx_do(callback: CallbackQuery, state: FSMContext):
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    data = await state.get_data()
    selected = list(dict.fromkeys(data.get("db_exp_selected", [])))
    if not selected or len(selected) > _MAX_SELECTED_CLASSES:
        await callback.answer("Сначала выберите классы", show_alert=True)
        return
    try:
        async with session_scope() as session:
            current = {key for key, _count in await _available_classes(session)}
            if any(key not in current for key in selected):
                await callback.answer("Список классов изменился. Откройте выбор заново.", show_alert=True)
                return
            body, count = await _advanced_xlsx(session, selected)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.message.answer_document(
        BufferedInputFile(body, filename="clients_by_classes.xlsx"),
        caption=f"Продвинутый Excel: {count} клиентов, {len(selected)} классов",
    )
    await callback.answer("Готово")


@router.callback_query(F.data == "db_exp_xlsx_nop")
async def db_exp_xlsx_nop(callback: CallbackQuery):
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await callback.answer()
