"""
Ключ OpenRouter: хранение в БД (опционально с шифрованием), ввод через бота.
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot.config import OPENROUTER_API_KEY, OWNER_ID
from bot.keyboards.main import (
    get_context_back_keyboard,
    get_mailing_neuro_keyboard,
    get_openrouter_key_clear_keyboard,
    get_openrouter_key_keyboard,
)
from database.repositories import InstanceSettingsRepository, MailingRepository
from database.session import session_scope
from utils.crypto_openrouter import mask_api_key
from utils.logger import log

router = Router()


class OpenRouterKeyFSM(StatesGroup):
    waiting_for_key = State()


async def _openrouter_key_text(mailing_id: int) -> str:
    async with session_scope() as session:
        eff = await InstanceSettingsRepository.get_effective_openrouter_key(session)
        stored = await InstanceSettingsRepository.has_stored_key(session)
    env_fallback = bool((OPENROUTER_API_KEY or "").strip())
    masked = mask_api_key(eff or "")
    parts = []
    if stored:
        parts.append("ключ в <b>базе бота</b>")
    if env_fallback:
        parts.append("резерв из <code>.env</code>")
    if not parts:
        parts.append("<b>ключ не задан</b>")
    src_line = " · ".join(parts)
    return (
        "🔑 <b>Ключ OpenRouter</b>\n\n"
        f"Активный ключ: <code>{masked}</code>\n"
        f"Источник: {src_line}\n\n"
        "Ключ из бота перекрывает <code>OPENROUTER_API_KEY</code> для этого инстанса.\n"
        "Для шифрования в БД задайте в .env <code>OPENROUTER_KEY_ENCRYPTION_KEY</code> "
        "(см. <code>.env.example</code>).\n\n"
        f"<i>Назад — нейрочат рассылки #{mailing_id}</i>"
    )


@router.callback_query(F.data.startswith("openrouter_key_menu_neuro_"))
async def cb_openrouter_key_menu(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    mailing_id = int(callback.data.split("_")[-1])
    txt = await _openrouter_key_text(mailing_id)
    await callback.message.edit_text(
        txt,
        reply_markup=get_openrouter_key_keyboard(mailing_id),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("openrouter_key_set_neuro_"))
async def cb_openrouter_key_set(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    mailing_id = int(callback.data.split("_")[-1])
    await state.set_state(OpenRouterKeyFSM.waiting_for_key)
    await state.update_data(or_key_mailing_id=mailing_id)
    await callback.message.edit_text(
        "🔑 <b>Новый ключ OpenRouter</b>\n\n"
        "Отправьте <b>одним сообщением</b> секретный ключ (как в личном кабинете OpenRouter).\n"
        "Сообщение после сохранения будет удалено, если бот сможет его удалить.\n\n"
        "<i>Назад — кнопка ниже.</i>",
        reply_markup=get_context_back_keyboard(
            f"openrouter_key_menu_neuro_{mailing_id}",
            "⬅️ Назад",
        ),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("openrouter_key_clear_confirm_neuro_"))
async def cb_openrouter_key_clear_confirm(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    mailing_id = int(callback.data.split("_")[-1])
    await callback.message.edit_text(
        "🗑 Удалить ключ из базы бота?\n\n"
        "После удаления будет использоваться только <code>OPENROUTER_API_KEY</code> из .env "
        "(если задан).",
        reply_markup=get_openrouter_key_clear_keyboard(mailing_id),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("openrouter_key_clear_do_neuro_"))
async def cb_openrouter_key_clear_do(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    mailing_id = int(callback.data.split("_")[-1])
    async with session_scope() as session:
        await InstanceSettingsRepository.clear_openrouter_key(session)
    txt = await _openrouter_key_text(mailing_id)
    await callback.message.edit_text(
        txt,
        reply_markup=get_openrouter_key_keyboard(mailing_id),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer("Ключ из бота удалён")


@router.message(OpenRouterKeyFSM.waiting_for_key)
async def process_openrouter_key(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return
    key = (message.text or "").strip()
    if not key:
        await message.answer("Пусто — пришлите ключ или нажмите «Назад».")
        return
    data = await state.get_data()
    mailing_id = data.get("or_key_mailing_id")
    await state.clear()
    try:
        await message.delete()
    except Exception as e:
        log.debug(f"Не удалось удалить сообщение с ключом: {e}")

    async with session_scope() as session:
        await InstanceSettingsRepository.set_openrouter_key(session, key)

    await message.answer("✅ Ключ сохранён в базе бота.")
    if mailing_id:
        async with session_scope() as session:
            mailing = await MailingRepository.get_by_id(session, mailing_id)
        if mailing:
            await message.answer(
                await _openrouter_key_text(mailing_id),
                reply_markup=get_openrouter_key_keyboard(mailing_id),
                parse_mode=ParseMode.HTML,
            )
            await message.answer(
                "🔮 Нейрочат",
                reply_markup=get_mailing_neuro_keyboard(mailing),
                parse_mode=ParseMode.HTML,
            )
