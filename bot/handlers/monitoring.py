"""
Мониторинг: статистика по группам аккаунтов и сводка по аккаунтам / ГЕО.
"""
from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.types import CallbackQuery

from bot.config import OWNER_ID
from bot.keyboards.main import (
    get_monitoring_accounts_keyboard,
    get_monitoring_keyboard,
    get_monitoring_stats_menu_keyboard,
    get_monitoring_stats_scope_keyboard,
    get_monitoring_warmup_keyboard,
)
from database.repositories import GroupRepository
from database.repositories import WarmupLogRepository
from database.session import session_scope
from utils.logger import log
from utils.monitoring_stats import build_global_accounts_dashboard, build_scope_stats
from utils.phone_geo import aggregate_geo_counts

router = Router()

_STATUS_LABELS = {
    "active": "Активные",
    "inactive": "Неактивные",
    "banned": "Забаненные",
    "flood_wait": "FloodWait",
    "error": "Ошибка сессии",
    "spam_blocked": "Статус «спам-блок»",
}


def _format_scope_stats_text(data: dict, group_title: str) -> str:
    if data.get("empty"):
        return (
            f"📊 <b>Статистика</b> — {group_title}\n\n"
            "В этой группе пока нет аккаунтов. Добавьте их в разделе «Аккаунты → Группы»."
        )

    lines = [f"📊 <b>Статистика</b>\n<i>{group_title}</i>\n"]
    b = data["by_status"]
    lines.append("<b>Аккаунты по статусам:</b>")
    for key, label in _STATUS_LABELS.items():
        lines.append(f"  • {label}: {b.get(key, 0)}")
    lines.append(f"\n<b>В выборке аккаунтов:</b> {data['account_count']}")
    lines.append(
        f"\n<b>Счётчики на аккаунтах</b> (всего отправлено / ошибки / сегодня):\n"
        f"  • Отправлено всего: {data['sum_sent']}\n"
        f"  • Ошибок (поле аккаунта): {data['sum_fail_acc']}\n"
        f"  • Сегодня: {data['sum_today']}"
    )
    lines.append(
        f"\n<b>Логи рассылок</b> по этим аккаунтам:\n"
        f"  • Успешных отправок: {data['mailing_log_ok']}\n"
        f"  • С ошибкой: {data['mailing_log_fail']}"
    )
    lines.append(
        f"\n<b>Прокси:</b> назначен {data['with_proxy']}, не назначен {data['without_proxy']}\n"
        f"  • Последняя проверка OK: {data['proxy_ok']}\n"
        f"  • Проверка не OK: {data['proxy_bad']}"
    )
    lines.append(f"\n<b>Флаг спам-блока</b> (is_spam_blocked): {data['spam_blocked']}")
    lines.append(
        f"\n<b>Клиенты в базе</b> (весь проект, не по группе):\n"
        f"  • Новые: {data['clients_new']}\n"
        f"  • Уже писали: {data['clients_contacted']}\n"
        f"  • Невалидные: {data['clients_invalid']}\n"
        f"  • Заблокировали бота: {data['clients_blocked']}"
    )
    rm = data["running_mailing"]
    if rm:
        lines.append(
            f"\n🚀 <b>Рассылка в работе:</b> #{rm.id}\n"
            f"   отправлено {rm.messages_sent}, ошибок {rm.messages_failed}"
        )
    else:
        lines.append("\n🚀 <b>Активной рассылки</b> (статус running) нет")
    return "\n".join(lines)


def _format_accounts_dashboard_text(data: dict) -> str:
    lines = ["👥 <b>Аккаунты — обзор</b>\n"]
    b = data["by_status"]
    lines.append("<b>По статусам:</b>")
    for key, label in _STATUS_LABELS.items():
        lines.append(f"  • {label}: {b.get(key, 0)}")
    lines.append(f"\n<b>Всего:</b> {data['account_count']}")
    lines.append(
        f"\n<b>Нагрузка:</b> всего отправлено {data['sum_sent']}, за сегодня {data['sum_today']}\n"
        f"<b>Прокси назначен:</b> {data['with_proxy']}, <b>без прокси:</b> {data['without_proxy']}\n"
        f"<b>Спам-блок (флаг):</b> {data['spam_blocked']}"
    )
    lines.append("\n<b>ГЕО по номеру телефона</b> (по коду страны, приблизительно):")
    geo = aggregate_geo_counts(data["phones"])
    if not geo:
        lines.append("  — нет номеров")
    else:
        for country, cnt in geo[:18]:
            lines.append(f"  • {country}: <b>{cnt}</b>")
        if len(geo) > 18:
            lines.append(f"  … ещё {len(geo) - 18} строк(и) регионов")
    lines.append(
        "\n<i>Уточнение: для одного кода (+1 и др.) страна условная; "
        "точнее — библиотека libphonenumber.</i>"
    )
    return "\n".join(lines)


@router.callback_query(F.data == "menu_monitoring")
async def cb_monitoring_menu(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await callback.message.edit_text(
        "📊 <b>Мониторинг</b>\n\n"
        "• <b>Статистика</b> — разрез по группам аккаунтов (или вся база): статусы, "
        "отправки, логи рассылок, прокси.\n"
        "• <b>Аккаунты</b> — сводка по всей сетке и распределение по ГЕО по номерам.\n\n"
        "Быстрый снимок: команда <code>/status</code>.",
        reply_markup=get_monitoring_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data == "monitoring_stats_menu")
async def cb_monitoring_stats_menu(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    try:
        async with session_scope() as session:
            groups = await GroupRepository.get_all(session)

        await callback.message.edit_text(
            "📊 <b>Статистика</b>\n\n"
            "Выберите группу или «Все аккаунты» для сводки по всей базе.",
            reply_markup=get_monitoring_stats_menu_keyboard(groups),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        log.error(f"monitoring_stats_menu: {e}")
        await callback.answer("Ошибка", show_alert=True)
        return

    await callback.answer()


@router.callback_query(F.data.startswith("monitoring_stats_g_"))
async def cb_monitoring_stats_scope(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    try:
        gid = int(callback.data.split("_")[-1])
    except ValueError:
        await callback.answer()
        return

    try:
        async with session_scope() as session:
            stats = await build_scope_stats(session, None if gid == 0 else gid)
            if gid == 0:
                title = "Все аккаунты (вся база)"
            else:
                g = await GroupRepository.get_by_id(session, gid)
                title = f"Группа «{g.name}»" if g else f"Группа id {gid}"

        text = _format_scope_stats_text(stats, title)
        if len(text) > 4000:
            text = text[:3997] + "…"

        await callback.message.edit_text(
            text,
            reply_markup=get_monitoring_stats_scope_keyboard(gid),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        log.error(f"monitoring_stats_scope: {e}")
        await callback.answer(f"Ошибка: {e}", show_alert=True)
        return

    await callback.answer()


@router.callback_query(F.data == "monitoring_accounts")
async def cb_monitoring_accounts(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    try:
        async with session_scope() as session:
            dash = await build_global_accounts_dashboard(session)
        text = _format_accounts_dashboard_text(dash)
        if len(text) > 4000:
            text = text[:3997] + "…"

        await callback.message.edit_text(
            text,
            reply_markup=get_monitoring_accounts_keyboard(),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        log.error(f"monitoring_accounts: {e}")
        await callback.answer(f"Ошибка: {e}", show_alert=True)
        return

    await callback.answer()


@router.callback_query(F.data == "monitoring_warmup")
async def cb_monitoring_warmup(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    try:
        async with session_scope() as session:
            s = await WarmupLogRepository.summary(session)
        text = (
            "🔥 <b>Прогрев аккаунтов</b>\n\n"
            f"• Включено аккаунтов: <b>{s['enabled']}</b>\n"
            f"• На паузе: <b>{s['paused']}</b>\n"
            f"• Действий за 24ч: <b>{s['actions_24h']}</b>\n\n"
            "Сейчас активен безопасный режим (умеренный профиль)."
        )
        await callback.message.edit_text(
            text,
            reply_markup=get_monitoring_warmup_keyboard(),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        log.error(f"monitoring_warmup: {e}")
        await callback.answer(f"Ошибка: {e}", show_alert=True)
        return
    await callback.answer()
