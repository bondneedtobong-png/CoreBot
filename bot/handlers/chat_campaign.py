"""Control-bot flow for scheduled text posts in account-administered chats."""
from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.config import is_authorized_user
from database.repositories import AccountRepository
from database.session import session_scope
from services.owned_chat_campaign import (
    CampaignError, campaign_history, enqueue_campaign, parse_links, preview_campaign,
    stop_campaign, validate_text,
)

router = Router()


class Flow(StatesGroup):
    links = State()
    text = State()
    confirmation = State()


def _keyboard(*rows: tuple[str, str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data)] for label, data in rows
    ])


_REASONS = {
    "chat_count": "Укажите от 1 до 5 чатов.",
    "invalid_links": "Нужны публичные ссылки вида https://t.me/name, по одной на строку.",
    "duplicate_chat": "Один чат указан несколько раз.",
    "invalid_text": "Нужен текст длиной до 4000 символов.",
    "account_unavailable": "Аккаунт недоступен.",
    "worker_unavailable": "Не удалось подключить выбранный аккаунт.",
    "ownership_unverified": "Не удалось проверить права в чате.",
    "admin_required": "Выбранный аккаунт не администратор чата.",
    "posting_rights_required": "У администратора нет права публиковать сообщения в канале.",
    "not_community": "Ссылка не ведёт в канал или супергруппу.",
}


@router.message(Command("chat_campaign"))
async def start(message: Message, state: FSMContext) -> None:
    if not is_authorized_user(message.from_user.id):
        return
    await state.clear()
    async with session_scope() as session:
        accounts = await AccountRepository.get_active(session)
    rows = [(f"{account.display_title[:42]} · #{account.id}", f"chat_campaign_account_{account.id}")
            for account in accounts[:30]]
    rows.append(("📋 История", "chat_campaign_history"))
    await message.answer(
        "<b>Публикация в своих чатах</b>\nВыберите один аккаунт. Доступны до 5 явно выбранных "
        "каналов или супергрупп, где он администратор. Одна текстовая публикация в каждый чат.",
        parse_mode="HTML", reply_markup=_keyboard(*rows),
    )


@router.callback_query(F.data == "chat_campaign_open")
async def open_from_menu(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    await state.clear()
    async with session_scope() as session:
        accounts = await AccountRepository.get_active(session)
    rows = [(f"{account.display_title[:42]} · #{account.id}", f"chat_campaign_account_{account.id}")
            for account in accounts[:30]]
    rows.append(("📋 История", "chat_campaign_history"))
    await callback.message.edit_text(
        "<b>Публикация в своих чатах</b>\nВыберите один аккаунт. Доступны до 5 явно выбранных "
        "каналов или супергрупп, где он администратор. Одна текстовая публикация в каждый чат.",
        parse_mode="HTML", reply_markup=_keyboard(*rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("chat_campaign_account_"))
async def account_selected(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    try:
        account_id = int(callback.data.rsplit("_", 1)[1])
        async with session_scope() as session:
            accounts = await AccountRepository.get_active(session)
        if not any(account.id == account_id for account in accounts):
            raise ValueError
    except ValueError:
        await callback.answer("Аккаунт недоступен", show_alert=True)
        return
    await state.clear()
    await state.update_data(account_id=account_id)
    await state.set_state(Flow.links)
    await callback.message.edit_text(
        "Пришлите 1–5 публичных ссылок на ваши чаты, по одной на строку. "
        "Например: https://t.me/my_channel\n\nПрава администратора будут проверены перед запуском и отправкой.",
        reply_markup=_keyboard(("❌ Отмена", "chat_campaign_cancel")),
    )
    await callback.answer()


@router.message(Flow.links)
async def links_received(message: Message, state: FSMContext) -> None:
    if not is_authorized_user(message.from_user.id):
        return
    try:
        links = parse_links(message.text or "")
    except CampaignError as exc:
        await message.answer(_REASONS.get(exc.reason, "Проверьте ссылки."))
        return
    await state.update_data(links=links)
    await state.set_state(Flow.text)
    await message.answer("Пришлите текст публикации (до 4000 символов).")


@router.message(Flow.text)
async def text_received(message: Message, state: FSMContext) -> None:
    if not is_authorized_user(message.from_user.id):
        return
    try:
        text = validate_text(message.text or "")
        data = await state.get_data()
        preview = await preview_campaign(int(data["account_id"]), data["links"], text)
    except CampaignError as exc:
        await message.answer(_REASONS.get(exc.reason, "Предпросмотр недоступен."))
        return
    except Exception:
        await message.answer("Не удалось проверить чаты. Попробуйте позже.")
        return
    await state.update_data(text=text, preview=preview)
    await state.set_state(Flow.confirmation)
    targets = "\n".join(f"• {escape(item['title'])} (<code>{escape(item['link'])}</code>)" for item in preview)
    await message.answer("Текст публикации:\n" + text, parse_mode=None)
    await message.answer(
        f"<b>Предпросмотр</b>\nАккаунт #{data['account_id']}\n{targets}\n\n"
        "Проверьте текст в сообщении выше и выберите время запуска.",
        parse_mode="HTML",
        reply_markup=_keyboard(
            ("✅ Сейчас", "chat_campaign_schedule_0"),
            ("⏲ Через 10 минут", "chat_campaign_schedule_600"),
            ("⏲ Через 1 час", "chat_campaign_schedule_3600"),
            ("❌ Отмена", "chat_campaign_cancel"),
        ),
    )


@router.callback_query(F.data.startswith("chat_campaign_schedule_"))
async def schedule(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    if await state.get_state() != Flow.confirmation.state:
        await callback.answer("Предпросмотр устарел", show_alert=True)
        return
    data = await state.get_data()
    await state.clear()
    try:
        delay = int(callback.data.rsplit("_", 1)[1])
        ids = await enqueue_campaign(int(data["account_id"]), callback.from_user.id,
                                     data["preview"], data["text"], delay)
    except (CampaignError, ValueError, KeyError):
        await callback.message.edit_text("Не удалось создать задание. Откройте /chat_campaign заново.")
        await callback.answer()
        return
    await callback.message.edit_text(
        f"✅ Кампания #{ids[0]} запланирована: {len(ids)} чат(ов).\n"
        "Статус каждой публикации доступен в истории. После начала отправки её результат может быть "
        "неопределённым при сбое и автоматический повтор не выполняется.",
        reply_markup=_keyboard(("⏹ Остановить", f"chat_campaign_stop_{ids[0]}"),
                               ("📋 История", "chat_campaign_history")),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("chat_campaign_stop_"))
async def stop(callback: CallbackQuery) -> None:
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    try:
        campaign_id = int(callback.data.rsplit("_", 1)[1])
        count = await stop_campaign(campaign_id, callback.from_user.id)
    except (ValueError, CampaignError):
        count = 0
    await callback.answer(f"Остановка запрошена для {count} публикаций", show_alert=True)


@router.callback_query(F.data == "chat_campaign_history")
async def history(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    await state.clear()
    rows = await campaign_history(callback.from_user.id)
    lines = ["<b>История публикаций по чатам</b>"]
    for row in rows:
        reason = escape(row["reason"] or "")[:80]
        lines.append(f"\n#{row['campaign_id']} · {escape(row['chat'])} · {escape(row['status'])}")
        if reason:
            lines.append(f"Причина/результат: {reason}")
    if not rows:
        lines.append("\nЗаписей пока нет.")
    active_campaigns = list(dict.fromkeys(
        row["campaign_id"] for row in rows if row["status"] in {"pending", "processing"}
    ))[:5]
    buttons = [(f"⏹ Остановить #{campaign_id}", f"chat_campaign_stop_{campaign_id}")
               for campaign_id in active_campaigns]
    buttons.append(("🔄 Обновить", "chat_campaign_history"))
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML",
                                     reply_markup=_keyboard(*buttons))
    await callback.answer()


@router.callback_query(F.data == "chat_campaign_cancel")
async def cancel(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text("Создание публикации отменено.")
    await callback.answer()
