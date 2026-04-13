"""
Общие утилиты для хендлеров аккаунтов: безопасное редактирование сообщений и текст карточки.
"""
import html
from pathlib import Path

from aiogram.enums import ParseMode

from bot.config import SESSIONS_DIR
from bot.keyboards.main import get_account_card_keyboard
from utils.logger import log


async def safe_edit_message(message, text: str, reply_markup=None, parse_mode=ParseMode.HTML):
    """
    Редактирование сообщения бота: caption для альбома/фото, иначе edit_text.
    """
    try:
        if message.photo or (message.caption is not None):
            await message.edit_caption(
                caption=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
        else:
            await message.edit_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
    except Exception as e:
        log.warning(f"safe_edit_message: {e}")


def build_account_card_text(
    account,
    is_authorized: bool,
    auth_status: str,
    photo_count: int = -1,
    *,
    db_only: bool = False,
) -> str:
    """HTML-текст карточки аккаунта для Control Bot.

    db_only=True — только данные из БД и наличие файла сессии, без подключения к Telegram.
    """
    status_emoji = {
        "active": "🟢",
        "inactive": "🟡",
        "banned": "🔴",
        "flood_wait": "🟠",
        "error": "⚫",
        "spam_blocked": "🚫",
    }.get(account.status.value, "⚪")

    if account.proxy:
        proxy_status = "🟢 Работает" if account.proxy.is_working else "🔴 Не работает"
        proxy_info = f"🌐 {account.proxy.name} ({account.proxy.host}:{account.proxy.port})\n   {proxy_status}"
    else:
        proxy_info = "❌ Без прокси"

    full_name = f"{account.first_name or ''} {account.last_name or ''}".strip()
    if not full_name:
        full_name = "Не указано"

    auth_warning = ""
    if not db_only and not is_authorized:
        auth_warning = (
            "⚠️ <b>Сессия не авторизована!</b>\n"
            "Нажмите '🔄 Перепроверить' для повторной проверки.\n\n"
        )

    if photo_count > 0:
        avatar_info = f"🖼 <b>Аватарок:</b> {photo_count}"
    elif photo_count == 0:
        avatar_info = "🖼 <b>Аватарок:</b> нет"
    else:
        if db_only:
            avatar_info = "🖼 <b>Аватарок:</b> не проверено (нужно подключение)"
        else:
            avatar_info = "🖼 <b>Аватарок:</b> не проверено"

    session_path = Path(SESSIONS_DIR) / f"{account.session_name}.session"
    session_file_ok = session_path.exists()

    if db_only:
        st_human = {
            "active": "активен",
            "inactive": "неактивен",
            "banned": "забанен",
            "flood_wait": "FloodWait",
            "error": "ошибка сессии",
            "spam_blocked": "спам-блок",
        }.get(account.status.value, account.status.value)
        auth_block = (
            "ℹ️ <i>Просмотр без подключения к Telegram — показаны данные из базы.</i>\n\n"
            f"🔐 <b>Статус в БД:</b> {st_human}\n"
            f"💾 <b>Файл сессии:</b> {'найден' if session_file_ok else 'не найден'}\n"
            "<i>Живое подключение — только при «Перепроверить», редактировании профиля, "
            "2FA, аватарках и других действиях.</i>\n\n"
        )
    else:
        auth_block = f"🔐 <b>Авторизация:</b> {auth_status}\n"

    warmup_enabled = bool(getattr(account, "warmup_enabled", False))
    warmup_profile = getattr(account, "warmup_profile", "safe") or "safe"
    warmup_actions_today = int(getattr(account, "warmup_actions_today", 0) or 0)
    warmup_pause_reason = getattr(account, "warmup_pause_reason", None)
    warmup_status = "ВКЛ" if warmup_enabled else "ВЫКЛ"
    if warmup_pause_reason:
        warmup_status += f" (pause: {warmup_pause_reason})"

    lbl = (getattr(account, "list_label", None) or "").strip()
    if lbl:
        list_line = f"🏷 <b>В списке бота:</b> {html.escape(lbl)}\n"
    else:
        auto_hint = html.escape(account.username or account.phone or str(account.id))
        list_line = f"🏷 <b>В списке бота:</b> <i>авто</i> ({auto_hint})\n"

    return (
        f"{status_emoji} <b>Карточка аккаунта</b>\n\n"
        f"{auth_warning}"
        f"👤 <b>Имя:</b> {full_name}\n"
        f"🔖 <b>Username:</b> @{account.username or 'Не указан'}\n"
        f"📱 <b>Телефон:</b> {account.phone}\n"
        f"{list_line}"
        f"📝 <b>Bio:</b> {account.bio or 'Не указано'}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{auth_block}"
        f"🆔 <b>ID:</b> {account.id}\n"
        f"💾 <b>Сессия:</b> <code>{account.session_name}</code>\n\n"
        f"{avatar_info}\n\n"
        f"🌐 <b>Прокси:</b> {proxy_info}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 <b>Статистика:</b>\n"
        f"  • ✅ Отправлено: {account.messages_sent}\n"
        f"  • ❌ Ошибок: {account.messages_failed}\n"
        f"  • 📊 Лимит: {account.messages_today}/{account.daily_limit}\n"
        f"\n🔥 <b>Прогрев:</b> {warmup_status}\n"
        f"  • Профиль: {warmup_profile}\n"
        f"  • Действий сегодня: {warmup_actions_today}\n"
    )


async def show_account_card(
    callback_or_message,
    account,
    is_authorized: bool,
    auth_status: str,
    photo_count: int = -1,
    send_new: bool = False,
    *,
    db_only: bool = False,
):
    """Отправить новое сообщение или отредактировать текущее — карточка аккаунта."""
    text = build_account_card_text(account, is_authorized, auth_status, photo_count, db_only=db_only)
    kb = get_account_card_keyboard(account, None if db_only else is_authorized)
    if send_new:
        await callback_or_message.answer(text=text, parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        await safe_edit_message(callback_or_message, text=text, reply_markup=kb)
