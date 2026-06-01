"""
Экран «Статус системы» — быстрый пульс инстанса для главного меню.

Только чтение: живые сессии аккаунтов, разрез по статусам, активные
рассылки, флудвейт/спам-блок, дневной лимит/использовано, глобальный
toggle нейрочата. Источник — monitoring_stats + worker_manager +
config_service.
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from bot.config import OWNER_ID
from database.models import Account, Mailing, MailingStatus
from database.session import session_scope
from services.neurochat.config_service import get_global_config
from sqlalchemy import func, select
from utils.monitoring_stats import build_global_accounts_dashboard

router = Router()


def get_system_status_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="menu_status")],
            [InlineKeyboardButton(text="⬅️ В главное меню", callback_data="menu_back")],
        ]
    )


async def build_system_status_text() -> str:
    """Собирает текст пульса. Безопасно вызывать из команды и из меню."""
    from workers.manager import worker_manager

    async with session_scope() as session:
        dash = await build_global_accounts_dashboard(session)
        running = int(
            await session.scalar(
                select(func.count(Mailing.id)).where(
                    Mailing.status == MailingStatus.RUNNING
                )
            )
            or 0
        )
        paused = int(
            await session.scalar(
                select(func.count(Mailing.id)).where(
                    Mailing.status == MailingStatus.PAUSED
                )
            )
            or 0
        )
        daily_limit_sum = int(
            await session.scalar(select(func.coalesce(func.sum(Account.daily_limit), 0)))
            or 0
        )
        neuro_on = (await get_global_config(session)).enabled

    bs = dash["by_status"]
    total = dash["account_count"]
    workers = worker_manager.workers
    live = sum(1 for w in workers.values() if getattr(w, "is_connected", False))
    loaded = len(workers)
    cycle = "да" if worker_manager.is_running else "нет"

    return (
        "📊 <b>Статус системы</b>\n\n"
        f"👥 <b>Аккаунты:</b> {total} всего\n"
        f"  • 🟢 онлайн-сессии: {live}/{loaded}\n"
        f"  • active: {bs.get('active', 0)} · inactive: {bs.get('inactive', 0)}\n"
        f"  • 🟠 floodwait: {bs.get('flood_wait', 0)} · 🚫 спам-блок: {dash['spam_blocked']} "
        f"· 🔴 бан: {bs.get('banned', 0)} · ⚫ error: {bs.get('error', 0)}\n"
        f"  • прокси: {dash['with_proxy']} с / {dash['without_proxy']} без\n\n"
        f"📬 <b>Рассылки:</b> активных {running}, на паузе {paused}\n"
        f"  • цикл рассылки в процессе: {cycle}\n\n"
        f"📈 <b>Дневной лимит:</b> {dash['sum_today']} / {daily_limit_sum} использовано "
        f"(всего отправлено {dash['sum_sent']})\n\n"
        f"🧠 <b>Нейрочат (глобально):</b> {'ВКЛ ✅' if neuro_on else 'ВЫКЛ ⛔'}"
    )


@router.callback_query(F.data == "menu_status")
async def cb_menu_status(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    text = await build_system_status_text()
    try:
        await callback.message.edit_text(
            text,
            reply_markup=get_system_status_keyboard(),
            parse_mode=ParseMode.HTML,
        )
    except TelegramBadRequest as e:
        # «message is not modified» при обновлении без изменений — это норма.
        if "not modified" not in (e.message or str(e)).lower():
            raise
    await callback.answer("Обновлено")
