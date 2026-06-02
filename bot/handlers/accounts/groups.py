"""Группы аккаунтов: создание, состав и массовые операции по группе."""
import asyncio
import html
import re
from pathlib import Path
from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.exc import IntegrityError

from bot.config import AVATARS_TEMP_DIR, OWNER_ID, SESSIONS_DIR
from bot.handlers.accounts.common import safe_edit_message
from bot.handlers.accounts.states import GroupBulk2FAFSM, GroupBulkProfileFSM, GroupManageFSM
from bot.keyboards.main import (
    get_account_groups_menu_keyboard,
    get_cancel_with_back_keyboard,
    get_group_add_pick_keyboard,
    get_group_detail_keyboard,
    get_group_members_keyboard,
)
from database.session import session_scope
from database.repositories import AccountRepository, GroupRepository
from utils.logger import log

router = Router()
_TPL_VAR_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}", re.IGNORECASE)


def _group_bulk_profile_menu_keyboard(gid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Изменить имя", callback_data=f"group_bulk_profile_edit_name_{gid}")],
            [InlineKeyboardButton(text="✏️ Изменить username", callback_data=f"group_bulk_profile_edit_username_{gid}")],
            [InlineKeyboardButton(text="🖼 Изменить фото", callback_data=f"group_bulk_profile_edit_photo_{gid}")],
            [InlineKeyboardButton(text="✏️ Изменить bio", callback_data=f"group_bulk_profile_edit_bio_{gid}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"group_view_{gid}")],
        ]
    )


@router.callback_query(F.data == "accounts_groups")
async def cb_accounts_groups(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await state.clear()
    await callback.answer()
    try:
        async with session_scope() as session:
            groups = await GroupRepository.get_all(session)

        if not groups:
            text = (
                "📁 <b>Группы аккаунтов</b>\n\n"
                "Пока нет групп. Создайте (например «Колумбия», «USA»), "
                "добавьте аккаунты в состав и запускайте проверки прокси и спам-блока по группе."
            )
        else:
            text = (
                "📁 <b>Группы аккаунтов</b>\n\n"
                f"Всего: {len(groups)}\n"
                "Выберите группу или создайте новую."
            )

        await safe_edit_message(
            callback.message,
            text,
            reply_markup=get_account_groups_menu_keyboard(groups),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        log.error(f"accounts_groups: {e}")
        await safe_edit_message(
            callback.message,
            "⚠️ Ошибка загрузки групп.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="accounts_list")]]
            ),
        )


async def _render_group_detail(message, gid: int) -> bool:
    """Карточка группы (меню действий). False — группа не найдена."""
    async with session_scope() as session:
        g = await GroupRepository.get_by_id(session, gid)

    if not g:
        await safe_edit_message(
            message,
            "❌ Группа не найдена.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="accounts_groups")]]
            ),
        )
        return False

    n = len(g.accounts)
    text = (
        f"📂 <b>{g.name}</b>\n\n"
        f"Аккаунтов в группе: <b>{n}</b>\n\n"
        "Проверки затрагивают все аккаунты группы (с загруженной сессией)."
    )
    await safe_edit_message(
        message,
        text,
        reply_markup=get_group_detail_keyboard(g.id),
        parse_mode=ParseMode.HTML,
    )
    return True


@router.callback_query(F.data.startswith("group_view_"))
async def cb_group_view(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await state.clear()
    gid = int(callback.data.split("_")[-1])
    await callback.answer()
    await _render_group_detail(callback.message, gid)


@router.callback_query(F.data == "groups_create_start")
async def cb_groups_create_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await state.set_state(GroupManageFSM.waiting_for_group_name)
    await safe_edit_message(
        callback.message,
        "➕ <b>Новая группа</b>\n\n"
        "Введите название (до 100 символов), например: <code>Колумбия</code> или <code>USA</code>.",
        reply_markup=get_cancel_with_back_keyboard("cancel_accounts", "accounts_groups"),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.message(GroupManageFSM.waiting_for_group_name)
async def process_group_name(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return

    name = (message.text or "").strip()
    if not name or len(name) > 100:
        await message.answer("❌ Введите непустое название (до 100 символов).")
        return

    try:
        async with session_scope() as session:
            g = await GroupRepository.create(session, name)
    except IntegrityError:
        await message.answer("❌ Группа с таким именем уже существует.")
        return
    except Exception as e:
        log.error(f"create group: {e}")
        await message.answer("❌ Ошибка при создании группы.")
        return

    await state.clear()
    await message.answer(
        f"✅ Группа «{g.name}» создана.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📂 Открыть группу",
                        callback_data=f"group_view_{g.id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="📁 Все группы",
                        callback_data="accounts_groups",
                    ),
                ],
            ]
        ),
        parse_mode=ParseMode.HTML,
    )


def _format_proxy_results(g_name: str, results: dict, id_to_account: dict) -> str:
    text = f"✅ <b>Проверка прокси — {g_name}</b>\n\n"
    working = not_working = unknown = 0
    for aid, is_working in results.items():
        acc = id_to_account.get(aid)
        display = html.escape(acc.list_row_caption if acc else str(aid))
        if is_working is None:
            unknown += 1
            text += f"⚪ {display} — нет сессии / не загружен\n"
        elif is_working:
            working += 1
            text += f"🟢 {display} — OK\n"
        else:
            not_working += 1
            text += f"🔴 {display} — FAIL\n"
    text += f"\n📊 Итого: {working} OK, {not_working} FAIL"
    if unknown:
        text += f", {unknown} без проверки"
    return text


def _format_spam_results(g_name: str, results: dict, id_to_account: dict) -> str:
    text = f"✅ <b>Проверка спам-блока — {g_name}</b>\n\n"
    blocked = clean = unknown = 0
    for aid, is_blocked in results.items():
        acc = id_to_account.get(aid)
        display = html.escape(acc.list_row_caption if acc else str(aid))
        if is_blocked is None:
            unknown += 1
            text += f"⚠️ {display} — не удалось проверить\n"
        elif is_blocked:
            blocked += 1
            text += f"🚫 {display} — SPAM BLOCKED\n"
        else:
            clean += 1
            text += f"✅ {display} — чист\n"
    text += f"\n📊 Итого: {clean} чистых, {blocked} с блоком"
    if unknown:
        text += f", {unknown} не удалось проверить"
    return text


@router.callback_query(F.data.startswith("group_check_proxy_"))
async def cb_group_check_proxy(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    gid = int(callback.data.split("_")[-1])

    async with session_scope() as session:
        g = await GroupRepository.get_by_id(session, gid)

    if not g:
        await callback.answer("Группа не найдена", show_alert=True)
        return

    ids = [a.id for a in g.accounts]
    if not ids:
        await callback.answer("В группе нет аккаунтов", show_alert=True)
        return

    await callback.answer()
    status_msg = await callback.message.answer("🔄 Проверка прокси для группы...")
    from workers.manager import worker_manager

    id_to_account = {a.id: a for a in g.accounts}

    try:
        await worker_manager.ensure_workers_for_account_ids(ids)

        results = await worker_manager.check_proxies_for_account_ids(ids)
        text = _format_proxy_results(g.name, results, id_to_account)

        await safe_edit_message(
            status_msg,
            text,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ К группе", callback_data=f"group_view_{gid}")],
                ]
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        log.error(f"group_check_proxy: {e}")
        await safe_edit_message(
            status_msg,
            f"❌ Ошибка: {e}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ К группе", callback_data=f"group_view_{gid}")],
                ]
            ),
        )
    finally:
        await worker_manager.disconnect_all()


@router.callback_query(F.data.startswith("group_check_spam_"))
async def cb_group_check_spam(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    gid = int(callback.data.split("_")[-1])

    async with session_scope() as session:
        g = await GroupRepository.get_by_id(session, gid)

    if not g:
        await callback.answer("Группа не найдена", show_alert=True)
        return

    ids = [a.id for a in g.accounts]
    if not ids:
        await callback.answer("В группе нет аккаунтов", show_alert=True)
        return

    await callback.answer()
    status_msg = await callback.message.answer("🔍 Проверка спам-блока для группы...")
    from workers.manager import worker_manager

    id_to_account = {a.id: a for a in g.accounts}

    try:
        await worker_manager.ensure_workers_for_account_ids(ids)

        results = await worker_manager.check_spam_blocks_for_account_ids(ids)
        text = _format_spam_results(g.name, results, id_to_account)

        await safe_edit_message(
            status_msg,
            text,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ К группе", callback_data=f"group_view_{gid}")],
                ]
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        log.error(f"group_check_spam: {e}")
        await safe_edit_message(
            status_msg,
            f"❌ Ошибка: {e}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ К группе", callback_data=f"group_view_{gid}")],
                ]
            ),
        )
    finally:
        await worker_manager.disconnect_all()


async def _render_group_members(message, gid: int) -> None:
    async with session_scope() as session:
        g = await GroupRepository.get_by_id(session, gid)

    if not g:
        await safe_edit_message(
            message,
            "❌ Группа не найдена.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="accounts_groups")]]
            ),
        )
        return

    accounts = sorted(g.accounts, key=lambda a: a.list_row_caption.lower())
    lines = [f"👥 <b>Состав — {g.name}</b>\n"]
    if not accounts:
        lines.append("Пока пусто — добавьте аккаунты кнопкой ниже.")
    else:
        for a in accounts:
            lines.append(
                f"• {html.escape(a.list_row_caption)} <code>(id {a.id})</code>"
            )
    text = "\n".join(lines)

    await safe_edit_message(
        message,
        text,
        reply_markup=get_group_members_keyboard(g.id, accounts),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data.startswith("group_members_"))
async def cb_group_members(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    gid = int(callback.data.split("_")[-1])
    await callback.answer()
    await _render_group_members(callback.message, gid)


async def _render_group_add_menu(message, gid: int) -> None:
    """Экран выбора аккаунта для добавления; после добавления остаёмся здесь с обновлённым списком."""
    async with session_scope() as session:
        g = await GroupRepository.get_by_id(session, gid)
        all_accounts = await AccountRepository.get_all(session)

    if not g:
        await safe_edit_message(
            message,
            "❌ Группа не найдена.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="accounts_groups")]]
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    member_ids = {a.id for a in g.accounts}
    available = [a for a in all_accounts if a.id not in member_ids]

    if not available:
        await safe_edit_message(
            message,
            "➕ <b>Добавить аккаунт</b>\n\n"
            "Все аккаунты уже в этой группе (или аккаунтов в базе нет).",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"group_members_{gid}")],
                ]
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    await safe_edit_message(
        message,
        "➕ <b>Выберите аккаунт</b> для добавления в группу:",
        reply_markup=get_group_add_pick_keyboard(gid, available),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data.startswith("group_add_menu_"))
async def cb_group_add_menu(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    gid = int(callback.data.split("_")[-1])
    await callback.answer()
    await _render_group_add_menu(callback.message, gid)


@router.callback_query(F.data.startswith("group_pick_"))
async def cb_group_pick_account(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    parts = callback.data.split("_")
    gid = int(parts[2])
    aid = int(parts[3])

    async with session_scope() as session:
        ok = await GroupRepository.add_account(session, gid, aid)

    if not ok:
        await callback.answer("Не удалось добавить", show_alert=True)
        return

    await callback.answer("✅ Добавлено")
    await _render_group_add_menu(callback.message, gid)


@router.callback_query(F.data.startswith("group_rm_"))
async def cb_group_remove_account(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    parts = callback.data.split("_")
    gid = int(parts[2])
    aid = int(parts[3])

    async with session_scope() as session:
        ok = await GroupRepository.remove_account(session, gid, aid)

    if not ok:
        await callback.answer("Не удалось убрать", show_alert=True)
        return

    await callback.answer("Убрано из группы")
    await _render_group_members(callback.message, gid)


@router.callback_query(F.data.startswith("group_delete_confirm_"))
async def cb_group_delete_confirm(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    gid = int(callback.data.split("_")[-1])
    await callback.answer()

    async with session_scope() as session:
        g = await GroupRepository.get_by_id(session, gid)

    name = g.name if g else "?"
    text = (
        f"🗑 Удалить группу «<b>{name}</b>»?\n\n"
        "Аккаунты из базы не удаляются, снимается только связь с группой."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Да, удалить",
                    callback_data=f"group_delete_yes_{gid}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Отмена",
                    callback_data=f"group_view_{gid}",
                ),
            ],
        ]
    )
    await safe_edit_message(callback.message, text, reply_markup=kb, parse_mode=ParseMode.HTML)


@router.callback_query(F.data.startswith("group_delete_yes_"))
async def cb_group_delete_yes(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    gid = int(callback.data.split("_")[-1])

    async with session_scope() as session:
        ok = await GroupRepository.delete(session, gid)

    await callback.answer("Удалено" if ok else "Ошибка")

    async with session_scope() as session:
        groups = await GroupRepository.get_all(session)

    text = (
        "📁 <b>Группы аккаунтов</b>\n\n"
        + (f"Всего: {len(groups)}\nВыберите группу или создайте новую." if groups else "Пока нет групп.")
    )
    await safe_edit_message(
        callback.message,
        text,
        reply_markup=get_account_groups_menu_keyboard(groups),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data == "groups_delete_empty")
async def cb_groups_delete_empty(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    async with session_scope() as session:
        n = await GroupRepository.count_empty(session)
    await callback.answer()
    if not n:
        await safe_edit_message(
            callback.message,
            "Пустых групп нет.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="accounts_groups")]]
            ),
            parse_mode=ParseMode.HTML,
        )
        return
    await safe_edit_message(
        callback.message,
        f"🗑 Удалить пустые группы: <b>{n}</b>?\n\n"
        "Удалятся только группы <b>без аккаунтов</b>. Группы с аккаунтами не "
        "затрагиваются, сами аккаунты — тоже.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=f"✅ Да, удалить {n}", callback_data="groups_delete_empty_do")],
                [InlineKeyboardButton(text="⬅️ Отмена", callback_data="accounts_groups")],
            ]
        ),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data == "groups_delete_empty_do")
async def cb_groups_delete_empty_do(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    async with session_scope() as session:
        n = await GroupRepository.delete_empty(session)
    log.info(f"Удалено пустых групп аккаунтов: {n}")
    await callback.answer(f"Удалено: {n}")
    async with session_scope() as session:
        groups = await GroupRepository.get_all(session)
    text = (
        "📁 <b>Группы аккаунтов</b>\n\n"
        + (
            f"Удалено пустых: {n}. Всего сейчас: {len(groups)}.\n"
            "Выберите группу или создайте новую."
            if groups
            else f"Удалено пустых: {n}. Групп больше нет — создайте новую."
        )
    )
    await safe_edit_message(
        callback.message,
        text,
        reply_markup=get_account_groups_menu_keyboard(groups),
        parse_mode=ParseMode.HTML,
    )


# ==================== Массовая 2FA по группе ====================


async def _safe_disconnect_worker(worker) -> None:
    try:
        if worker.client:
            await worker.disconnect()
    except Exception:
        pass


async def _apply_2fa_single_account(worker, password: str) -> None:
    from telethon import functions

    if not worker.is_connected:
        ok = await worker.connect()
        if not ok or not worker.is_connected:
            raise RuntimeError("сессия не авторизована или ошибка подключения")
    ok = await worker.client.edit_2fa(
        new_password=password,
        hint="Восстановление через поддержку",
    )
    if not ok:
        raise RuntimeError("не удалось установить пароль (возможно уже включена 2FA)")
    await worker.client(functions.auth.ResetAuthorizationsRequest())


def _format_group_2fa_report(
    group_name: str,
    successes: list[str],
    failures: list[tuple[str, str]],
) -> str:
    lines = [
        f"🔐 <b>Массовая 2FA — {html.escape(group_name)}</b>\n",
        f"✅ Успешно: <b>{len(successes)}</b>",
        f"❌ Ошибок: <b>{len(failures)}</b>",
        "",
    ]
    if successes:
        lines.append("<b>Успешно:</b>")
        for s in successes[:40]:
            lines.append(f"• {html.escape(s)}")
        if len(successes) > 40:
            lines.append(f"… и ещё {len(successes) - 40}")
        lines.append("")
    if failures:
        lines.append("<b>Ошибки:</b>")
        for label, err in failures[:30]:
            short = (err or "?")[:180]
            lines.append(f"• {html.escape(label)} — <code>{html.escape(short)}</code>")
        if len(failures) > 30:
            lines.append(f"… и ещё {len(failures) - 30}")
    return "\n".join(lines)


async def _run_group_bulk_2fa(
    account_ids: list[int],
    id_to_account: dict,
    password: str,
) -> tuple[list[str], list[tuple[str, str]]]:
    from workers.manager import worker_manager

    successes: list[str] = []
    failures: list[tuple[str, str]] = []

    await worker_manager.ensure_workers_for_account_ids(account_ids)

    ids_sorted = sorted(account_ids)
    for i, aid in enumerate(ids_sorted):
        acc = id_to_account.get(aid)
        label = (acc.username or acc.phone or str(aid)) if acc else str(aid)
        worker = worker_manager.workers.get(aid)
        if not worker:
            failures.append((label, "нет .session / аккаунт не загружен в менеджер"))
            continue
        try:
            await _apply_2fa_single_account(worker, password)
            successes.append(label)
        except Exception as e:
            failures.append((label, str(e)))
        finally:
            await _safe_disconnect_worker(worker)
        if i + 1 < len(ids_sorted):
            await asyncio.sleep(0.35)

    return successes, failures


@router.callback_query(F.data.startswith("group_bulk_2fa_start_"))
async def cb_group_bulk_2fa_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    gid = int(callback.data.split("_")[-1])

    async with session_scope() as session:
        g = await GroupRepository.get_by_id(session, gid)

    if not g:
        await callback.answer("Группа не найдена", show_alert=True)
        return
    if not g.accounts:
        await callback.answer("В группе нет аккаунтов", show_alert=True)
        return

    await state.update_data(group_id=gid, group_name=g.name)
    await state.set_state(GroupBulk2FAFSM.waiting_for_password)

    await callback.answer()
    await safe_edit_message(
        callback.message,
        f"🔐 <b>Массовая 2FA — {html.escape(g.name)}</b>\n\n"
        f"Один пароль будет установлен на <b>все</b> аккаунты в группе "
        f"(<b>{len(g.accounts)}</b> шт.).\n\n"
        "На каждом аккаунте будут завершены прочие сессии.\n\n"
        "Введите пароль:\n\n"
        "❌ Отмена: /start",
        reply_markup=get_cancel_with_back_keyboard(
            f"group_bulk_2fa_abort_{gid}", f"group_view_{gid}"
        ),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data.startswith("group_bulk_2fa_abort_"))
async def cb_group_bulk_2fa_abort(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    gid = int(callback.data.split("_")[-1])
    await state.clear()
    await callback.answer("Отменено")
    await _render_group_detail(callback.message, gid)


@router.message(GroupBulk2FAFSM.waiting_for_password)
async def process_group_bulk_2fa_password(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return

    pwd = (message.text or "").strip()
    if pwd.lower() in ("/start", "отмена", "cancel"):
        await state.clear()
        await message.answer("❌ Массовая установка 2FA отменена.")
        return

    data = await state.get_data()
    gid = data.get("group_id")
    await state.update_data(password=pwd)
    await state.set_state(GroupBulk2FAFSM.waiting_for_password_confirm)

    await message.answer(
        "🔐 <b>Подтверждение пароля</b>\n\n"
        "Введите тот же пароль ещё раз:",
        reply_markup=get_cancel_with_back_keyboard(
            f"group_bulk_2fa_abort_{gid}", f"group_view_{gid}"
        ),
        parse_mode=ParseMode.HTML,
    )


@router.message(GroupBulk2FAFSM.waiting_for_password_confirm)
async def process_group_bulk_2fa_confirm(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return

    confirm = (message.text or "").strip()
    if confirm.lower() in ("/start", "отмена", "cancel"):
        await state.clear()
        await message.answer("❌ Массовая установка 2FA отменена.")
        return

    data = await state.get_data()
    gid = data.get("group_id")
    group_name = data.get("group_name") or "?"
    password = data.get("password")

    if password != confirm:
        await message.answer("❌ Пароли не совпадают. Введите пароль заново:")
        await state.update_data(password=None)
        await state.set_state(GroupBulk2FAFSM.waiting_for_password)
        return

    async with session_scope() as session:
        g = await GroupRepository.get_by_id(session, gid)

    if not g or not g.accounts:
        await state.clear()
        await message.answer("❌ Группа пуста или не найдена.")
        return

    ids = [a.id for a in g.accounts]
    id_to_account = {a.id: a for a in g.accounts}

    status_msg = await message.answer("⏳ Установка 2FA для всех аккаунтов группы…")

    from workers.manager import worker_manager

    try:
        successes, failures = await _run_group_bulk_2fa(ids, id_to_account, password)
        log.info(
            f"Массовая 2FA группа «{group_name}»: ok={len(successes)}, fail={len(failures)}"
        )
        report = _format_group_2fa_report(group_name, successes, failures)
        if len(report) > 4000:
            report = report[:3900] + "\n\n… <i>сообщение обрезано</i>"

        await safe_edit_message(
            status_msg,
            report,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ К группе", callback_data=f"group_view_{gid}")],
                ]
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        log.error(f"group bulk 2fa: {e}")
        await safe_edit_message(
            status_msg,
            f"❌ Критическая ошибка: <code>{html.escape(str(e)[:500])}</code>",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ К группе", callback_data=f"group_view_{gid}")],
                ]
            ),
            parse_mode=ParseMode.HTML,
        )
    finally:
        await worker_manager.disconnect_all()
        await state.clear()


# ==================== Массовый шаблон профиля по группе ====================


def _render_template(tpl: str, account, idx: int, group_name: str) -> str:
    full_name = f"{(account.first_name or '').strip()} {(account.last_name or '').strip()}".strip()
    username = (account.username or "").strip().lstrip("@")
    mapping = {
        "i": str(idx),
        "n": str(idx),
        "id": str(account.id),
        "phone": (account.phone or "").strip(),
        "username": username,
        "first_name": (account.first_name or "").strip(),
        "last_name": (account.last_name or "").strip(),
        "full_name": full_name,
        "group": group_name,
    }
    def repl(match: re.Match[str]) -> str:
        key = (match.group(1) or "").strip()
        return mapping.get(key, "")
    return _TPL_VAR_RE.sub(repl, tpl).strip()


async def _load_group_for_bulk(gid: int):
    async with session_scope() as session:
        return await GroupRepository.get_by_id(session, gid)


def _format_bulk_result(title: str, successes: list[str], failures: list[tuple[str, str]]) -> str:
    out = [
        f"🧩 <b>{html.escape(title)}</b>\n",
        f"✅ Успешно: <b>{len(successes)}</b>",
        f"❌ Ошибок: <b>{len(failures)}</b>",
    ]
    if failures:
        out.append("\n<b>Ошибки:</b>")
        for lbl, err in failures[:25]:
            out.append(f"• {html.escape(lbl)} — <code>{html.escape((err or '?')[:180])}</code>")
        if len(failures) > 25:
            out.append(f"… и ещё {len(failures) - 25}")
    return "\n".join(out)


async def _apply_group_profile_updates(
    gid: int,
    *,
    name_template: str | None = None,
    username_template: str | None = None,
    bio_template: str | None = None,
    photo_path: str | None = None,
) -> tuple[str, list[str], list[tuple[str, str]]]:
    g = await _load_group_for_bulk(gid)
    if not g or not g.accounts:
        raise RuntimeError("Группа пуста или не найдена")

    from workers.manager import Worker
    from telethon.errors import FloodWaitError, UsernameInvalidError, UsernameOccupiedError
    from telethon.tl.functions.account import UpdateProfileRequest, UpdateUsernameRequest

    group_name = g.name
    accounts = sorted(g.accounts, key=lambda a: a.id)
    successes: list[str] = []
    failures: list[tuple[str, str]] = []

    for idx, acc in enumerate(accounts, 1):
        label = acc.list_row_caption
        session_path = SESSIONS_DIR / f"{acc.session_name}.session"
        if not session_path.exists():
            failures.append((label, "файл .session не найден"))
            continue

        worker = Worker(acc, session_path, acc.proxy)
        try:
            connected = await worker.connect(quiet=True)
            if not connected or not worker.client:
                failures.append((label, "не удалось подключить аккаунт"))
                continue

            local_first = (acc.first_name or "").strip()
            local_last = (acc.last_name or "").strip()
            local_bio = (acc.bio or "").strip()
            local_username = (acc.username or "").strip().lstrip("@")

            update_kwargs = {}
            if name_template is not None:
                full = _render_template(name_template, acc, idx, group_name)
                parts = full.split(maxsplit=1)
                local_first = parts[0] if parts else ""
                local_last = parts[1] if len(parts) > 1 else ""
                update_kwargs["first_name"] = local_first
                update_kwargs["last_name"] = local_last or None
            if bio_template is not None:
                local_bio = _render_template(bio_template, acc, idx, group_name)
                update_kwargs["about"] = local_bio
            if update_kwargs:
                await worker.client(UpdateProfileRequest(**update_kwargs))

            if username_template is not None:
                new_username = _render_template(
                    username_template, acc, idx, group_name
                ).lstrip("@")
                await worker.client(UpdateUsernameRequest(username=new_username))
                local_username = new_username

            if photo_path:
                ok = await worker.set_profile_photo(photo_path)
                if not ok:
                    raise RuntimeError("не удалось установить фото профиля")

            async with session_scope() as session:
                await AccountRepository.update_profile(
                    session,
                    acc.id,
                    first_name=local_first or None,
                    last_name=local_last or None,
                    bio=local_bio or None,
                )
                if username_template is not None:
                    await AccountRepository.update_username(session, acc.id, local_username)

            successes.append(label)
        except (UsernameOccupiedError, UsernameInvalidError, FloodWaitError) as e:
            failures.append((label, str(e)))
        except Exception as e:
            failures.append((label, str(e)))
        finally:
            await _safe_disconnect_worker(worker)
        await asyncio.sleep(0.35)

    return group_name, successes, failures


@router.callback_query(F.data.startswith("group_bulk_profile_start_"))
async def cb_group_bulk_profile_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    gid = int(callback.data.split("_")[-1])
    async with session_scope() as session:
        g = await GroupRepository.get_by_id(session, gid)
    if not g:
        await callback.answer("Группа не найдена", show_alert=True)
        return
    if not g.accounts:
        await callback.answer("В группе нет аккаунтов", show_alert=True)
        return

    await callback.answer()
    await safe_edit_message(
        callback.message,
        f"🧩 <b>Массовое редактирование профиля — {html.escape(g.name)}</b>\n\n"
        "Выберите, что редактировать для всех аккаунтов в группе.",
        reply_markup=_group_bulk_profile_menu_keyboard(gid),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data.startswith("group_bulk_profile_abort_"))
async def cb_group_bulk_profile_abort(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    gid = int(callback.data.split("_")[-1])
    await state.clear()
    await callback.answer("Отменено")
    await _render_group_detail(callback.message, gid)


@router.callback_query(F.data.startswith("group_bulk_profile_edit_name_"))
async def cb_group_bulk_profile_edit_name(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    gid = int(callback.data.split("_")[-1])
    await state.clear()
    await state.update_data(group_id=gid)
    await state.set_state(GroupBulkProfileFSM.waiting_for_name_template)
    await callback.answer()
    await safe_edit_message(
        callback.message,
        "✏️ <b>Изменить имя в группе</b>\n\n"
        "Отправьте шаблон имени для всех аккаунтов.\n"
        "Пример: <code>Alex {i}</code>\n\n"
        "Переменные: <code>{i}</code>, <code>{id}</code>, <code>{phone}</code>, "
        "<code>{username}</code>, <code>{first_name}</code>, <code>{last_name}</code>, "
        "<code>{full_name}</code>, <code>{group}</code>.",
        reply_markup=get_cancel_with_back_keyboard(
            f"group_bulk_profile_abort_{gid}", f"group_bulk_profile_start_{gid}"
        ),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data.startswith("group_bulk_profile_edit_username_"))
async def cb_group_bulk_profile_edit_username(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    gid = int(callback.data.split("_")[-1])
    await state.clear()
    await state.update_data(group_id=gid)
    await state.set_state(GroupBulkProfileFSM.waiting_for_username_template)
    await callback.answer()
    await safe_edit_message(
        callback.message,
        "✏️ <b>Изменить username в группе</b>\n\n"
        "Отправьте шаблон username без @.\n"
        "Пример: <code>geo_{group}_{i}</code>\n\n"
        "⚠️ Шаблон должен давать уникальные username.",
        reply_markup=get_cancel_with_back_keyboard(
            f"group_bulk_profile_abort_{gid}", f"group_bulk_profile_start_{gid}"
        ),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data.startswith("group_bulk_profile_edit_bio_"))
async def cb_group_bulk_profile_edit_bio(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    gid = int(callback.data.split("_")[-1])
    await state.clear()
    await state.update_data(group_id=gid)
    await state.set_state(GroupBulkProfileFSM.waiting_for_bio_template)
    await callback.answer()
    await safe_edit_message(
        callback.message,
        "✏️ <b>Изменить bio в группе</b>\n\n"
        "Отправьте шаблон bio.\n"
        "Пример: <code>Trader from {group}, account #{i}</code>",
        reply_markup=get_cancel_with_back_keyboard(
            f"group_bulk_profile_abort_{gid}", f"group_bulk_profile_start_{gid}"
        ),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data.startswith("group_bulk_profile_edit_photo_"))
async def cb_group_bulk_profile_edit_photo(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    gid = int(callback.data.split("_")[-1])
    await state.clear()
    await state.update_data(group_id=gid)
    await state.set_state(GroupBulkProfileFSM.waiting_for_photo)
    await callback.answer()
    await safe_edit_message(
        callback.message,
        "🖼 <b>Изменить фото в группе</b>\n\n"
        "Отправьте одно фото (или изображение документом).\n"
        "Оно будет установлено всем аккаунтам группы.",
        reply_markup=get_cancel_with_back_keyboard(
            f"group_bulk_profile_abort_{gid}", f"group_bulk_profile_start_{gid}"
        ),
        parse_mode=ParseMode.HTML,
    )


@router.message(GroupBulkProfileFSM.waiting_for_name_template)
async def process_group_bulk_name_template(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return

    tpl = (message.text or "").strip()
    if tpl.lower() in ("/start", "отмена", "cancel"):
        await state.clear()
        await message.answer("❌ Отменено.")
        return

    data = await state.get_data()
    gid = int(data.get("group_id"))
    status_msg = await message.answer("⏳ Обновляю name для аккаунтов группы…")
    try:
        gname, ok, fail = await _apply_group_profile_updates(gid, name_template=tpl)
        await safe_edit_message(
            status_msg,
            _format_bulk_result(f"Массовое изменение имени — {gname}", ok, fail),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ К группе", callback_data=f"group_view_{gid}")]]
            ),
            parse_mode=ParseMode.HTML,
        )
    finally:
        await state.clear()


@router.message(GroupBulkProfileFSM.waiting_for_username_template)
async def process_group_bulk_username_template(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return

    tpl = (message.text or "").strip().lstrip("@")
    if tpl.lower() in ("/start", "отмена", "cancel"):
        await state.clear()
        await message.answer("❌ Отменено.")
        return

    data = await state.get_data()
    gid = int(data.get("group_id"))
    status_msg = await message.answer("⏳ Обновляю username для аккаунтов группы…")
    try:
        gname, ok, fail = await _apply_group_profile_updates(gid, username_template=tpl)
        await safe_edit_message(
            status_msg,
            _format_bulk_result(f"Массовое изменение username — {gname}", ok, fail),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ К группе", callback_data=f"group_view_{gid}")]]
            ),
            parse_mode=ParseMode.HTML,
        )
    finally:
        await state.clear()


@router.message(GroupBulkProfileFSM.waiting_for_bio_template)
async def process_group_bulk_bio_template(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return

    tpl = (message.text or "").strip()
    if tpl.lower() in ("/start", "отмена", "cancel"):
        await state.clear()
        await message.answer("❌ Отменено.")
        return

    data = await state.get_data()
    gid = int(data.get("group_id"))
    status_msg = await message.answer("⏳ Обновляю bio для аккаунтов группы…")
    try:
        gname, ok, fail = await _apply_group_profile_updates(gid, bio_template=tpl)
        await safe_edit_message(
            status_msg,
            _format_bulk_result(f"Массовое изменение bio — {gname}", ok, fail),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ К группе", callback_data=f"group_view_{gid}")]]
            ),
            parse_mode=ParseMode.HTML,
        )
    finally:
        await state.clear()


def _is_image_document(message: Message) -> bool:
    doc = message.document
    if not doc:
        return False
    mime = (doc.mime_type or "").lower()
    if mime.startswith("image/"):
        return True
    name = (doc.file_name or "").lower()
    return name.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))


async def _download_group_photo(message: Message, gid: int) -> Path | None:
    AVATARS_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    if message.photo:
        src = message.photo[-1]
        file_id = src.file_id
        unique = src.file_unique_id
        ext = ".jpg"
    elif message.document and _is_image_document(message):
        src = message.document
        file_id = src.file_id
        unique = src.file_unique_id
        ext = Path(src.file_name or "").suffix.lower() or ".jpg"
    else:
        return None

    dest = AVATARS_TEMP_DIR / f"group_{gid}_{unique}{ext}"
    try:
        fi = await message.bot.get_file(file_id)
        await message.bot.download_file(fi.file_path, destination=dest)
        return dest
    except Exception:
        return None


@router.message(GroupBulkProfileFSM.waiting_for_photo, F.photo)
@router.message(GroupBulkProfileFSM.waiting_for_photo, F.document)
async def process_group_bulk_photo(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return
    if message.document and not _is_image_document(message):
        await message.answer("❌ Пришлите изображение (фото или image-документ).")
        return

    data = await state.get_data()
    gid = int(data.get("group_id"))
    temp_photo = await _download_group_photo(message, gid)
    if not temp_photo or not temp_photo.exists():
        await message.answer("❌ Не удалось скачать изображение.")
        await state.clear()
        return

    status_msg = await message.answer("⏳ Обновляю photo для аккаунтов группы…")
    try:
        gname, ok, fail = await _apply_group_profile_updates(gid, photo_path=str(temp_photo))
        await safe_edit_message(
            status_msg,
            _format_bulk_result(f"Массовое изменение фото — {gname}", ok, fail),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ К группе", callback_data=f"group_view_{gid}")]]
            ),
            parse_mode=ParseMode.HTML,
        )
    finally:
        try:
            temp_photo.unlink(missing_ok=True)
        except Exception:
            pass
        await state.clear()


@router.message(GroupBulkProfileFSM.waiting_for_photo)
async def process_group_bulk_photo_invalid(message: Message):
    if message.from_user.id != OWNER_ID:
        return
    await message.answer("📎 Отправьте фото или изображение документом.")
