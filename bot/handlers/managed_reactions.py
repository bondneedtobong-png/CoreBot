"""Ручная одиночная реакция от выбранного аккаунта на конкретное сообщение."""
from __future__ import annotations

import re
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.config import is_authorized_user
from database.repository import db
from database.repositories import AccountRepository
from services.managed_reactions import (
    list_managed_reaction_history,
    preview_managed_reaction,
    send_managed_reaction,
)

router = Router()
PAGE_SIZE = 10
EMOJIS = ("👍", "❤️", "🔥")
_REASONS = {
    "invalid_emoji": "Эта реакция не поддерживается.",
    "invalid_link": "Ссылка не ведёт на конкретное сообщение.",
    "account_missing": "Аккаунт не найден.",
    "chat_unavailable": "Чат недоступен этому аккаунту.",
    "not_community": "Ссылка должна вести в канал или супергруппу.",
    "chat_mismatch": "Чат по ссылке не совпал с найденным чатом.",
    "target_changed": "Цель по ссылке изменилась после предпросмотра. Проверьте ссылку заново.",
    "permission_unverified": "Не удалось проверить права администратора.",
    "admin_required": "Аккаунт должен быть администратором сообщества.",
    "message_unavailable": "Сообщение недоступно.",
    "message_missing": "Сообщение не найдено.",
    "worker_unavailable": "Аккаунт сейчас недоступен.",
    "already_attempted": "Для этого сообщения реакция уже отправлялась или запускалась.",
    "daily_limit": "Исчерпан дневной лимит аккаунта.",
    "account_not_active": "Аккаунт не активен.",
    "platform_restricted": "Для аккаунта действует ограничение Telegram.",
    "manual_pause": "Аккаунт остановлен оператором.",
    "flood_wait": "Telegram временно ограничил действия аккаунта.",
    "telegram_denied": "Telegram отклонил эту реакцию.",
    "outcome_uncertain": "Результат отправки не удалось подтвердить.",
}
# Поддерживаются публичная ссылка t.me/name/message и приватная t.me/c/id/message.
_MESSAGE_LINK = re.compile(
    r"^https://(?:www\.)?t\.me/(?:c/[1-9]\d*|[A-Za-z][A-Za-z0-9_]{4,31})/[1-9]\d*/?$",
    re.IGNORECASE,
)


def _is_message_link(value: str) -> bool:
    return len(value) <= 255 and _MESSAGE_LINK.fullmatch(value) is not None


class ManagedReactionFlow(StatesGroup):
    waiting_link = State()
    waiting_emoji = State()
    waiting_confirmation = State()


def _account_keyboard(accounts: list, page: int) -> InlineKeyboardMarkup:
    pages = max(1, (len(accounts) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    rows = []
    for account in accounts[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]:
        name = account.list_label or account.username or account.phone
        rows.append([InlineKeyboardButton(
            text=f"{name} · #{account.id}"[:64],
            callback_data=f"managed_reaction_account_{account.id}",
        )])
    if not accounts:
        rows.append([InlineKeyboardButton(text="Нет активных аккаунтов", callback_data="managed_reaction_noop")])
    if pages > 1:
        nav = []
        if page:
            nav.append(InlineKeyboardButton(text="◀️", callback_data=f"managed_reaction_page_{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="managed_reaction_noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(text="▶️", callback_data=f"managed_reaction_page_{page + 1}"))
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="📋 История реакций", callback_data="managed_reaction_history")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="managed_reaction_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _emoji_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=emoji, callback_data=f"managed_reaction_emoji_{i}") for i, emoji in enumerate(EMOJIS)],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="managed_reaction_cancel")],
    ])


def _confirmation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Поставить одну реакцию", callback_data="managed_reaction_confirm")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="managed_reaction_cancel")],
    ])


@router.callback_query(F.data == "managed_reaction_start")
async def start_managed_reaction(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    try:
        async with db.async_session_maker() as session:
            accounts = await AccountRepository.get_active(session)
    except Exception:
        await callback.message.edit_text("Не удалось загрузить аккаунты. Попробуйте позже.")
        await callback.answer()
        return
    await callback.message.edit_text(
        "👍 <b>Одиночная реакция</b>\n\n"
        "Выберите один активный аккаунт. Функция предназначена только для ваших сообществ, "
        "где у аккаунта есть права администратора. Будет отправлена одна реакция. "
        "Автоматические серии и мультиаккаунты не используются.",
        reply_markup=_account_keyboard(accounts, 0), parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("managed_reaction_page_"))
async def page_managed_reaction_accounts(callback: CallbackQuery) -> None:
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    try:
        page = int(callback.data.rsplit("_", 1)[1])
        async with db.async_session_maker() as session:
            accounts = await AccountRepository.get_active(session)
        await callback.message.edit_reply_markup(reply_markup=_account_keyboard(accounts, page))
    except Exception:
        await callback.answer("Не удалось обновить список", show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("managed_reaction_account_"))
async def choose_managed_reaction_account(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    try:
        account_id = int(callback.data.rsplit("_", 1)[1])
        async with db.async_session_maker() as session:
            accounts = await AccountRepository.get_active(session)
        account = next((a for a in accounts if a.id == account_id), None)
    except Exception:
        account = None
    if account is None:
        await state.clear()
        await callback.answer("Аккаунт не найден или уже не активен. Начните заново.", show_alert=True)
        return
    await state.clear()
    await state.update_data(account_id=account_id)
    await state.set_state(ManagedReactionFlow.waiting_link)
    await callback.message.edit_text(
        "Пришлите прямую ссылку на конкретное сообщение: <code>https://t.me/channel/123</code> "
        "(для приватного сообщества: <code>https://t.me/c/123456/123</code>).\n\n"
        "Только собственные сообщества, где выбранный аккаунт имеет права администратора.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="managed_reaction_cancel")]]),
    )
    await callback.answer()


@router.message(ManagedReactionFlow.waiting_link)
async def receive_managed_reaction_link(message: Message, state: FSMContext) -> None:
    if not is_authorized_user(message.from_user.id):
        return
    link = (message.text or "").strip()
    if not _is_message_link(link):
        await message.answer("Нужна прямая ссылка t.me на конкретное сообщение. Например: https://t.me/channel/123")
        return
    await state.update_data(link=link)
    await state.set_state(ManagedReactionFlow.waiting_emoji)
    await message.answer("Выберите одну реакцию:", reply_markup=_emoji_keyboard())


@router.callback_query(F.data.startswith("managed_reaction_emoji_"))
async def preview_managed_reaction_choice(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    if await state.get_state() != ManagedReactionFlow.waiting_emoji.state:
        await callback.answer("Шаг устарел. Начните заново.", show_alert=True)
        return
    try:
        emoji = EMOJIS[int(callback.data.rsplit("_", 1)[1])]
        data = await state.get_data()
        result = await preview_managed_reaction(int(data["account_id"]), str(data["link"]), emoji)
    except (ValueError, KeyError, IndexError):
        await callback.answer("Некорректная реакция или устаревший шаг", show_alert=True)
        return
    except Exception as exc:
        error_reason = getattr(exc, "reason", "")
        await state.clear()
        detail = _REASONS.get(str(error_reason), "Не удалось подготовить предпросмотр. Проверьте доступ аккаунта к сообщению.")
        await callback.message.edit_text(f"❌ {escape(detail)} Начните заново.")
        await callback.answer()
        return
    if not isinstance(result, dict) or result.get("status", "ok") not in {"ok", "success", None}:
        raw_reason = str((result or {}).get("reason") or "")
        reason = escape(_REASONS.get(raw_reason, "Предпросмотр недоступен."))
        await state.clear()
        await callback.message.edit_text(f"❌ {reason}\nНачните заново.")
        await callback.answer()
        return
    if result.get("already_attempted"):
        await state.clear()
        await callback.message.edit_text(
            "Для этого аккаунта попытка реакции на сообщение уже записана. "
            "Повторная отправка заблокирована.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 История", callback_data="managed_reaction_history")],
                [InlineKeyboardButton(text="⬅️ К реакциям", callback_data="managed_reaction_start")],
            ]),
        )
        await callback.answer()
        return
    await state.update_data(emoji=emoji, peer_id=result.get("peer_id"))
    await state.set_state(ManagedReactionFlow.waiting_confirmation)
    title = escape(str(result.get("chat_title") or "Сообщество"))[:180]
    excerpt = escape(str(result.get("text_excerpt") or "(без текста)"))[:500]
    account_name = escape(str(result.get("account_name") or data["account_id"]))[:100]
    msg_id = escape(str(result.get("message_id") or "—"))[:32]
    await callback.message.edit_text(
        "<b>Проверьте перед отправкой</b>\n\n"
        f"Аккаунт: <b>{account_name}</b>\nЧат: <b>{title}</b>\nСообщение #{msg_id}\n"
        f"Текст: {excerpt}\nРеакция: {emoji}\n\n"
        "Подтвердите одну реакцию. Используйте только в собственном сообществе, "
        "где аккаунт является администратором.",
        parse_mode="HTML", reply_markup=_confirmation_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "managed_reaction_confirm")
async def confirm_managed_reaction(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    if await state.get_state() != ManagedReactionFlow.waiting_confirmation.state:
        await callback.answer("Подтверждение устарело. Начните заново.", show_alert=True)
        return
    data = await state.get_data()
    await state.clear()
    try:
        expected = data.get("peer_id")
        target_guard = {"expected_peer_id": int(expected)} if expected is not None else {}
        result = await send_managed_reaction(
            int(data["account_id"]), str(data["link"]), str(data["emoji"]),
            callback.from_user.id, **target_guard,
        )
    except Exception:
        await callback.message.edit_text("❌ Не удалось отправить реакцию. Проверьте доступ аккаунта к сообщению.")
        await callback.answer()
        return
    status = str((result or {}).get("status", "error")) if isinstance(result, dict) else "error"
    if status in {"sent", "ok", "success"}:
        text = "✅ Одна реакция отправлена."
    else:
        raw_reason = str((result or {}).get("reason") or status or "")
        reason = escape(_REASONS.get(raw_reason, "Ошибка отправки."))
        text = f"❌ Реакция не отправлена: {reason}"
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 История", callback_data="managed_reaction_history")],
            [InlineKeyboardButton(text="⬅️ К прогреву", callback_data="menu_warmup")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data == "managed_reaction_cancel")
async def cancel_managed_reaction(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(
        "Одиночная реакция отменена.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ К прогреву", callback_data="menu_warmup")],
        ]),
    )
    await callback.answer("Отменено")


@router.callback_query(F.data == "managed_reaction_history")
async def managed_reaction_history(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    try:
        attempts = await list_managed_reaction_history(limit=20)
    except Exception:
        await callback.answer("Не удалось загрузить историю", show_alert=True)
        return
    lines = ["📋 <b>Последние попытки реакций</b>"]
    for item in attempts:
        name = escape(str(item.get("account_name") or "Аккаунт"))[:60]
        emoji = escape(str(item.get("emoji") or ""))[:8]
        status = escape(str(item.get("status") or ""))[:24]
        reason = escape(str(item.get("reason") or ""))[:50]
        link = escape(str(item.get("link") or ""), quote=True)[:255]
        created = item.get("created_at")
        date = created.strftime("%d.%m %H:%M UTC") if hasattr(created, "strftime") else escape(str(created or ""))[:30]
        lines.append(f"\n{date} · {name} · {emoji} · {status}")
        if link:
            lines.append(f"<code>{link}</code>")
        if reason:
            lines.append(f"Причина: {reason}")
    if not attempts:
        lines.append("\nПока нет отправок.")
    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="managed_reaction_history")],
            [InlineKeyboardButton(text="⬅️ К реакциям", callback_data="managed_reaction_start")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data == "managed_reaction_noop")
async def managed_reaction_noop(callback: CallbackQuery) -> None:
    await callback.answer()
