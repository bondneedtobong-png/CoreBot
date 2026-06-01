"""
Массовая чистка флота из бота: прокси и аккаунты.

- Прокси: удалить свободные / удалить все (с отвязкой от аккаунтов).
- Аккаунты: проверить прокси сейчас, отвязать прокси у всех, удалить все
  аккаунты (+ их .session и зависимые данные).

Всё деструктивное — только с подтверждением. Связано с безопасной
загрузкой: аккаунты с мёртвым/отсутствующим прокси не подключаются
(workers.manager.connect_all, require_working_proxy=True).
"""
from __future__ import annotations

import os

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from bot.config import OWNER_ID, SESSIONS_DIR
from bot.keyboards.main import get_accounts_keyboard, get_proxy_keyboard
from database.session import session_scope
from services.database import fleet_cleanup
from utils.logger import log

router = Router()


def _owner(uid: int) -> bool:
    return uid == OWNER_ID


async def _safe_edit(callback: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except TelegramBadRequest as e:
        if "not modified" not in (e.message or str(e)).lower():
            raise


def _confirm_kb(do_cb: str, back_cb: str, n: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"⚠️ Да, выполнить ({n})", callback_data=do_cb)],
            [InlineKeyboardButton(text="⬅️ Отмена", callback_data=back_cb)],
        ]
    )


# ==================== Прокси: чистка ====================


def _proxy_cleanup_kb(total: int, free: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"🧹 Удалить свободные ({free})", callback_data="proxy_clean_free")],
            [InlineKeyboardButton(text=f"🗑 Удалить ВСЕ прокси ({total})", callback_data="proxy_clean_all")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_proxy")],
        ]
    )


@router.callback_query(F.data == "proxy_cleanup_menu")
async def proxy_cleanup_menu(callback: CallbackQuery, state: FSMContext):
    if not _owner(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    async with session_scope() as session:
        total = await fleet_cleanup.count_proxies(session)
        free = await fleet_cleanup.count_free_proxies(session)
    await _safe_edit(
        callback,
        "🧹 <b>Чистка прокси</b>\n\n"
        f"Всего прокси: <b>{total}</b>\n"
        f"Свободных (без аккаунтов): <b>{free}</b>\n\n"
        "Удаление прокси автоматически отвязывает их от аккаунтов.",
        _proxy_cleanup_kb(total, free),
    )
    await callback.answer()


@router.callback_query(F.data == "proxy_clean_free")
async def proxy_clean_free(callback: CallbackQuery):
    if not _owner(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    async with session_scope() as session:
        free = await fleet_cleanup.count_free_proxies(session)
    if not free:
        await callback.answer("Свободных прокси нет", show_alert=True)
        return
    await _safe_edit(
        callback,
        f"🧹 <b>Удалить свободные прокси</b>\n\nБудет удалено: <b>{free}</b> "
        "(не назначенных ни одному аккаунту). Продолжить?",
        _confirm_kb("proxy_clean_free_do", "proxy_cleanup_menu", free),
    )
    await callback.answer()


@router.callback_query(F.data == "proxy_clean_free_do")
async def proxy_clean_free_do(callback: CallbackQuery):
    if not _owner(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    async with session_scope() as session:
        n = await fleet_cleanup.delete_free_proxies(session)
    log.info(f"Fleet cleanup: удалено свободных прокси = {n}")
    await _safe_edit(
        callback, f"✅ Удалено свободных прокси: <b>{n}</b>.", get_proxy_keyboard()
    )
    await callback.answer("Готово")


@router.callback_query(F.data == "proxy_clean_all")
async def proxy_clean_all(callback: CallbackQuery):
    if not _owner(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    async with session_scope() as session:
        total = await fleet_cleanup.count_proxies(session)
    if not total:
        await callback.answer("Прокси нет", show_alert=True)
        return
    await _safe_edit(
        callback,
        f"🗑 <b>Удалить ВСЕ прокси</b>\n\nБудет удалено: <b>{total}</b>. "
        "У всех аккаунтов прокси будет отвязан.\n\n"
        "⚠️ Действие нельзя отменить. Продолжить?",
        _confirm_kb("proxy_clean_all_do", "proxy_cleanup_menu", total),
    )
    await callback.answer()


@router.callback_query(F.data == "proxy_clean_all_do")
async def proxy_clean_all_do(callback: CallbackQuery):
    if not _owner(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    async with session_scope() as session:
        n = await fleet_cleanup.delete_all_proxies(session)
    log.info(f"Fleet cleanup: удалено ВСЕХ прокси = {n}")
    await _safe_edit(
        callback,
        f"✅ Удалено прокси: <b>{n}</b>. Прокси отвязаны у всех аккаунтов.",
        get_proxy_keyboard(),
    )
    await callback.answer("Готово")


# ==================== Аккаунты: чистка / проверка прокси ====================


def _acc_cleanup_kb(total: int, with_proxy: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Проверить прокси аккаунтов", callback_data="acc_proxy_check")],
            [InlineKeyboardButton(text=f"🔌 Отвязать прокси у всех ({with_proxy})", callback_data="acc_detach_all")],
            [InlineKeyboardButton(text=f"🗑 Удалить ВСЕ аккаунты ({total})", callback_data="acc_delete_all")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_accounts")],
        ]
    )


@router.callback_query(F.data == "acc_cleanup_menu")
async def acc_cleanup_menu(callback: CallbackQuery, state: FSMContext):
    if not _owner(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    async with session_scope() as session:
        total = await fleet_cleanup.count_accounts(session)
        with_proxy = await fleet_cleanup.count_accounts_with_proxy(session)
    await _safe_edit(
        callback,
        "🧹 <b>Чистка / проверка аккаунтов</b>\n\n"
        f"Всего аккаунтов: <b>{total}</b>\n"
        f"С назначенным прокси: <b>{with_proxy}</b>\n\n"
        "<i>Аккаунты с мёртвым/отсутствующим прокси при старте не подключаются "
        "и не авторизуются — это защита от банов.</i>",
        _acc_cleanup_kb(total, with_proxy),
    )
    await callback.answer()


@router.callback_query(F.data == "acc_proxy_check")
async def acc_proxy_check(callback: CallbackQuery):
    if not _owner(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    from workers.manager import worker_manager

    await callback.answer("Проверяю прокси…")
    if not worker_manager.workers:
        await worker_manager.load_accounts()
    results = await worker_manager.precheck_proxies()

    ok = dead = noproxy = 0
    lines = []
    for aid, worker in worker_manager.workers.items():
        st = results.get(aid)
        if st is None:
            noproxy += 1
            mark = "⚪ нет прокси"
        elif st:
            ok += 1
            mark = "✅ работает"
        else:
            dead += 1
            mark = "⛔ не работает"
        label = worker.account.display_title
        lines.append(f"{mark} — #{aid} {label}")

    shown = "\n".join(lines[:30])
    if len(lines) > 30:
        shown += f"\n… и ещё {len(lines) - 30}"
    text = (
        "🔍 <b>Проверка прокси аккаунтов</b>\n\n"
        f"✅ рабочих: <b>{ok}</b> · ⛔ мёртвых: <b>{dead}</b> · ⚪ без прокси: <b>{noproxy}</b>\n\n"
        f"{shown}\n\n"
        "<i>Подключатся при старте только аккаунты с рабочим прокси.</i>"
    )
    async with session_scope() as session:
        total = await fleet_cleanup.count_accounts(session)
        with_proxy = await fleet_cleanup.count_accounts_with_proxy(session)
    await _safe_edit(callback, text, _acc_cleanup_kb(total, with_proxy))


@router.callback_query(F.data == "acc_detach_all")
async def acc_detach_all(callback: CallbackQuery):
    if not _owner(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    async with session_scope() as session:
        n = await fleet_cleanup.count_accounts_with_proxy(session)
    if not n:
        await callback.answer("Ни у одного аккаунта нет прокси", show_alert=True)
        return
    await _safe_edit(
        callback,
        f"🔌 <b>Отвязать прокси у всех</b>\n\nЗатронет аккаунтов: <b>{n}</b>. "
        "Аккаунты и сессии не удаляются, только снимается привязка прокси. Продолжить?",
        _confirm_kb("acc_detach_all_do", "acc_cleanup_menu", n),
    )
    await callback.answer()


@router.callback_query(F.data == "acc_detach_all_do")
async def acc_detach_all_do(callback: CallbackQuery):
    if not _owner(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    async with session_scope() as session:
        n = await fleet_cleanup.detach_all_account_proxies(session)
    log.info(f"Fleet cleanup: отвязан прокси у аккаунтов = {n}")
    await _safe_edit(
        callback, f"✅ Прокси отвязан у <b>{n}</b> аккаунтов.", get_accounts_keyboard()
    )
    await callback.answer("Готово")


@router.callback_query(F.data == "acc_delete_all")
async def acc_delete_all(callback: CallbackQuery):
    if not _owner(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    async with session_scope() as session:
        total = await fleet_cleanup.count_accounts(session)
    if not total:
        await callback.answer("Аккаунтов нет", show_alert=True)
        return
    await _safe_edit(
        callback,
        f"🗑 <b>Удалить ВСЕ аккаунты</b>\n\nБудет удалено: <b>{total}</b>.\n\n"
        "Удаляются: записи аккаунтов, файлы .session, диалоги нейрочата, логи "
        "отправок, mail-сессии, прогрев, очередь. Клиенты/классы/рассылки "
        "сохраняются.\n\n⚠️ Действие нельзя отменить. Продолжить?",
        _confirm_kb("acc_delete_all_do", "acc_cleanup_menu", total),
    )
    await callback.answer()


@router.callback_query(F.data == "acc_delete_all_do")
async def acc_delete_all_do(callback: CallbackQuery):
    if not _owner(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    from workers.manager import worker_manager

    await callback.answer("Удаляю…")
    async with session_scope() as session:
        session_names = await fleet_cleanup.list_account_session_names(session)

    # Отключаем воркеров и закрываем сессии до удаления файлов (Windows).
    try:
        await worker_manager.disconnect_all()
    except Exception as e:
        log.warning(f"disconnect_all при удалении аккаунтов: {e}")
    worker_manager.workers.clear()

    async with session_scope() as session:
        n = await fleet_cleanup.delete_all_accounts(session)

    removed = 0
    for name in session_names:
        path = SESSIONS_DIR / f"{name}.session"
        try:
            if path.exists():
                os.remove(path)
                removed += 1
        except Exception as e:
            log.warning(f"Не удалить сессию {path}: {e}")

    log.info(f"Fleet cleanup: удалено аккаунтов={n}, файлов сессий={removed}")
    await _safe_edit(
        callback,
        f"✅ Удалено аккаунтов: <b>{n}</b> (файлов сессий: {removed}).",
        get_accounts_keyboard(),
    )
