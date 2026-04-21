from pathlib import Path

proc = Path("bot/handlers/_neuro_proc.txt").read_text(encoding="utf-8")
# Drop text /cancel branches in message handlers
proc = proc.replace(
    """    if (message.text or "").strip().lower() in ("/cancel", "отмена"):
        data = await state.get_data()
        mailing_id = data.get("mailing_neuro_id")
        await state.clear()
        if mailing_id:
            async with session_scope() as session:
                mailing = await MailingRepository.get_by_id(session, mailing_id)
            if mailing:
                overrides = parse_sampling_mailing_column(
                    getattr(mailing, "neuro_sampling_json", None)
                )
                effective = merge_sampling_for_request(overrides)
                block = format_sampling_menu_block(effective)
                await message.answer(
                    "🎛 <b>Параметры сэмплирования (OpenRouter)</b>\\n\\n"
                    f"📋 {html.escape(mailing.name or str(mailing_id))}\\n\\n"
                    "<b>Текущие значения</b>:\\n"
                    f"{block}\\n\\n"
                    "Нажмите параметр и отправьте <b>одно число</b>.",
                    reply_markup=get_mailing_neuro_sampling_keyboard(mailing_id),
                    parse_mode=ParseMode.HTML,
                )
                return
        await message.answer("Отменено.")
        return
""",
    "",
)
proc = proc.replace(
    """    if (message.text or "").strip().lower() in ("/cancel", "отмена"):
        await state.clear()
        await message.answer("Отменено.")
        return

""",
    "",
)
proc = proc.replace(
    'await message.answer("Пустая строка — повторите или /cancel")',
    'await message.answer("Пустая строка — повторите ввод.")',
)
proc = proc.replace(
    """    if raw.lower() in ("/cancel", "cancel", "отмена"):
        await state.clear()
        await message.answer("Отменено.")
        return

""",
    "",
)

HEADER = r'''"""
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
    get_cancel_with_back_keyboard,
    get_context_back_keyboard,
    get_mailing_neuro_keyboard,
    get_mailing_neuro_sampling_keyboard,
    get_mailing_neuro_stoplist_keyboard,
)
from database.models import Mailing
from database.repositories import (
    ClientRepository,
    InstanceSettingsRepository,
    MailingRepository,
    NeuroActionRepository,
    NeuroStopRepository,
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
        "<b>Выберите рассылку</b> — модель, system.txt, ссылка {link}, сэмплирование, STOP-лист.\n"
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


'''

out = HEADER + "\n" + proc
Path("bot/handlers/neurochat.py").write_text(out, encoding="utf-8")
print("written", len(out.splitlines()))
