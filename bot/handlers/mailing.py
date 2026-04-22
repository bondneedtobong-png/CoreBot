"""
Хендлеры для управления рассылками.
Новая система: создание, список, настройки по модулям, запуск по клику.
"""
import asyncio
import html
import io
import json
import re
from datetime import datetime

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import update

from bot.config import (
    MAILING_BASE_UTC_OFFSET,
    OWNER_ID,
    mailing_timezone_label,
)
from bot.keyboards.main import (
    MAILING_LIST_PAGE_SIZE,
    get_mailing_keyboard,
    get_mailing_list_keyboard,
    get_mailing_view_keyboard,
    get_mailing_settings_keyboard,
    get_mailing_modules_keyboard,
    get_mailing_audience_keyboard,
    get_mailing_campaign_keyboard,
    get_mailing_security_keyboard,
    get_mailing_first_message_keyboard,
    get_mailing_tz_keyboard,
    get_mailing_target_group_keyboard,
    get_context_back_keyboard,
    mailing_target_group_button_label,
)
from database.session import session_scope
from bot.handlers.database.sheet_import_common import parse_usernames_from_txt
from database.models import Mailing, MailingStatus
from database.repositories import (
    ClientRepository,
    GroupRepository,
    InstanceSettingsRepository,
    MailingLogRepository,
    MailingRepository,
    MailingTestRecipientRepository,
)
from services.neurochat.stats_service import count_actions_by_mailing
from utils.logger import log

router = Router()

MAILING_TZ_ADJ_RE = re.compile(r"^mailing_tz_adj_(\d+)_(-?\d+)$")


async def _kbd_mailing_first_message(mailing_id: int, mailing: Mailing):
    """Клавиатура модуля «Первое сообщение» с актуальной меткой часового пояса."""
    extra = _extra_variants(mailing)
    async with session_scope() as session:
        eff = await InstanceSettingsRepository.get_effective_mailing_base_utc_offset(session)
    tz = mailing_timezone_label(eff)
    return get_mailing_first_message_keyboard(
        mailing_id,
        len(extra),
        variant_mode=(getattr(mailing, "variant_mode", None) or "random"),
        tz_short=tz,
    )


async def _render_mailing_timezone_menu(message: Message, mailing_id: int) -> None:
    async with session_scope() as session:
        eff = await InstanceSettingsRepository.get_effective_mailing_base_utc_offset(session)
        stored = await InstanceSettingsRepository.get_stored_mailing_base_utc_offset(session)
    src = (
        "<i>Сейчас используется значение, <b>сохранённое в боте</b>.</i>"
        if stored is not None
        else f"<i>В боте не задано — берётся из .env: <code>MAILING_BASE_UTC_OFFSET={MAILING_BASE_UTC_OFFSET}</code></i>"
    )
    lo = InstanceSettingsRepository._MAILING_TZ_MIN
    hi = InstanceSettingsRepository._MAILING_TZ_MAX
    txt = (
        "🕐 <b>Часовой пояс плейсхолдеров</b>\n\n"
        f"Активно: <b>{mailing_timezone_label(eff)}</b>\n"
        f"{src}\n\n"
        "Плейсхолдеры <code>{date}</code>, <code>{time}</code>, <code>{datetime}</code>, "
        "<code>{timezone}</code> и смещения вида <code>{time+2}</code> считаются от этого базового UTC-сдвига "
        f"(диапазон {lo}…{hi} ч).\n\n"
        "«Как в .env» — убрать сохранённое в боте и снова использовать только переменную окружения."
    )
    await message.edit_text(
        txt,
        reply_markup=get_mailing_tz_keyboard(mailing_id),
        parse_mode=ParseMode.HTML,
    )


def mailing_security_screen_html(mailing: Mailing) -> str:
    """Текст экрана «Настройка безопасности»: что делают переключатели и откуда берутся паузы."""
    name = html.escape(
        (mailing.name or f"Рассылка #{mailing.id}").strip() or f"#{mailing.id}"
    )
    dba = float(getattr(mailing, "delay_between_accounts", None) or 10.0)
    return (
        "🛡 <b>Настройка безопасности</b>\n\n"
        f"📋 {name}\n\n"
        "<b>Переключатели</b>\n"
        "• <b>Имитация набора</b> — перед отправкой клиенту показывается «печатает…», "
        "затем пауза <b>случайная от 5 до 10 секунд</b> (каждое сообщение — своё значение). "
        "Если выключено — сообщение уходит сразу.\n\n"
        "• <b>Умная задержка</b> — к паузам после каждой отправки и к паузе при смене аккаунта "
        "добавляется случайный разброс (~±30%) от заданного времени, чтобы интервалы не были одинаковыми.\n\n"
        "• <b>Автостоп</b> — кнопка «🕒» ниже: через сколько часов рассылка завершится сама; "
        "<code>0</code> — только ручная остановка.\n\n"
        f"<i>Пауза между аккаунтами</i> складывается из <code>{dba:g}</code> с (поле в данных рассылки) "
        "и «задержки между пакетами», которую задаёте кнопкой ниже.\n\n"
        "<b>Числовые параметры</b> — нажмите кнопку: перед вводом будет краткое пояснение.\n\n"
        "<b>Лимит «на аккаунт» и пауза</b> — после стольких-то <b>успешных первых сообщений</b> "
        "с одного аккаунта включается пауза рассылки (первое сообщение) на время из "
        "«Аудитория рассылки → Пауза аккаунта». В это время аккаунт по-прежнему может "
        "вести <b>нейрочат</b> по ответам. Суточный лимит сообщений аккаунта в БД "
        "(daily_limit) по-прежнему действует параллельно."
    )


def _format_mailing_detail_text(
    mailing: Mailing,
    account_stats: list[tuple[int, int, int, str]],
    neuro_actions: dict[str, int],
    *,
    accounts_hidden_from_stats: int = 0,
    new_clients_count: int = 0,
    mailing_is_running_here: bool = False,
) -> str:
    """Текст карточки рассылки: статус кампании, счётчики из БД, без разбивки по аккаунтам."""
    status_emoji = {
        "draft": "📝",
        "pending": "⏳",
        "running": "🚀",
        "paused": "⏸",
        "completed": "✅",
        "cancelled": "🛑",
        "error": "⚠️",
    }.get(mailing.status.value, "⚪")

    sent = int(getattr(mailing, "messages_sent", None) or 0)
    fail = int(getattr(mailing, "messages_failed", None) or 0)
    mpa = max(1, int(getattr(mailing, "messages_per_batch", None) or 10))
    auto_stop = getattr(mailing, "auto_stop_hours", None)
    stop_mode = f"{float(auto_stop):g} ч" if auto_stop and float(auto_stop) > 0 else "до ручной остановки"

    neuro_on = getattr(mailing, "neurochat_enabled", False)
    lines = [
        f"{status_emoji} <b>{mailing.name or f'Рассылка #{mailing.id}'}</b>",
        "",
        f"🆔 ID: {mailing.id}",
        f"📊 Статус: <code>{mailing.status.value}</code>",
        mailing_target_group_button_label(mailing),
        "",
        "📈 <b>За текущий запуск</b>",
        f"✅ Успешно: <b>{sent}</b>",
        f"❌ Ошибок: <b>{fail}</b>",
        "<i>При новом запуске счётчики обнуляются; сводки по клиентам — в «База данных».</i>",
        f"🆕 Клиентов NEW в очереди: <b>{new_clients_count}</b>",
    ]
    am = (getattr(mailing, "audience_mode", None) or "classes").strip().lower()
    am_lbl = {"test": "тест (txt)", "new": "из базы NEW", "classes": "по классам"}.get(am, am)
    mr = getattr(mailing, "max_recipients", None)
    lines.append(f"🎯 Режим аудитории: <b>{html.escape(am_lbl)}</b>")
    lines.append(
        f"🔢 Лимит успешных за запуск: <b>{int(mr)}</b>"
        if mr
        else "🔢 Лимит успешных за запуск: <i>нет</i>"
    )
    if mailing_is_running_here:
        lines.append("<i>▶️ Идёт отправка… Обновите «🔄 Обновить».</i>")
    if accounts_hidden_from_stats > 0:
        lines.append(
            f"<i>Скрыто аккаунтов с логами вне группы: {accounts_hidden_from_stats}</i>",
        )
    lines += [
        "",
        f"🕒 Режим остановки: <b>{stop_mode}</b>",
        f"🔮 Нейрочат: <b>{'вкл' if neuro_on else 'выкл'}</b> (после рассылки — ответы; ключ OpenRouter в боте или .env)",
        "🤖 Нейро-команды: "
        f"[SEND_LINK]={neuro_actions.get('SEND_LINK', 0)} · "
        f"[STOP]={neuro_actions.get('STOP', 0)} · "
        f"[ACCEPT]={neuro_actions.get('ACCEPT', 0)} · "
        f"[DECLINE]={neuro_actions.get('DECLINE', 0)} · "
        f"[HATER]={neuro_actions.get('HATER', 0)}",
        "",
        f"⏱ Задержка: {mailing.delay_between_messages} сек",
        f"⌨️ Имитация набора: {'✅ (5–10 с случайно)' if mailing.use_typing else '❌'}",
        "",
        f"<i>Смена аккаунта в этом запуске:</i> после <b>{mpa}</b> успешных подряд с одного аккаунта — следующий "
        "(«На аккаунт» в безопасности; лимит не накапливается между запусками).",
    ]
    return "\n".join(lines)


def _mailing_show_neuro_stop(mailing, show_stop: bool) -> bool:
    """Показать кнопку выключения нейрочата, когда рассылка не в процессе, а нейрочат включён."""
    return bool(getattr(mailing, "neurochat_enabled", False)) and not show_stop


async def _render_mailing_screen(callback: CallbackQuery, mailing_id: int) -> None:
    """Обновляет сообщение: актуальные данные из БД и кнопки Старт/Стоп."""
    from bot.main import safe_edit_message
    from workers.manager import worker_manager

    async with session_scope() as session:
        mailing = await MailingRepository.get_by_id(session, mailing_id)
        if not mailing:
            await callback.answer("Рассылка не найдена", show_alert=True)
            return
        account_stats = await MailingLogRepository.get_send_stats_by_account(session, mailing_id)
        neuro_actions = await count_actions_by_mailing(session, mailing_id)
        hidden = 0
        tg_id = getattr(mailing, "target_group_id", None)
        if tg_id:
            in_group = await GroupRepository.get_member_account_ids(session, tg_id)
            before = len(account_stats)
            account_stats = [s for s in account_stats if s[0] in in_group]
            hidden = before - len(account_stats)
        new_clients_count = await ClientRepository.count_new(session)

    show_stop = (
        mailing.status == MailingStatus.RUNNING
        and worker_manager.is_running
        and worker_manager.current_mailing_id == mailing_id
    )
    show_neuro_stop = _mailing_show_neuro_stop(mailing, show_stop)
    text = _format_mailing_detail_text(
        mailing,
        account_stats,
        neuro_actions,
        accounts_hidden_from_stats=hidden,
        new_clients_count=new_clients_count,
        mailing_is_running_here=show_stop,
    )
    kb = get_mailing_view_keyboard(
        mailing, show_stop=show_stop, show_neuro_stop=show_neuro_stop
    )
    await safe_edit_message(callback, text, kb, parse_mode=ParseMode.HTML)


class MailingCreateFSM(StatesGroup):
    """Состояния для создания рассылки."""
    waiting_for_name = State()
    waiting_for_suffix = State()


class MailingEditFSM(StatesGroup):
    """Состояния для редактирования настроек рассылки."""
    waiting_for_delay = State()
    waiting_for_batch_size = State()
    waiting_for_batch_delay = State()
    waiting_for_runtime_hours = State()
    waiting_for_text = State()
    waiting_for_variant_add = State()
    waiting_for_variant_edit = State()


class MailingAudienceFSM(StatesGroup):
    """Фильтр аудитории: списки include/exclude классов."""
    waiting_include = State()
    waiting_exclude = State()


class MailingCampaignFSM(StatesGroup):
    """Лимит рассылки, пауза аккаунта, тестовый txt."""
    waiting_max_recipients = State()
    waiting_cooldown_hours = State()
    waiting_test_txt = State()


def _extra_variants(mailing) -> list:
    try:
        raw = json.loads(mailing.message_variants_json or "[]")
        return raw if isinstance(raw, list) else []
    except Exception:
        return []


def _split_bulk_variants(raw: str) -> list[str]:
    """Разбивает ввод на варианты по строкам, удаляя ведущую нумерацию."""
    text = (raw or "").strip()
    if not text:
        return []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) <= 1:
        return [text]
    out: list[str] = []
    for ln in lines:
        cleaned = re.sub(r"^\s*\d+\s*[)\].:-]?\s*", "", ln).strip()
        out.append(cleaned or ln)
    return [x for x in out if x]


def mailing_has_launchable_text(mailing) -> bool:
    """Есть основной текст или непустые доп. варианты."""
    if mailing.message_text and str(mailing.message_text).strip():
        return True
    for x in _extra_variants(mailing):
        if str(x).strip():
            return True
    return False


# ==================== Главное меню рассылок ====================

@router.callback_query(F.data == "menu_mailing")
async def cb_mailing_menu(callback: CallbackQuery, state: FSMContext):
    """Главное меню раздела Рассылка."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await state.clear()
    await callback.message.edit_text(
        "📬 <b>Рассылка</b>\n\n"
        "Управление рассылками:\n"
        "• Создание новых рассылок\n"
        "• Просмотр и настройка\n"
        "• Запуск и мониторинг",
        reply_markup=get_mailing_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


# ==================== Создание рассылки ====================

@router.callback_query(F.data == "mailing_create")
async def cb_mailing_create(callback: CallbackQuery, state: FSMContext):
    """Начало создания рассылки."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await state.set_state(MailingCreateFSM.waiting_for_name)

    await callback.message.edit_text(
        "➕ <b>Создание рассылки</b>\n\n"
        "Введите <b>название рассылки</b>:\n"
        "(например: test1)\n\n"
        "<i>Назад — кнопка ниже.</i>",
        reply_markup=get_context_back_keyboard("menu_mailing"),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.message(MailingCreateFSM.waiting_for_name)
async def process_name(message: Message, state: FSMContext):
    """Обработка названия рассылки."""
    if message.from_user.id != OWNER_ID:
        return

    name = message.text.strip()
    await state.update_data(name=name)
    await state.set_state(MailingCreateFSM.waiting_for_suffix)

    await message.answer(
        "➕ <b>Создание рассылки</b>\n\n"
        "Введите <b>суффикс</b>:\n"
        "(например: usa)\n\n"
        "Итоговое имя: [{suffix}] {name}\n\n"
        "<i>Назад — кнопка ниже.</i>",
        reply_markup=get_context_back_keyboard("menu_mailing"),
        parse_mode=ParseMode.HTML,
    )


@router.message(MailingCreateFSM.waiting_for_suffix)
async def process_suffix(message: Message, state: FSMContext):
    """Обработка суффикса и создание рассылки."""
    if message.from_user.id != OWNER_ID:
        return

    suffix = message.text.strip()
    data = await state.get_data()
    name = data.get('name', 'unnamed')

    # Формируем итоговое имя
    final_name = f"[{suffix}] {name}"

    # Создаём рассылку в БД
    async with session_scope() as session:
        mailing = await MailingRepository.create(
            session=session,
            name=final_name,
            message_text="",
            delay_between_messages=10.0,
            delay_between_accounts=10.0,
        )

    await state.clear()

    log.info(f"Создана рассылка: {final_name} (ID={mailing.id})")

    await message.answer(
        f"✅ <b>Рассылка создана!</b>\n\n"
        f"📋 Название: {final_name}\n"
        f"🆔 ID: {mailing.id}\n\n"
        "Теперь вы можете настроить рассылку:",
        reply_markup=get_mailing_view_keyboard(
            mailing,
            show_stop=False,
            show_neuro_stop=_mailing_show_neuro_stop(mailing, False),
        ),
        parse_mode=ParseMode.HTML,
    )


def _mailing_list_page_from_data(data: str) -> int:
    if data == "mailing_list":
        return 0
    if data.startswith("mailing_list_p_"):
        return int(data.rsplit("_", 1)[-1])
    return 0


@router.callback_query(F.data == "mailing_list_page_info")
async def cb_mailing_list_page_info(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔", show_alert=True)
        return
    await callback.answer("Номер страницы · листайте ◀ ▶", show_alert=True)


# ==================== Список рассылок ====================

@router.callback_query(F.data == "mailing_list")
@router.callback_query(F.data.startswith("mailing_list_p_"))
async def cb_mailing_list(callback: CallbackQuery):
    """Показать список всех рассылок."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    page = _mailing_list_page_from_data(callback.data)

    async with session_scope() as session:
        from sqlalchemy import select
        result = await session.execute(
            select(Mailing).order_by(Mailing.created_at.desc())
        )
        mailings = list(result.scalars().all())

    if not mailings:
        await callback.message.answer(
            "📋 <b>Мои рассылки</b>\n\n"
            "У вас пока нет рассылок.\n"
            "Создайте первую рассылку!",
            reply_markup=get_context_back_keyboard("menu_mailing"),
            parse_mode=ParseMode.HTML,
        )
        await callback.answer()
        return

    total = len(mailings)
    total_pages = max(1, (total + MAILING_LIST_PAGE_SIZE - 1) // MAILING_LIST_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * MAILING_LIST_PAGE_SIZE + 1
    end = min((page + 1) * MAILING_LIST_PAGE_SIZE, total)

    await callback.message.answer(
        "📋 <b>Мои рассылки</b>\n\n"
        f"Всего: {total}\n"
        f"Страница {page + 1} из {total_pages} · строки {start}–{end}",
        reply_markup=get_mailing_list_keyboard(mailings, page=page),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


# ==================== Просмотр конкретной рассылки ====================

@router.callback_query(F.data.regexp(r"^mailing_view_\d+$"))
async def cb_mailing_view(callback: CallbackQuery):
    """Открыть карточку рассылки (редактирует текущее сообщение, без дублей)."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    mailing_id = int(callback.data.split("_")[-1])
    await _render_mailing_screen(callback, mailing_id)
    await callback.answer()


@router.callback_query(F.data.regexp(r"^mailing_refresh_\d+$"))
async def cb_mailing_refresh(callback: CallbackQuery):
    """Обновить статистику и кнопки с карточки рассылки."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    mailing_id = int(callback.data.split("_")[-1])
    await _render_mailing_screen(callback, mailing_id)
    await callback.answer("Обновлено")


@router.callback_query(F.data.regexp(r"^mailing_stop_\d+$"))
async def cb_mailing_stop(callback: CallbackQuery):
    """Остановить текущую рассылку (воркер в фоне)."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    from workers.manager import worker_manager

    mailing_id = int(callback.data.split("_")[-1])
    if not worker_manager.is_running or worker_manager.current_mailing_id != mailing_id:
        await callback.answer("Эта рассылка сейчас не выполняется", show_alert=True)
        await _render_mailing_screen(callback, mailing_id)
        return

    worker_manager.stop_mailing()
    await callback.answer("Остановка запрошена…")
    await _render_mailing_screen(callback, mailing_id)


@router.callback_query(F.data.regexp(r"^mailing_neuro_stop_view_\d+$"))
async def cb_mailing_neuro_stop_from_view(callback: CallbackQuery):
    """Выключить нейрочат с карточки рассылки (после завершения кампании и т.п.)."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    mailing_id = int(callback.data.split("_")[-1])
    async with session_scope() as session:
        mailing = await MailingRepository.get_by_id(session, mailing_id)
        if not mailing:
            await callback.answer("Рассылка не найдена", show_alert=True)
            return
        await MailingRepository.update_neuro(session, mailing_id, neurochat_enabled=False)

    await callback.answer("Нейрочат выключен")
    await _render_mailing_screen(callback, mailing_id)


# ==================== Настройки рассылки ====================

@router.callback_query(F.data.startswith("mailing_settings_"))
async def cb_mailing_settings(callback: CallbackQuery, state: FSMContext):
    """Настройки рассылки."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await state.clear()
    mailing_id = int(callback.data.split("_")[-1])

    async with session_scope() as session:
        mailing = await MailingRepository.get_by_id(session, mailing_id)

    if not mailing:
        await callback.message.answer("❌ Рассылка не найдена.")
        await callback.answer()
        return

    text = (
        f"⚙️ <b>Настройки рассылки</b>\n\n"
        f"📋 {mailing.name or f'Рассылка #{mailing.id}'}\n\n"
        "Выберите модуль:"
    )

    await callback.message.edit_text(
        text,
        reply_markup=get_mailing_modules_keyboard(mailing),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


# ==================== Модули настроек ====================

@router.callback_query(F.data.startswith("mailing_mod_security_"))
async def cb_mailing_mod_security(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await state.clear()
    mailing_id = int(callback.data.split("_")[-1])
    async with session_scope() as session:
        mailing = await MailingRepository.get_by_id(session, mailing_id)
    if not mailing:
        await callback.answer("Не найдено", show_alert=True)
        return

    await callback.message.edit_text(
        mailing_security_screen_html(mailing),
        reply_markup=get_mailing_security_keyboard(mailing),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


def _mailing_aud_back_kb(mailing_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ К аудитории",
                    callback_data=f"mailing_campaign_aud_{mailing_id}",
                )
            ]
        ]
    )


def _mailing_campaign_flow_back_kb(mailing_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ К настройкам аудитории",
                    callback_data=f"mailing_campaign_aud_{mailing_id}",
                )
            ]
        ]
    )


async def _render_mailing_audience_screen(callback: CallbackQuery, mailing_id: int) -> None:
    async with session_scope() as session:
        mailing = await MailingRepository.get_by_id(session, mailing_id)
    if not mailing:
        await callback.answer("Не найдено", show_alert=True)
        return
    aud = ClientRepository.parse_mailing_audience(mailing)
    st = aud["client_status"]
    lbl = "только NEW" if st == "new" else "NEW+CONTACTED"
    raw_js = json.dumps(aud, ensure_ascii=False, indent=2)
    text = (
        "🎯 <b>Аудитория рассылки</b>\n\n"
        f"<pre>{html.escape(raw_js)}</pre>\n\n"
        "• Переключатель — <code>client_status</code> (new ↔ open).\n"
        "• Include — нужны все перечисленные классы с count&gt;0 (пусто — без фильтра include).\n"
        "• Exclude — отбрасываем клиентов с count&gt;0 по любому из классов.\n"
        "• Сброс — дефолт: new + exclude <code>bl</code>."
    )
    await callback.message.edit_text(
        text,
        reply_markup=get_mailing_audience_keyboard(mailing_id, lbl),
        parse_mode=ParseMode.HTML,
    )


MAILING_AUD_MODE_RE = re.compile(r"^mailing_aud_mode_(test|new|classes)_(\d+)$")


async def _render_mailing_campaign_screen(callback: CallbackQuery, mailing_id: int) -> None:
    async with session_scope() as session:
        mailing = await MailingRepository.get_by_id(session, mailing_id)
        if not mailing:
            await callback.answer("Не найдено", show_alert=True)
            return
        nq = await ClientRepository.count_mailing_queue(session, mailing)
        n_test = await MailingTestRecipientRepository.count_for_mailing(session, mailing_id)
    mode = (getattr(mailing, "audience_mode", None) or "classes").strip().lower()
    mode_lbl = {"test": "Тест (txt)", "new": "Из базы NEW", "classes": "По классам"}.get(
        mode, mode
    )
    cap = getattr(mailing, "max_recipients", None)
    cap_txt = f"<b>{int(cap)}</b>" if cap else "<i>без лимита</i>"
    cd = float(getattr(mailing, "mailing_cooldown_hours", None) or 12.0)
    mpa = max(1, int(getattr(mailing, "messages_per_batch", None) or 10))
    text = (
        "🎯 <b>Аудитория рассылки</b>\n\n"
        f"Режим: <b>{html.escape(mode_lbl)}</b>\n"
        f"В очереди сейчас: <b>{nq}</b> клиентов\n"
        f"Тестовых записей в списке: <b>{n_test}</b>\n"
        f"Лимит успешных первых сообщений за запуск: {cap_txt}\n"
        f"Пауза <b>рассылки (первое сообщение)</b> для аккаунта после "
        f"<code>{mpa}</code> успешных: <b>{cd:g}</b> ч\n\n"
        "<i>Пока аккаунты в паузе рассылки, нейрочат этой кампании отвечает на входящие.</i>"
    )
    await callback.message.edit_text(
        text,
        reply_markup=get_mailing_campaign_keyboard(mailing),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data.startswith("mailing_campaign_aud_"))
async def cb_mailing_campaign_aud(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    mailing_id = int(callback.data.split("_")[-1])
    await _render_mailing_campaign_screen(callback, mailing_id)
    await callback.answer()


@router.callback_query(F.data.regexp(MAILING_AUD_MODE_RE))
async def cb_mailing_aud_mode_set(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔", show_alert=True)
        return
    await state.clear()
    m = MAILING_AUD_MODE_RE.match(callback.data or "")
    if not m:
        await callback.answer()
        return
    mode, mailing_id = m.group(1), int(m.group(2))
    async with session_scope() as session:
        await session.execute(
            update(Mailing)
            .where(Mailing.id == mailing_id)
            .values(audience_mode=mode, updated_at=datetime.utcnow())
        )
        await session.commit()
    await callback.answer(f"Режим: {mode}")
    await _render_mailing_campaign_screen(callback, mailing_id)


@router.callback_query(F.data.startswith("mailing_cap_edit_"))
async def cb_mailing_cap_edit(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔", show_alert=True)
        return
    mailing_id = int(callback.data.split("_")[-1])
    await state.set_state(MailingCampaignFSM.waiting_max_recipients)
    await state.update_data(mailing_cap_id=mailing_id)
    await callback.message.answer(
        "🔢 <b>Лимит успешных первых сообщений</b>\n\n"
        "Целое число (например <code>1000</code>) или <code>0</code> / слово "
        "<code>сброс</code> — без лимита.\n\n"
        "<i>Назад — кнопка ниже (ввод отменится).</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=_mailing_campaign_flow_back_kb(mailing_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("mailing_cd_edit_"))
async def cb_mailing_cd_edit(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔", show_alert=True)
        return
    mailing_id = int(callback.data.split("_")[-1])
    await state.set_state(MailingCampaignFSM.waiting_cooldown_hours)
    await state.update_data(mailing_cd_id=mailing_id)
    await callback.message.answer(
        "⏳ <b>Пауза рассылки (часы)</b>\n\n"
        "После пакета успешных первых сообщений с одного аккаунта (см. «На аккаунт» "
        "в безопасности) аккаунт не шлёт первое сообщение указанное время — "
        "но может вести нейрочат.\n\n"
        "Число часов, например <code>12</code> или <code>5.5</code>.\n\n"
        "<i>Назад — кнопка ниже (ввод отменится).</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=_mailing_campaign_flow_back_kb(mailing_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("mailing_test_txt_"))
async def cb_mailing_test_txt(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔", show_alert=True)
        return
    mailing_id = int(callback.data.split("_")[-1])
    await state.set_state(MailingCampaignFSM.waiting_test_txt)
    await state.update_data(mailing_test_id=mailing_id)
    await callback.message.answer(
        "📎 Пришлите <b>.txt</b> со списком @username (как в импорте листов).\n"
        "Список заменит предыдущий тестовый набор для этой рассылки.\n\n"
        "<i>Назад — кнопка ниже (загрузка отменится).</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=_mailing_campaign_flow_back_kb(mailing_id),
    )
    await callback.answer()


@router.message(MailingCampaignFSM.waiting_max_recipients, F.text)
async def cb_mailing_cap_save(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return
    data = await state.get_data()
    mailing_id = data.get("mailing_cap_id")
    await state.clear()
    if not mailing_id:
        await message.answer("Сессия устарела.")
        return
    val: int | None
    if raw in ("", "0", "сброс", "none", "нет"):
        val = None
    else:
        try:
            val = int(raw)
            if val < 1:
                val = None
        except ValueError:
            await message.answer("Нужно целое число или сброс.")
            return
    async with session_scope() as session:
        await session.execute(
            update(Mailing)
            .where(Mailing.id == mailing_id)
            .values(max_recipients=val, updated_at=datetime.utcnow())
        )
        await session.commit()
    await message.answer(
        f"✅ Лимит: <code>{val}</code>" if val else "✅ Лимит снят.",
        parse_mode=ParseMode.HTML,
        reply_markup=_mailing_campaign_flow_back_kb(mailing_id),
    )


@router.message(MailingCampaignFSM.waiting_cooldown_hours, F.text)
async def cb_mailing_cd_save(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return
    data = await state.get_data()
    mailing_id = data.get("mailing_cd_id")
    await state.clear()
    if not mailing_id:
        await message.answer("Сессия устарела.")
        return
    try:
        hours = float((message.text or "").strip().replace(",", "."))
        if hours < 0.1 or hours > 168:
            raise ValueError("range")
    except ValueError:
        await message.answer("Нужно число от 0.1 до 168 (часов).")
        return
    async with session_scope() as session:
        await session.execute(
            update(Mailing)
            .where(Mailing.id == mailing_id)
            .values(mailing_cooldown_hours=hours, updated_at=datetime.utcnow())
        )
        await session.commit()
    await message.answer(
        f"✅ Пауза рассылки: <b>{hours:g}</b> ч",
        parse_mode=ParseMode.HTML,
        reply_markup=_mailing_campaign_flow_back_kb(mailing_id),
    )


@router.message(MailingCampaignFSM.waiting_test_txt, F.document)
async def cb_mailing_test_txt_save(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return
    data = await state.get_data()
    mailing_id = data.get("mailing_test_id")
    if not mailing_id:
        await state.clear()
        return
    doc = message.document
    if not doc or not (doc.file_name or "").lower().endswith(".txt"):
        await message.answer("Нужен файл .txt")
        return
    buf = io.BytesIO()
    await message.bot.download(doc, destination=buf)
    raw = buf.getvalue().decode("utf-8", errors="replace")
    usernames = parse_usernames_from_txt(raw)
    async with session_scope() as session:
        added, dups = await MailingTestRecipientRepository.replace_from_usernames(
            session, mailing_id, sorted(usernames)
        )
    await state.clear()
    await message.answer(
        f"✅ Тестовый список: <b>{added}</b> уникальных, дубликатов строк: <b>{dups}</b>.\n"
        "Переключите режим аудитории на «Тест», если ещё не.",
        parse_mode=ParseMode.HTML,
        reply_markup=_mailing_campaign_flow_back_kb(mailing_id),
    )


@router.callback_query(F.data.startswith("mailing_aud_menu_"))
async def cb_mailing_aud_menu(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    mailing_id = int(callback.data.split("_")[-1])
    await _render_mailing_audience_screen(callback, mailing_id)
    await callback.answer()


@router.callback_query(F.data.startswith("mailing_aud_toggle_"))
async def cb_mailing_aud_toggle(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    mailing_id = int(callback.data.split("_")[-1])
    async with session_scope() as session:
        mailing = await MailingRepository.get_by_id(session, mailing_id)
        if not mailing:
            await callback.answer("Не найдено", show_alert=True)
            return
        aud = ClientRepository.parse_mailing_audience(mailing)
        aud["client_status"] = "open" if aud["client_status"] == "new" else "new"
        await session.execute(
            update(Mailing).where(Mailing.id == mailing_id).values(
                audience_filter_json=json.dumps(aud, ensure_ascii=False),
                updated_at=datetime.utcnow(),
            )
        )
        await session.commit()
    await callback.answer("Сохранено")
    await _render_mailing_audience_screen(callback, mailing_id)


@router.callback_query(F.data.startswith("mailing_aud_reset_"))
async def cb_mailing_aud_reset(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    mailing_id = int(callback.data.split("_")[-1])
    async with session_scope() as session:
        await session.execute(
            update(Mailing).where(Mailing.id == mailing_id).values(
                audience_filter_json=None,
                updated_at=datetime.utcnow(),
            )
        )
        await session.commit()
    await callback.answer("Сброшено")
    await _render_mailing_audience_screen(callback, mailing_id)


@router.callback_query(F.data.startswith("mailing_aud_inc_"))
async def cb_mailing_aud_inc_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    mailing_id = int(callback.data.split("_")[-1])
    await state.set_state(MailingAudienceFSM.waiting_include)
    await state.update_data(mailing_aud_id=mailing_id)
    await callback.message.answer(
        "✏️ <b>Include классы</b>\n\n"
        "Список через запятую (например: <code>pulse,ru</code>).\n"
        "Пустое сообщение — очистить include.\n\n"
        "<i>Назад — кнопка ниже.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=_mailing_aud_back_kb(mailing_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("mailing_aud_exc_"))
async def cb_mailing_aud_exc_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    mailing_id = int(callback.data.split("_")[-1])
    await state.set_state(MailingAudienceFSM.waiting_exclude)
    await state.update_data(mailing_aud_id=mailing_id)
    await callback.message.answer(
        "✏️ <b>Exclude классы</b>\n\n"
        "Список через запятую (например: <code>bl,spam</code>).\n"
        "Пустое сообщение — очистить exclude (никого не отсекаем по классам).\n\n"
        "<i>Назад — кнопка ниже.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=_mailing_aud_back_kb(mailing_id),
    )
    await callback.answer()


@router.message(MailingAudienceFSM.waiting_include, F.text)
async def cb_mailing_aud_inc_save(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return
    raw = (message.text or "").strip()
    inc = [x.strip().lower() for x in raw.split(",") if x.strip()] if raw else []
    data = await state.get_data()
    mailing_id = data.get("mailing_aud_id")
    await state.clear()
    if not mailing_id:
        await message.answer("Сессия устарела.")
        return
    async with session_scope() as session:
        mailing = await MailingRepository.get_by_id(session, mailing_id)
        if not mailing:
            await message.answer("Рассылка не найдена.")
            return
        aud = ClientRepository.parse_mailing_audience(mailing)
        aud["include_classes"] = inc
        await session.execute(
            update(Mailing).where(Mailing.id == mailing_id).values(
                audience_filter_json=json.dumps(aud, ensure_ascii=False),
                updated_at=datetime.utcnow(),
            )
        )
        await session.commit()
    await message.answer(
        f"✅ include сохранён: <code>{','.join(inc) or '—'}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=_mailing_aud_back_kb(mailing_id),
    )


@router.message(MailingAudienceFSM.waiting_exclude, F.text)
async def cb_mailing_aud_exc_save(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return
    raw = (message.text or "").strip()
    exc = [x.strip().lower() for x in raw.split(",") if x.strip()] if raw else []
    data = await state.get_data()
    mailing_id = data.get("mailing_aud_id")
    await state.clear()
    if not mailing_id:
        await message.answer("Сессия устарела.")
        return
    async with session_scope() as session:
        mailing = await MailingRepository.get_by_id(session, mailing_id)
        if not mailing:
            await message.answer("Рассылка не найдена.")
            return
        aud = ClientRepository.parse_mailing_audience(mailing)
        aud["exclude_classes"] = exc
        await session.execute(
            update(Mailing).where(Mailing.id == mailing_id).values(
                audience_filter_json=json.dumps(aud, ensure_ascii=False),
                updated_at=datetime.utcnow(),
            )
        )
        await session.commit()
    await message.answer(
        f"✅ exclude сохранён: <code>{','.join(exc) or '—'}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=_mailing_aud_back_kb(mailing_id),
    )


@router.callback_query(F.data.startswith("mailing_mod_first_"))
async def cb_mailing_mod_first(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await state.clear()
    mailing_id = int(callback.data.split("_")[-1])
    async with session_scope() as session:
        mailing = await MailingRepository.get_by_id(session, mailing_id)
        eff_h = await InstanceSettingsRepository.get_effective_mailing_base_utc_offset(session)
        stored_h = await InstanceSettingsRepository.get_stored_mailing_base_utc_offset(session)
    if not mailing:
        await callback.answer("Не найдено", show_alert=True)
        return

    extra = _extra_variants(mailing)
    mode = (getattr(mailing, "variant_mode", None) or "random").strip().lower()
    mode_lbl = "Случайно" if mode == "random" else "По очереди"
    preview_main = (mailing.message_text or "")[:400]
    if len(mailing.message_text or "") > 400:
        preview_main += "…"

    lines = [
        "✉️ <b>Первое сообщение</b>\n",
        f"📋 {mailing.name or mailing_id}\n",
        "",
        "<b>Основной текст</b> (один из вариантов при отправке):",
        f"<pre>{preview_main or '— пусто —'}</pre>",
        "",
        f"<b>Дополнительных вариантов:</b> {len(extra)}",
        f"<b>Перебор вариантов:</b> {mode_lbl}",
        "",
        f"🕐 База для <code>{{date}}</code>/<code>{{time}}</code>/<code>{{timezone}}</code>: "
        f"<b>{mailing_timezone_label(eff_h)}</b>",
        (
            "<i>Источник: значение в боте (ниже).</i>"
            if stored_h is not None
            else f"<i>Источник: .env <code>MAILING_BASE_UTC_OFFSET={MAILING_BASE_UTC_OFFSET}</code> "
            "(можно задать в боте — кнопка ниже).</i>"
        ),
        "",
        "Плейсхолдеры в тексте:",
        "<code>{username}</code> @получателя · <code>{date}</code> <code>{time}</code> <code>{datetime}</code> <code>{timezone}</code>",
        "<code>{date+3}</code> <code>{time-2}</code> <code>{datetime+1}</code> <code>{timezone-1}</code> — сдвиг от базового UTC-часового пояса",
        "<code>{firstname}</code> <code>{lastname}</code> — аккаунт-отправитель · <code>{phone}</code> <code>{account_id}</code>",
        "<code>{mailing}</code> — название кампании · <code>{link}</code> — ссылка сообщества",
        "<code>{random4}</code> <code>{random6}</code> — случайные числа",
        "",
        (
            "При отправке выбирается <b>случайный</b> непустой вариант: "
            "основной текст + список ниже."
            if mode == "random"
            else "При отправке варианты идут <b>по очереди</b>: "
            "основной текст + список ниже, затем снова с начала."
        ),
    ]

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=await _kbd_mailing_first_message(mailing_id, mailing),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("mailing_tz_menu_"))
async def cb_mailing_tz_menu(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    mailing_id = int(callback.data.split("_")[-1])
    await _render_mailing_timezone_menu(callback.message, mailing_id)
    await callback.answer()


@router.callback_query(lambda c: bool(c.data and MAILING_TZ_ADJ_RE.match(c.data)))
async def cb_mailing_tz_adj(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    m = MAILING_TZ_ADJ_RE.match(callback.data or "")
    if not m:
        return
    mailing_id = int(m.group(1))
    delta = int(m.group(2))
    async with session_scope() as session:
        cur = await InstanceSettingsRepository.get_effective_mailing_base_utc_offset(session)
        new_h = InstanceSettingsRepository.clamp_mailing_base_utc_offset(cur + delta)
        await InstanceSettingsRepository.set_mailing_base_utc_offset(session, new_h)
    await callback.answer(f"→ {mailing_timezone_label(new_h)}", show_alert=False)
    await _render_mailing_timezone_menu(callback.message, mailing_id)


@router.callback_query(F.data.startswith("mailing_tz_reset_"))
async def cb_mailing_tz_reset(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    mailing_id = int(callback.data.split("_")[-1])
    async with session_scope() as session:
        await InstanceSettingsRepository.clear_mailing_base_utc_offset(session)
    await callback.answer("Сброшено к .env", show_alert=True)
    await _render_mailing_timezone_menu(callback.message, mailing_id)


@router.callback_query(F.data.startswith("mailing_pick_group_"))
async def cb_mailing_pick_group(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    mailing_id = int(callback.data.split("_")[-1])
    async with session_scope() as session:
        groups = await GroupRepository.get_all(session)

    await callback.message.edit_text(
        "👥 <b>Целевая группа аккаунтов</b>\n\n"
        "Рассылка пойдёт только с аккаунтов из выбранной группы "
        "(раздел «Аккаунты → Группы»). «Все аккаунты» — без фильтра по группе.",
        reply_markup=get_mailing_target_group_keyboard(
            mailing_id, groups, back_callback=f"mailing_view_{mailing_id}"
        ),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("mailing_target_set_"))
async def cb_mailing_target_set(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    parts = callback.data.split("_")
    mailing_id = int(parts[3])
    gid = int(parts[4])
    val = None if gid == 0 else gid

    async with session_scope() as session:
        await session.execute(
            update(Mailing).where(Mailing.id == mailing_id).values(target_group_id=val)
        )
        await session.commit()
        mailing = await MailingRepository.get_by_id(session, mailing_id)

    if not mailing:
        await callback.answer("Ошибка", show_alert=True)
        return

    await callback.answer("Сохранено")
    await _render_mailing_screen(callback, mailing_id)


@router.callback_query(F.data.startswith("mailing_variant_add_"))
async def cb_mailing_variant_add(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    mailing_id = int(callback.data.split("_")[-1])
    await state.update_data(mailing_id=mailing_id)
    await state.set_state(MailingEditFSM.waiting_for_variant_add)

    await callback.message.edit_text(
        "➕ <b>Новый вариант текста</b>\n\n"
        "Можно добавить один вариант или сразу несколько.\n\n"
        "Если нужно массово — отправьте каждый вариант с новой строки, например:\n"
        "<code>1Variant one</code>\n"
        "<code>2The second variant</code>\n"
        "<code>3Another one</code>\n\n"
        "Цифры в начале будут убраны. Если число не указано — строка сохранится как есть.",
        reply_markup=get_context_back_keyboard(f"mailing_mod_first_{mailing_id}"),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.message(MailingEditFSM.waiting_for_variant_add)
async def process_variant_add(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return

    text = (message.text or "").strip()
    variants = _split_bulk_variants(text)
    if not variants:
        await message.answer("❌ Пустой текст — отправьте ещё раз.")
        return

    data = await state.get_data()
    mailing_id = data.get("mailing_id")

    async with session_scope() as session:
        mailing = await MailingRepository.get_by_id(session, mailing_id)
        if not mailing:
            await state.clear()
            await message.answer("❌ Рассылка не найдена.")
            return

        extra = _extra_variants(mailing)
        extra.extend(variants)
        await session.execute(
            update(Mailing)
            .where(Mailing.id == mailing_id)
            .values(message_variants_json=json.dumps(extra, ensure_ascii=False))
        )
        await session.commit()
        mailing = await MailingRepository.get_by_id(session, mailing_id)

    await state.clear()
    extra = _extra_variants(mailing)
    await message.answer(
        f"✅ Добавлено вариантов: <b>{len(variants)}</b>. Всего дополнительных: <b>{len(extra)}</b>.",
        reply_markup=await _kbd_mailing_first_message(mailing_id, mailing),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data.startswith("mailing_variant_mode_toggle_"))
async def cb_mailing_variant_mode_toggle(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    mailing_id = int(callback.data.split("_")[-1])
    async with session_scope() as session:
        mailing = await MailingRepository.get_by_id(session, mailing_id)
        if not mailing:
            await callback.answer("Не найдено", show_alert=True)
            return
        old_mode = (getattr(mailing, "variant_mode", None) or "random").strip().lower()
        new_mode = "sequential" if old_mode == "random" else "random"
        await session.execute(
            update(Mailing).where(Mailing.id == mailing_id).values(variant_mode=new_mode)
        )
        await session.commit()
        mailing = await MailingRepository.get_by_id(session, mailing_id)

    await callback.answer("Режим сохранён")
    extra = _extra_variants(mailing)
    mode_lbl = "Случайно" if new_mode == "random" else "По очереди"
    preview_main = (mailing.message_text or "")[:400]
    if len(mailing.message_text or "") > 400:
        preview_main += "…"
    lines = [
        "✉️ <b>Первое сообщение</b>\n",
        f"📋 {mailing.name or mailing_id}\n",
        "",
        "<b>Основной текст</b> (один из вариантов при отправке):",
        f"<pre>{preview_main or '— пусто —'}</pre>",
        "",
        f"<b>Дополнительных вариантов:</b> {len(extra)}",
        f"<b>Перебор вариантов:</b> {mode_lbl}",
    ]
    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=await _kbd_mailing_first_message(mailing_id, mailing),
    )


@router.callback_query(F.data.startswith("mailing_variants_"))
async def cb_mailing_variants(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    mailing_id = int(callback.data.split("_")[-1])
    async with session_scope() as session:
        mailing = await MailingRepository.get_by_id(session, mailing_id)
    if not mailing:
        await callback.answer("Не найдено", show_alert=True)
        return
    extra = _extra_variants(mailing)
    rows: list[list[InlineKeyboardButton]] = []
    if extra:
        for i, txt in enumerate(extra[:60]):
            preview = (txt or "").strip().replace("\n", " ")
            if len(preview) > 42:
                preview = preview[:39] + "…"
            rows.append([
                InlineKeyboardButton(
                    text=f"{i + 1}. {preview or '— пусто —'}",
                    callback_data=f"mailing_variant_open_{mailing_id}_{i}",
                )
            ])
    else:
        rows.append([InlineKeyboardButton(text="📭 Нет вариантов", callback_data="mailing_variant_noop")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"mailing_mod_first_{mailing_id}")])
    await callback.message.edit_text(
        f"📚 <b>Список вариантов</b>\n\nВсего: <b>{len(extra)}</b>\n"
        "Нажмите на вариант, чтобы редактировать или удалить.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data == "mailing_variant_noop")
async def cb_mailing_variant_noop(callback: CallbackQuery):
    await callback.answer("Добавьте варианты через кнопку «➕ Добавить вариант».", show_alert=True)


@router.callback_query(F.data.regexp(r"^mailing_variant_open_\d+_\d+$"))
async def cb_mailing_variant_open(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    _, _, _, mailing_id_s, idx_s = callback.data.split("_")
    mailing_id = int(mailing_id_s)
    idx = int(idx_s)
    async with session_scope() as session:
        mailing = await MailingRepository.get_by_id(session, mailing_id)
    if not mailing:
        await callback.answer("Не найдено", show_alert=True)
        return
    extra = _extra_variants(mailing)
    if idx < 0 or idx >= len(extra):
        await callback.answer("Вариант не найден", show_alert=True)
        return
    text = (extra[idx] or "").strip()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"mailing_variant_edit_{mailing_id}_{idx}")],
            [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"mailing_variant_rm_{mailing_id}_{idx}")],
            [InlineKeyboardButton(text="⬅️ К списку вариантов", callback_data=f"mailing_variants_{mailing_id}")],
        ]
    )
    await callback.message.edit_text(
        f"✉️ <b>Вариант #{idx + 1}</b>\n\n<pre>{html.escape(text or '— пусто —')}</pre>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^mailing_variant_edit_\d+_\d+$"))
async def cb_mailing_variant_edit(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    _, _, _, mailing_id_s, idx_s = callback.data.split("_")
    mailing_id = int(mailing_id_s)
    idx = int(idx_s)
    await state.update_data(mailing_id=mailing_id, variant_idx=idx)
    await state.set_state(MailingEditFSM.waiting_for_variant_edit)
    await callback.message.edit_text(
        f"✏️ <b>Редактирование варианта #{idx + 1}</b>\n\n"
        "Отправьте новый текст этого варианта.",
        parse_mode=ParseMode.HTML,
        reply_markup=get_context_back_keyboard(f"mailing_variant_open_{mailing_id}_{idx}"),
    )
    await callback.answer()


@router.message(MailingEditFSM.waiting_for_variant_edit)
async def process_variant_edit(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("❌ Пустой текст — отправьте ещё раз.")
        return
    data = await state.get_data()
    mailing_id = int(data.get("mailing_id"))
    idx = int(data.get("variant_idx"))
    async with session_scope() as session:
        mailing = await MailingRepository.get_by_id(session, mailing_id)
        if not mailing:
            await state.clear()
            await message.answer("❌ Рассылка не найдена.")
            return
        extra = _extra_variants(mailing)
        if idx < 0 or idx >= len(extra):
            await state.clear()
            await message.answer("❌ Вариант не найден.")
            return
        extra[idx] = text
        await session.execute(
            update(Mailing).where(Mailing.id == mailing_id).values(
                message_variants_json=json.dumps(extra, ensure_ascii=False)
            )
        )
        await session.commit()
    await state.clear()
    await message.answer(
        "✅ Вариант обновлён.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📚 К списку вариантов", callback_data=f"mailing_variants_{mailing_id}")],
                [InlineKeyboardButton(text="⬅️ К модулю", callback_data=f"mailing_mod_first_{mailing_id}")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("mailing_variant_rm_"))
async def cb_mailing_variant_rm(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    parts = callback.data.split("_")
    mailing_id = int(parts[3])
    idx = int(parts[4])

    async with session_scope() as session:
        mailing = await MailingRepository.get_by_id(session, mailing_id)
        if not mailing:
            await callback.answer("Не найдено", show_alert=True)
            return
        extra = _extra_variants(mailing)
        if 0 <= idx < len(extra):
            extra.pop(idx)
            await session.execute(
                update(Mailing)
                .where(Mailing.id == mailing_id)
                .values(message_variants_json=json.dumps(extra, ensure_ascii=False))
            )
            await session.commit()
        mailing = await MailingRepository.get_by_id(session, mailing_id)

    await callback.answer("Удалено")
    await cb_mailing_variants(callback)


# ==================== Переключатели настроек ====================

@router.callback_query(F.data.startswith("mailing_toggle_typing_"))
async def cb_toggle_typing(callback: CallbackQuery):
    """Переключение имитации набора текста."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    mailing_id = int(callback.data.split("_")[-1])

    async with session_scope() as session:
        mailing = await MailingRepository.get_by_id(session, mailing_id)
        if mailing:
            new_value = not mailing.use_typing
            await session.execute(
                update(Mailing)
                .where(Mailing.id == mailing_id)
                .values(use_typing=new_value)
            )
            await session.commit()

            log.info(f"Рассылка {mailing_id}: use_typing = {new_value}")

            mailing.use_typing = new_value
            await callback.message.edit_text(
                mailing_security_screen_html(mailing),
                reply_markup=get_mailing_security_keyboard(mailing),
                parse_mode=ParseMode.HTML,
            )

    await callback.answer()


@router.callback_query(F.data.startswith("mailing_toggle_smart_"))
async def cb_toggle_smart(callback: CallbackQuery):
    """Переключение умной задержки."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    mailing_id = int(callback.data.split("_")[-1])

    async with session_scope() as session:
        mailing = await MailingRepository.get_by_id(session, mailing_id)
        if mailing:
            new_value = not mailing.smart_delay
            await session.execute(
                update(Mailing)
                .where(Mailing.id == mailing_id)
                .values(smart_delay=new_value)
            )
            await session.commit()

            log.info(f"Рассылка {mailing_id}: smart_delay = {new_value}")

            mailing.smart_delay = new_value
            await callback.message.edit_text(
                mailing_security_screen_html(mailing),
                reply_markup=get_mailing_security_keyboard(mailing),
                parse_mode=ParseMode.HTML,
            )

    await callback.answer()


# ==================== Редактирование параметров ====================

@router.callback_query(F.data.startswith("mailing_edit_delay_"))
async def cb_edit_delay(callback: CallbackQuery, state: FSMContext):
    """Редактирование задержки между сообщениями."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    mailing_id = int(callback.data.split("_")[-1])
    await state.update_data(mailing_id=mailing_id)
    await state.set_state(MailingEditFSM.waiting_for_delay)

    await callback.message.edit_text(
        "⏱ <b>Задержка между сообщениями</b>\n\n"
        "<b>Что меняет:</b> сколько секунд ждать после каждой попытки отправки одному клиенту "
        "(успех или ошибка), прежде чем брать следующего клиента в текущей очереди или повторять логику. "
        "Это основной «темп» рассылки.\n\n"
        "При включённой <b>умной задержке</b> к введённому числу добавляется случайный разброс.\n\n"
        "Введите число секунд (например: <code>10</code>).\n\n"
        "<i>Назад — кнопка ниже.</i>",
        reply_markup=get_context_back_keyboard(f"mailing_mod_security_{mailing_id}"),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.message(MailingEditFSM.waiting_for_delay)
async def process_delay(message: Message, state: FSMContext):
    """Обработка нового значения задержки."""
    if message.from_user.id != OWNER_ID:
        return

    try:
        new_delay = float(message.text.strip())
        if new_delay < 0:
            raise ValueError("Задержка должна быть >= 0")
    except ValueError as e:
        await message.answer(f"❌ Ошибка: {e}\n\nВведите число >= 0:")
        return

    data = await state.get_data()
    mailing_id = data.get('mailing_id')

    async with session_scope() as session:
        await session.execute(
            update(Mailing)
            .where(Mailing.id == mailing_id)
            .values(delay_between_messages=new_delay)
        )
        await session.commit()

        mailing = await MailingRepository.get_by_id(session, mailing_id)

    await state.clear()

    log.info(f"Рассылка {mailing_id}: delay_between_messages = {new_delay}")

    await message.answer(
        f"✅ Задержка обновлена: {new_delay} сек\n\n"
        "⚙️ Настройки рассылки:",
        reply_markup=get_mailing_settings_keyboard(mailing),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data.startswith("mailing_edit_messages_per_batch_"))
async def cb_edit_batch(callback: CallbackQuery, state: FSMContext):
    """Редактирование лимита успешных сообщений на один аккаунт (и ротация)."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    mailing_id = int(callback.data.split("_")[-1])
    await state.update_data(mailing_id=mailing_id)
    await state.set_state(MailingEditFSM.waiting_for_batch_size)

    await callback.message.edit_text(
        "📨 <b>Сообщений на аккаунт</b>\n\n"
        "<b>Для рассылки с группой аккаунтов:</b> каждый аккаунт делает до N <b>успешных</b> "
        "отправок подряд, затем очередь переходит к следующему; когда у <b>всех</b> аккаунтов "
        "группы набрано по N успехов — кампания завершается (остальные NEW остаются на потом).\n\n"
        "Введите целое число ≥ 1 (по умолчанию в новых рассылках: <code>10</code>).\n\n"
        "<i>Назад — кнопка ниже.</i>",
        reply_markup=get_context_back_keyboard(f"mailing_mod_security_{mailing_id}"),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.message(MailingEditFSM.waiting_for_batch_size)
async def process_batch_size(message: Message, state: FSMContext):
    """Обработка нового значения количества сообщений."""
    if message.from_user.id != OWNER_ID:
        return

    try:
        new_value = int(message.text.strip())
        if new_value < 1:
            raise ValueError("Значение должно быть >= 1")
    except ValueError as e:
        await message.answer(f"❌ Ошибка: {e}\n\nВведите число >= 1:")
        return

    data = await state.get_data()
    mailing_id = data.get('mailing_id')

    async with session_scope() as session:
        await session.execute(
            update(Mailing)
            .where(Mailing.id == mailing_id)
            .values(messages_per_batch=new_value)
        )
        await session.commit()

        mailing = await MailingRepository.get_by_id(session, mailing_id)

    await state.clear()

    log.info(f"Рассылка {mailing_id}: messages_per_account (messages_per_batch) = {new_value}")

    await message.answer(
        f"✅ «На аккаунт» обновлено: {new_value}\n\n"
        "⚙️ Настройки рассылки:",
        reply_markup=get_mailing_settings_keyboard(mailing),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data.startswith("mailing_edit_batch_delay_"))
async def cb_edit_batch_delay(callback: CallbackQuery, state: FSMContext):
    """Редактирование задержки между пакетами."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    mailing_id = int(callback.data.split("_")[-1])
    await state.update_data(mailing_id=mailing_id)
    await state.set_state(MailingEditFSM.waiting_for_batch_delay)

    await callback.message.edit_text(
        "🔄 <b>Дополнительная задержка между сменой аккаунтов</b>\n\n"
        "<b>Что меняет:</b> после того как аккаунт отправил свою порцию («на аккаунт»), "
        "перед переключением на следующий аккаунт добавляется эта пауза <b>плюс</b> внутренняя "
        "«задержка между аккаунтами» из профиля рассылки (по умолчанию 10 с; меняется только в БД/коде).\n\n"
        "При <b>умной задержке</b> к итоговой сумме тоже применяется разброс.\n\n"
        "Введите секунды (например: <code>45</code>).\n\n"
        "<i>Назад — кнопка ниже.</i>",
        reply_markup=get_context_back_keyboard(f"mailing_mod_security_{mailing_id}"),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.message(MailingEditFSM.waiting_for_batch_delay)
async def process_batch_delay(message: Message, state: FSMContext):
    """Обработка нового значения задержки между пакетами."""
    if message.from_user.id != OWNER_ID:
        return

    try:
        new_delay = float(message.text.strip())
        if new_delay < 0:
            raise ValueError("Задержка должна быть >= 0")
    except ValueError as e:
        await message.answer(f"❌ Ошибка: {e}\n\nВведите число >= 0:")
        return

    data = await state.get_data()
    mailing_id = data.get('mailing_id')

    async with session_scope() as session:
        await session.execute(
            update(Mailing)
            .where(Mailing.id == mailing_id)
            .values(batch_delay=new_delay)
        )
        await session.commit()

        mailing = await MailingRepository.get_by_id(session, mailing_id)

    await state.clear()

    log.info(f"Рассылка {mailing_id}: batch_delay = {new_delay}")

    await message.answer(
        f"✅ Задержка между пакетами обновлена: {new_delay} сек\n\n"
        "⚙️ Настройки рассылки:",
        reply_markup=get_mailing_settings_keyboard(mailing),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data.startswith("mailing_edit_runtime_"))
async def cb_edit_runtime(callback: CallbackQuery, state: FSMContext):
    """Редактирование автоостановки рассылки (в часах)."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    mailing_id = int(callback.data.split("_")[-1])
    await state.update_data(mailing_id=mailing_id)
    await state.set_state(MailingEditFSM.waiting_for_runtime_hours)

    await callback.message.edit_text(
        "🕒 <b>Автостоп по времени</b>\n\n"
        "<b>Что меняет:</b> через сколько часов после <b>старта</b> этой рассылки она сама завершится "
        "(статус «завершена»), даже если база клиентов не исчерпана. Удобно для ночных прогонов.\n\n"
        "Введите часы (например: <code>12</code>) или <code>0</code> / <code>off</code> / <code>выкл</code> — "
        "только ручная остановка.\n\n"
        "<i>Назад — кнопка ниже.</i>",
        reply_markup=get_context_back_keyboard(f"mailing_mod_security_{mailing_id}"),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.message(MailingEditFSM.waiting_for_runtime_hours)
async def process_runtime_hours(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return

    raw = (message.text or "").strip().lower()
    value = None
    if raw in ("0", "off", "none", "выкл", "нет"):
        value = None
    else:
        try:
            hrs = float(raw.replace(",", "."))
            if hrs <= 0:
                raise ValueError("Часы должны быть > 0")
            value = hrs
        except ValueError as e:
            await message.answer(
                f"❌ Ошибка: {e}\n\nВведите число > 0 или 0/off для отключения:"
            )
            return

    data = await state.get_data()
    mailing_id = data.get("mailing_id")

    async with session_scope() as session:
        await session.execute(
            update(Mailing)
            .where(Mailing.id == mailing_id)
            .values(auto_stop_hours=value)
        )
        await session.commit()
        mailing = await MailingRepository.get_by_id(session, mailing_id)

    await state.clear()

    txt = (
        f"✅ Автоостановка обновлена: {value:g} ч"
        if value is not None
        else "✅ Автоостановка отключена: рассылка работает до ручной остановки."
    )
    await message.answer(
        f"{txt}\n\n⚙️ Настройки рассылки:",
        reply_markup=get_mailing_settings_keyboard(mailing),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data.startswith("mailing_edit_text_"))
async def cb_edit_text(callback: CallbackQuery, state: FSMContext):
    """Редактирование текста сообщения."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    mailing_id = int(callback.data.split("_")[-1])
    await state.update_data(mailing_id=mailing_id)
    await state.set_state(MailingEditFSM.waiting_for_text)

    await callback.message.edit_text(
        "📝 <b>Основной текст сообщения</b>\n\n"
        "Отправьте текст. Плейсхолдеры:\n"
        "<code>{username}</code> <code>{date}</code> <code>{time}</code> <code>{datetime}</code> <code>{timezone}</code>\n"
        "<code>{date+3}</code> <code>{time-2}</code> <code>{datetime+1}</code> <code>{timezone-1}</code>\n"
        "<code>{firstname}</code> <code>{lastname}</code> <code>{phone}</code> <code>{account_id}</code>\n"
        "<code>{mailing}</code> <code>{link}</code> <code>{random4}</code> <code>{random6}</code>\n\n"
        "<i>Базовый UTC-сдвиг: кнопка «🕐 Часовой пояс» в модуле «Первое сообщение» "
        f"(запасной вариант — <code>MAILING_BASE_UTC_OFFSET</code> в .env, сейчас {MAILING_BASE_UTC_OFFSET}).</i>\n\n"
        "Дополнительные варианты — в модуле «Первое сообщение».\n\n"
        "<i>Назад — кнопка ниже.</i>",
        reply_markup=get_context_back_keyboard(f"mailing_mod_first_{mailing_id}"),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.message(MailingEditFSM.waiting_for_text)
async def process_text(message: Message, state: FSMContext):
    """Обработка нового текста сообщения."""
    if message.from_user.id != OWNER_ID:
        return

    new_text = message.text

    data = await state.get_data()
    mailing_id = data.get('mailing_id')

    async with session_scope() as session:
        await session.execute(
            update(Mailing)
            .where(Mailing.id == mailing_id)
            .values(message_text=new_text)
        )
        await session.commit()

        mailing = await MailingRepository.get_by_id(session, mailing_id)

    await state.clear()

    log.info(f"Рассылка {mailing_id}: текст обновлён")

    extra_n = len(_extra_variants(mailing))
    await message.answer(
        "✅ Основной текст обновлён.\n\n"
        "✉️ Модуль «Первое сообщение»:",
        reply_markup=await _kbd_mailing_first_message(mailing_id, mailing),
        parse_mode=ParseMode.HTML,
    )


# ==================== Удаление рассылки ====================

@router.callback_query(F.data.startswith("mailing_delete_confirm_"))
async def cb_mailing_delete_confirm(callback: CallbackQuery):
    """Подтверждение удаления рассылки."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    mailing_id = int(callback.data.split("_")[-1])

    # Показываем подтверждение
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⚠️ Да, удалить", callback_data=f"mailing_delete_{mailing_id}"),
            ],
            [
                InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"mailing_view_{mailing_id}"),
            ],
        ]
    )

    await callback.message.answer(
        "⚠️ <b>Подтверждение удаления</b>\n\n"
        f"Вы уверены, что хотите удалить рассылку #{mailing_id}?\n"
        "Это действие нельзя отменить.",
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("mailing_delete_"))
async def cb_mailing_delete(callback: CallbackQuery):
    """Удаление рассылки."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    mailing_id = int(callback.data.split("_")[-1])

    async with session_scope() as session:
        await MailingRepository.delete(session, mailing_id)

    log.info(f"Удалена рассылка {mailing_id}")

    await callback.message.answer(
        f"🗑 Рассылка #{mailing_id} удалена.",
        reply_markup=get_context_back_keyboard("mailing_list", "📋 К списку рассылок"),
    )
    await callback.answer()


# ==================== Запуск рассылки ====================

@router.callback_query(F.data.regexp(r"^mailing_start_\d+$"))
async def cb_mailing_start(callback: CallbackQuery):
    """Перед запуском — экран подтверждения (проверка аккаунтов, готовность)."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    mailing_id = int(callback.data.split("_")[-1])

    async with session_scope() as session:
        mailing = await MailingRepository.get_by_id(session, mailing_id)

    if not mailing:
        await callback.message.answer(
            "❌ Рассылка не найдена.",
            reply_markup=get_context_back_keyboard("mailing_list", "📋 К списку рассылок"),
        )
        await callback.answer()
        return

    if not mailing_has_launchable_text(mailing):
        await callback.message.answer(
            "⚠️ <b>Нет текста для рассылки</b>\n\n"
            "Задайте основной текст или добавьте варианты в разделе "
            "«Настройки → Первое сообщение».",
            reply_markup=get_mailing_view_keyboard(
                mailing,
                show_stop=False,
                show_neuro_stop=_mailing_show_neuro_stop(mailing, False),
            ),
            parse_mode=ParseMode.HTML,
        )
        await callback.answer()
        return

    from bot.main import safe_edit_message

    safe_name = html.escape((mailing.name or f"#{mailing.id}").strip() or f"#{mailing.id}")
    confirm_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить",
                    callback_data=f"mailing_start_confirm_{mailing_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Нет, назад",
                    callback_data=f"mailing_view_{mailing_id}",
                ),
            ],
        ]
    )
    await safe_edit_message(
        callback,
        "🚀 <b>Запуск рассылки</b>\n\n"
        f"Рассылка «{safe_name}» (id <code>{mailing_id}</code>).\n\n"
        "<b>Рекомендуется</b> перед стартом пройти проверку аккаунтов в группе "
        "(спамблок и прочее) и убедиться, что аккаунты готовы к работе.\n\n"
        "Запустить сейчас?",
        reply_markup=confirm_kb,
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^mailing_start_confirm_\d+$"))
async def cb_mailing_start_confirm(callback: CallbackQuery):
    """Подтверждённый запуск рассылки."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    mailing_id = int(callback.data.split("_")[-1])

    async with session_scope() as session:
        mailing = await MailingRepository.get_by_id(session, mailing_id)

    if not mailing:
        await callback.message.answer(
            "❌ Рассылка не найдена.",
            reply_markup=get_context_back_keyboard("mailing_list", "📋 К списку рассылок"),
        )
        await callback.answer()
        return

    if not mailing_has_launchable_text(mailing):
        await callback.message.answer(
            "⚠️ <b>Нет текста для рассылки</b>\n\n"
            "Задайте основной текст или добавьте варианты в разделе "
            "«Настройки → Первое сообщение».",
            reply_markup=get_mailing_view_keyboard(
                mailing,
                show_stop=False,
                show_neuro_stop=_mailing_show_neuro_stop(mailing, False),
            ),
            parse_mode=ParseMode.HTML,
        )
        await callback.answer()
        return

    if not await _execute_mailing_start(callback, mailing):
        return


async def _execute_mailing_start(callback: CallbackQuery, mailing: Mailing) -> bool:
    """Запуск рассылки. False — уже ответили через callback (ошибка/конфликт)."""
    from workers.manager import worker_manager

    if worker_manager._mailing_busy:
        if worker_manager.current_mailing_id == mailing.id:
            await callback.answer("Эта рассылка уже запущена", show_alert=True)
        else:
            await callback.answer(
                "Дождитесь завершения текущей рассылки или восстановления аккаунтов",
                show_alert=True,
            )
        return False

    await callback.answer("Подключаю аккаунты…")
    gid = getattr(mailing, "target_group_id", None)
    await worker_manager.load_accounts(group_id=gid)
    await worker_manager.connect_all()

    asyncio.create_task(worker_manager.start_mailing(mailing.id))
    await _render_mailing_screen(callback, mailing.id)
    return True


# ==================== Кнопка отмены ====================

@router.callback_query(F.data == "cancel_mailing")
async def cb_cancel_mailing(callback: CallbackQuery, state: FSMContext):
    """Отмена сценария рассылки (отдельный callback от аккаунтов/прокси)."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await state.clear()

    await callback.message.edit_text(
        "❌ <b>Отменено</b>\n\n"
        "Операция отменена.",
        reply_markup=get_mailing_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()
