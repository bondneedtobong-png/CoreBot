"""
Нейрочаттинг: настройки OpenRouter по кампании (после рассылки).
"""
import html
import json

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import update

from bot.config import DEFAULT_NEURO_MODEL, OPENROUTER_API_KEY, OWNER_ID
from bot.keyboards.main import (
    get_context_back_keyboard,
    get_mailing_neuro_keyboard,
    get_mailing_neuro_sampling_keyboard,
)
from database.models import Mailing
from database.repositories import (
    ClientRepository,
    InstanceSettingsRepository,
    MailingRepository,
    NeuroActionRepository,
)
from database.session import session_scope
from utils.crypto_openrouter import mask_api_key
from utils.links import normalize_public_link
from utils.neuro_prompts import neuro_prompt_file_path, prompt_file_exists, load_system_prompt
from utils.neuro_sampling import (
    CODE_TO_KEY,
    NEURO_PARAM_BUTTONS,
    coerce_param_value,
    format_sampling_human,
    format_sampling_menu_block,
    merge_sampling_for_request,
    parse_sampling_mailing_column,
)

router = Router()


class NeuroChatFSM(StatesGroup):
    """Модель, промпт, ссылка, сэмплирование."""
    waiting_for_model = State()
    waiting_for_prompt_file = State()
    waiting_for_link = State()
    waiting_for_sampling_value = State()


def _param_display_label(param_key: str) -> str:
    for _code, k, lab in NEURO_PARAM_BUTTONS:
        if k == param_key:
            return lab
    return param_key


async def _render_neurochat_hub(callback: CallbackQuery) -> None:
    async with session_scope() as session:
        mailings = await MailingRepository.get_all(session)
    rows = []
    for m in mailings[:25]:
        label = (m.name or f"#{m.id}")[:48]
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🔮 {label}",
                    callback_data=f"neurochat_open_{m.id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ В главное меню",
                callback_data="menu_back",
            )
        ]
    )
    await callback.message.edit_text(
        "🧠 <b>Нейрочаттинг</b>\n\n"
        "Нейрочат привязан к <b>рассылке</b>: после первого сообщения кампании ответы на входящие "
        "идут с того же аккаунта по промпту и модели ниже.\n\n"
        "<b>Выберите рассылку</b> — модель, system.txt (плейсхолдеры {first_name}, {link}, …), сэмплирование.\n"
        "<i>Сводка по клиентам/классам — в «База данных».</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data == "menu_neurochat")
async def cb_menu_neurochat(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    await _render_neurochat_hub(callback)
    await callback.answer()


@router.callback_query(F.data.startswith("neurochat_progress_"))
async def cb_neurochat_progress(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔", show_alert=True)
        return
    await state.clear()
    mailing_id = int(callback.data.split("_")[-1])
    async with session_scope() as session:
        mailing = await MailingRepository.get_by_id(session, mailing_id)
        if not mailing:
            await callback.answer("Не найдено", show_alert=True)
            return
        nq = await ClientRepository.count_mailing_queue(session, mailing)
        sent = int(getattr(mailing, "messages_sent", None) or 0)
        fail = int(getattr(mailing, "messages_failed", None) or 0)
        neuro_actions = await NeuroActionRepository.count_by_action(session, mailing_id)
    cap = getattr(mailing, "max_recipients", None)
    cap_line = f"{sent} / {int(cap)}" if cap else f"{sent} (лимит не задан)"
    text = (
        "📊 <b>Прогресс и нейрочат</b>\n\n"
        f"Рассылка ID: <code>{mailing_id}</code>\n"
        f"Успешных первых сообщений (за запуск): <b>{cap_line}</b>\n"
        f"Ошибок: <b>{fail}</b>\n"
        f"Оценка очереди: <b>{nq}</b> клиентов\n\n"
        "🔮 <b>Нейро-команды</b> (за запуск): "
        f"[SEND_LINK]={neuro_actions.get('SEND_LINK', 0)} · "
        f"[STOP]={neuro_actions.get('STOP', 0)} · "
        f"[ACCEPT]={neuro_actions.get('ACCEPT', 0)}\n\n"
        "<i>Пока аккаунты в паузе рассылки, нейрочат отвечает параллельно.</i>"
    )
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⚙️ Настройки нейрочата",
                        callback_data=f"neurochat_open_{mailing_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ К рассылке",
                        callback_data=f"mailing_view_{mailing_id}",
                    )
                ],
            ]
        ),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()



@router.callback_query(F.data.startswith("neurochat_open_"))
async def cb_neurochat_open(callback: CallbackQuery, state: FSMContext):
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
        eff = await InstanceSettingsRepository.get_effective_openrouter_key(session)
        stored = await InstanceSettingsRepository.has_stored_key(session)

    model = (mailing.neuro_model or "").strip() or DEFAULT_NEURO_MODEL
    link = normalize_public_link((getattr(mailing, "community_link", None) or "").strip())
    has_prompt = prompt_file_exists(mailing_id)
    prompt_preview = ""
    if has_prompt:
        full = load_system_prompt(mailing_id)
        prompt_preview = full[:200] + ("…" if len(full) > 200 else "")
    masked = mask_api_key(eff or "")
    if stored:
        key_line = f"активный ключ: <code>{masked}</code> (в базе бота)"
    elif (OPENROUTER_API_KEY or "").strip():
        key_line = f"активный ключ: <code>{masked}</code> (из .env)"
    else:
        key_line = "ключ <b>не задан</b> — укажите через «Ключ OpenRouter» или .env"

    overrides = parse_sampling_mailing_column(getattr(mailing, "neuro_sampling_json", None))
    effective_samp = merge_sampling_for_request(overrides)
    samp_line = format_sampling_human(effective_samp)

    txt = (
        "🔮 <b>Нейрочат (OpenRouter)</b>\n\n"
        f"📋 {mailing.name or mailing_id}\n\n"
        f"• Включено: <b>{'да' if mailing.neurochat_enabled else 'нет'}</b>\n"
        f"• Модель: <code>{model}</code>\n"
        f"• Ссылка {{link}}: <code>{link or 'не задана'}</code>\n"
        f"• Файл промпта: <code>data/neuro/mailings/{mailing_id}/system.txt</code>\n"
        f"  — {'загружен' if has_prompt else 'нет (используется дефолт из кода)'}\n"
        f"• Ключ API: {key_line}\n"
        f"• Сэмплирование: {html.escape(samp_line)}\n"
    )
    if prompt_preview:
        txt += f"\n<i>Превью:</i>\n<pre>{prompt_preview}</pre>\n"
    txt += (
        "\nПосле завершения рассылки (если нейрочат включён) входящие в личку "
        "отвечаются с того же аккаунта. Контекст — последние сообщения в паре "
        "«аккаунт ↔ собеседник», без смешивания диалогов."
    )

    await callback.message.edit_text(
        txt,
        reply_markup=get_mailing_neuro_keyboard(mailing),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^mailing_neuro_sampling_(\d+)$"))
async def cb_neurochat_sampling_menu(callback: CallbackQuery, state: FSMContext):
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
    overrides = parse_sampling_mailing_column(getattr(mailing, "neuro_sampling_json", None))
    effective = merge_sampling_for_request(overrides)
    block = format_sampling_menu_block(effective)
    await callback.message.edit_text(
        "🎛 <b>Параметры сэмплирования (OpenRouter)</b>\n\n"
        f"📋 {html.escape(mailing.name or str(mailing_id))}\n\n"
        "<b>Текущие значения</b> (дефолты из config + переопределения рассылки):\n"
        f"{block}\n\n"
        "Нажмите параметр и отправьте <b>одно число</b>. "
        "«Сбросить» убирает переопределения рассылки и возвращает глобальные дефолты.\n\n"
        "<i>Отдельные модели могут игнорировать часть полей.</i>",
        reply_markup=get_mailing_neuro_sampling_keyboard(mailing_id),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^mailing_nsp_(\d+)_(mt|te|tp|tk|fp|pp|rp|mp|ta)$"))
async def cb_neurochat_sampling_param(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    parts = callback.data.split("_")
    mailing_id = int(parts[2])
    code = parts[3]
    param_key = CODE_TO_KEY.get(code)
    if not param_key:
        await callback.answer("Ошибка кнопки", show_alert=True)
        return
    async with session_scope() as session:
        mailing = await MailingRepository.get_by_id(session, mailing_id)
    if not mailing:
        await callback.answer("Не найдено", show_alert=True)
        return
    overrides = parse_sampling_mailing_column(getattr(mailing, "neuro_sampling_json", None))
    effective = merge_sampling_for_request(overrides)
    current_val = effective.get(param_key)
    label = _param_display_label(param_key)
    await state.set_state(NeuroChatFSM.waiting_for_sampling_value)
    await state.update_data(
        mailing_neuro_id=mailing_id,
        sampling_param_key=param_key,
    )
    await callback.message.edit_text(
        f"🎛 <b>{html.escape(label)}</b>\n\n"
        f"Текущее значение: <code>{html.escape(str(current_val))}</code>\n\n"
        "Отправьте одно число (целое или с десятичной точкой).\n\n"
        "<i>Назад — кнопка ниже.</i>",
        reply_markup=get_context_back_keyboard(
            f"mailing_neuro_sampling_{mailing_id}",
            "⬅️ К параметрам",
        ),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^mailing_neuro_sampling_reset_(\d+)$"))
async def cb_neurochat_sampling_reset(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    mailing_id = int(callback.data.split("_")[-1])
    async with session_scope() as session:
        await MailingRepository.update_neuro(
            session, mailing_id, neuro_sampling_json="{}"
        )
    await callback.answer("Сброшено")
    await cb_neurochat_sampling_menu(callback, state)


@router.callback_query(F.data.startswith("mailing_neuro_toggle_"))
async def cb_neurochat_toggle(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    mailing_id = int(callback.data.split("_")[-1])
    async with session_scope() as session:
        mailing = await MailingRepository.get_by_id(session, mailing_id)
        if not mailing:
            await callback.answer("Не найдено", show_alert=True)
            return
        await MailingRepository.update_neuro(
            session,
            mailing_id,
            neurochat_enabled=not mailing.neurochat_enabled,
        )
    await cb_neurochat_open(callback, state)


@router.callback_query(F.data.startswith("mailing_neuro_model_"))
async def cb_neurochat_model(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    mailing_id = int(callback.data.split("_")[-1])
    await state.set_state(NeuroChatFSM.waiting_for_model)
    await state.update_data(mailing_neuro_id=mailing_id)

    await callback.message.edit_text(
        "🧠 <b>Модель OpenRouter</b>\n\n"
        "Отправьте одним сообщением идентификатор модели "
        "(как на openrouter.ai), например:\n"
        f"<code>{DEFAULT_NEURO_MODEL}</code>\n\n"
        "<i>Назад — кнопка ниже.</i>",
        reply_markup=get_context_back_keyboard(
            f"neurochat_open_{mailing_id}",
            "⬅️ Назад",
        ),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.message(NeuroChatFSM.waiting_for_sampling_value)
async def process_neuro_sampling_value(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return
    data = await state.get_data()
    mailing_id = data.get("mailing_neuro_id")
    param_key = data.get("sampling_param_key")
    if not mailing_id or not param_key:
        await state.clear()
        await message.answer("Сессия устарела.")
        return
    try:
        val = coerce_param_value(param_key, message.text or "")
    except ValueError as e:
        await message.answer(f"Нужно одно число: {e}")
        return
    async with session_scope() as session:
        mailing = await MailingRepository.get_by_id(session, mailing_id)
        if not mailing:
            await state.clear()
            await message.answer("Рассылка не найдена.")
            return
        overrides = parse_sampling_mailing_column(getattr(mailing, "neuro_sampling_json", None))
        overrides[param_key] = val
        await MailingRepository.update_neuro(
            session,
            mailing_id,
            neuro_sampling_json=json.dumps(overrides, ensure_ascii=False),
        )
    await state.clear()
    label = _param_display_label(param_key)
    await message.answer(
        f"✅ <b>{html.escape(label)}</b> = <code>{html.escape(str(val))}</code>",
        parse_mode=ParseMode.HTML,
    )
    await message.answer(
        "🎛 Параметры сэмплирования",
        reply_markup=get_mailing_neuro_sampling_keyboard(mailing_id),
        parse_mode=ParseMode.HTML,
    )


@router.message(NeuroChatFSM.waiting_for_model)
async def process_neuro_model(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return

    model = (message.text or "").strip()
    if not model:
        await message.answer("Пустая строка — повторите ввод.")
        return

    data = await state.get_data()
    mailing_id = data.get("mailing_neuro_id")
    await state.clear()

    if not mailing_id:
        await message.answer("Сессия устарела.")
        return

    async with session_scope() as session:
        await MailingRepository.update_neuro(session, mailing_id, neuro_model=model)

    async with session_scope() as session:
        mailing = await MailingRepository.get_by_id(session, mailing_id)

    await message.answer(
        f"✅ Модель сохранена: <code>{model}</code>",
        parse_mode=ParseMode.HTML,
    )
    if mailing:
        await message.answer(
            "🔮 Нейрочат",
            reply_markup=get_mailing_neuro_keyboard(mailing),
            parse_mode=ParseMode.HTML,
        )


@router.callback_query(F.data.startswith("mailing_neuro_prompt_"))
async def cb_neurochat_prompt(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    mailing_id = int(callback.data.split("_")[-1])
    await state.set_state(NeuroChatFSM.waiting_for_prompt_file)
    await state.update_data(mailing_neuro_id=mailing_id)

    await callback.message.edit_text(
        "📄 <b>System-промпт</b>\n\n"
        "Пришлите <b>.txt</b> файл (документом, не как текст). "
        "Он будет сохранён как\n"
        f"<code>data/neuro/mailings/{mailing_id}/system.txt</code>\n\n"
        "Поддерживаемые служебные команды в тексте ответа модели:\n"
        "• <code>[SEND_LINK]</code> — отправить ссылку\n"
        "• <code>[STOP]</code> — класс <code>stop</code> у клиента в CRM (без отдельного стоп-листа)\n"
        "• <code>[ACCEPT]</code> — пометить успешный интерес\n"
        "• <code>[DECLINE]</code> — пометить отказ\n"
        "• <code>[HATER]</code> — пометить токсичный отказ\n\n"
        "Назад — кнопка ниже.",
        reply_markup=get_context_back_keyboard(
            f"neurochat_open_{mailing_id}",
            "⬅️ Назад",
        ),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("mailing_neuro_link_"))
async def cb_neurochat_link(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    mailing_id = int(callback.data.split("_")[-1])
    await state.set_state(NeuroChatFSM.waiting_for_link)
    await state.update_data(mailing_neuro_id=mailing_id)

    await callback.message.edit_text(
        "🔗 <b>Ссылка для плейсхолдера {link}</b>\n\n"
        "Отправьте действующую ссылку (https://...).\n"
        "Она будет подставляться в первое сообщение и в нейропромпт/ответы вместо <code>{link}</code>.\n"
        "Чтобы очистить, отправьте: <code>off</code>\n\n"
        "Назад — кнопка ниже.",
        reply_markup=get_context_back_keyboard(
            f"neurochat_open_{mailing_id}",
            "⬅️ Назад",
        ),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.message(NeuroChatFSM.waiting_for_prompt_file, F.document)
async def process_neuro_prompt_doc(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return

    data = await state.get_data()
    mailing_id = data.get("mailing_neuro_id")
    if not mailing_id:
        await state.clear()
        await message.answer("Сессия устарела.")
        return

    fn = (message.document.file_name or "").lower()
    if not fn.endswith(".txt"):
        await message.answer("Нужен файл с расширением .txt")
        return

    from bot.config import BASE_DIR

    dest = neuro_prompt_file_path(mailing_id)
    dest.parent.mkdir(parents=True, exist_ok=True)

    await message.bot.download(message.document, destination=dest)

    await state.clear()

    async with session_scope() as session:
        mailing = await MailingRepository.get_by_id(session, mailing_id)

    await message.answer(
        f"✅ Промпт сохранён: <code>{dest.relative_to(BASE_DIR)}</code>",
        parse_mode=ParseMode.HTML,
    )
    if mailing:
        await message.answer(
            "🔮 Нейрочат",
            reply_markup=get_mailing_neuro_keyboard(mailing),
            parse_mode=ParseMode.HTML,
        )


@router.message(NeuroChatFSM.waiting_for_link)
async def process_neuro_link(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return

    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Отправьте ссылку или off.")
        return
    value = None
    if raw.lower() not in ("off", "none", "нет", "выкл", "0"):
        if not (raw.startswith("http://") or raw.startswith("https://")):
            await message.answer("Нужна ссылка в формате http:// или https://")
            return
        value = raw

    data = await state.get_data()
    mailing_id = data.get("mailing_neuro_id")
    await state.clear()
    if not mailing_id:
        await message.answer("Сессия устарела.")
        return

    async with session_scope() as session:
        await session.execute(
            update(Mailing).where(Mailing.id == mailing_id).values(community_link=value)
        )
        await session.commit()
        mailing = await MailingRepository.get_by_id(session, mailing_id)

    await message.answer(
        f"✅ Ссылка {('{link} очищена' if value is None else 'сохранена')}.\n"
        f"{value or ''}",
        parse_mode=ParseMode.HTML,
    )
    if mailing:
        await message.answer(
            "🔮 Нейрочат",
            reply_markup=get_mailing_neuro_keyboard(mailing),
            parse_mode=ParseMode.HTML,
        )