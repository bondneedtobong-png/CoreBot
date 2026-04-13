"""Отдельное меню прогрева: аккаунты/группы/настройки/сообщества."""
from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot.config import OWNER_ID
from bot.handlers.accounts.common import safe_edit_message
from bot.keyboards.main import (
    get_warmup_menu_keyboard,
    get_warmup_accounts_pick_keyboard,
    get_warmup_account_profile_keyboard,
    get_warmup_groups_pick_keyboard,
    get_warmup_group_action_keyboard,
    get_warmup_group_profile_keyboard,
    get_warmup_settings_keyboard,
    get_warmup_copy_source_keyboard,
    get_warmup_copy_mode_keyboard,
    get_warmup_copy_target_keyboard,
    get_warmup_copy_confirm_keyboard,
)
from database.session import session_scope
from database.repositories import (
    AccountRepository,
    GroupRepository,
    WarmupProfileRepository,
)

router = Router()


class WarmupSettingsFSM(StatesGroup):
    waiting_delay = State()
    waiting_limit = State()
    waiting_chats = State()
    waiting_copy_new_name = State()


@router.callback_query(F.data == "menu_warmup")
async def cb_menu_warmup(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await safe_edit_message(
        callback.message,
        "🔥 <b>Прогрев аккаунтов</b>\n\n"
        "Выбери цель (аккаунты/группа) и настрой профиль.\n"
        "Профиль применяется как безопасный сценарий активности.",
        reply_markup=get_warmup_menu_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data == "warmup_pick_accounts")
async def cb_warmup_pick_accounts(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    async with session_scope() as session:
        accounts = await AccountRepository.get_all(session)
    await safe_edit_message(
        callback.message,
        "👤 <b>Аккаунты</b>\n\nТапни, чтобы переключить прогрев для конкретного аккаунта.",
        reply_markup=get_warmup_accounts_pick_keyboard(accounts),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("warmup_toggle_acc_"))
async def cb_warmup_toggle_acc(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    account_id = int(callback.data.split("_")[-1])
    async with session_scope() as session:
        acc = await AccountRepository.get_by_id(session, account_id)
        if not acc:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return
        await AccountRepository.update_warmup_settings(
            session,
            account_id=account_id,
            enabled=not bool(acc.warmup_enabled),
            profile=acc.warmup_profile or "safe",
        )
        accounts = await AccountRepository.get_all(session)
    await safe_edit_message(
        callback.message,
        "👤 <b>Аккаунты</b>\n\nТапни, чтобы переключить прогрев для конкретного аккаунта.",
        reply_markup=get_warmup_accounts_pick_keyboard(accounts),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer("Обновлено")


@router.callback_query(F.data.startswith("warmup_acc_profile_"))
async def cb_warmup_acc_profile(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    account_id = int(callback.data.split("_")[-1])
    async with session_scope() as session:
        acc = await AccountRepository.get_by_id(session, account_id)
        if not acc:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return
        profiles = await WarmupProfileRepository.get_all(session)
    await safe_edit_message(
        callback.message,
        f"🧠 <b>Профиль прогрева для аккаунта #{account_id}</b>\n\n"
        f"Текущий: <code>{acc.warmup_profile or 'safe'}</code>",
        reply_markup=get_warmup_account_profile_keyboard(account_id, profiles, acc.warmup_profile or "safe"),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("warmup_set_acc_profile_"))
async def cb_warmup_set_acc_profile(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    payload = callback.data[len("warmup_set_acc_profile_"):]
    account_id_str, profile = payload.split("_", 1)
    account_id = int(account_id_str)
    async with session_scope() as session:
        acc = await AccountRepository.get_by_id(session, account_id)
        if not acc:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return
        await AccountRepository.update_warmup_settings(
            session,
            account_id=account_id,
            profile=profile.strip() or "safe",
            enabled=acc.warmup_enabled,
        )
        profiles = await WarmupProfileRepository.get_all(session)
    await safe_edit_message(
        callback.message,
        f"🧠 <b>Профиль прогрева для аккаунта #{account_id}</b>\n\n"
        f"Текущий: <code>{profile}</code>",
        reply_markup=get_warmup_account_profile_keyboard(account_id, profiles, profile),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer("Профиль обновлен")


@router.callback_query(F.data == "warmup_pick_group")
async def cb_warmup_pick_group(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    async with session_scope() as session:
        groups = await GroupRepository.get_all(session)
    await safe_edit_message(
        callback.message,
        "📂 <b>Группы аккаунтов</b>\n\nВыбери группу и включи/выключи прогрев для всей группы.",
        reply_markup=get_warmup_groups_pick_keyboard(groups),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("warmup_group_menu_"))
async def cb_warmup_group_menu(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    group_id = int(callback.data.split("_")[-1])
    await safe_edit_message(
        callback.message,
        f"📂 <b>Управление прогревом группы #{group_id}</b>",
        reply_markup=get_warmup_group_action_keyboard(group_id),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("warmup_group_on_"))
async def cb_warmup_group_on(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    group_id = int(callback.data.split("_")[-1])
    async with session_scope() as session:
        group = await GroupRepository.get_by_id(session, group_id)
        profile = "safe"
        if group and group.accounts:
            profile = (group.accounts[0].warmup_profile or "safe")
        changed = await AccountRepository.update_warmup_for_group(
            session, group_id, enabled=True, profile=profile
        )
    await callback.answer(f"Включено: {changed}", show_alert=True)


@router.callback_query(F.data.startswith("warmup_group_off_"))
async def cb_warmup_group_off(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    group_id = int(callback.data.split("_")[-1])
    async with session_scope() as session:
        changed = await AccountRepository.update_warmup_for_group(
            session, group_id, enabled=False
        )
    await callback.answer(f"Выключено: {changed}", show_alert=True)


@router.callback_query(F.data.startswith("warmup_group_profile_"))
async def cb_warmup_group_profile(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    group_id = int(callback.data.split("_")[-1])
    selected = "safe"
    async with session_scope() as session:
        group = await GroupRepository.get_by_id(session, group_id)
        profiles = await WarmupProfileRepository.get_all(session)
        if group and group.accounts:
            selected = (group.accounts[0].warmup_profile or "safe")
    await safe_edit_message(
        callback.message,
        f"🧠 <b>Профиль прогрева группы #{group_id}</b>\n\n"
        f"Выбор применится ко всем аккаунтам группы.",
        reply_markup=get_warmup_group_profile_keyboard(group_id, profiles, selected),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("warmup_group_set_profile_"))
async def cb_warmup_group_set_profile(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    payload = callback.data[len("warmup_group_set_profile_"):]
    group_id_str, profile = payload.split("_", 1)
    group_id = int(group_id_str)
    async with session_scope() as session:
        await AccountRepository.update_warmup_for_group(
            session,
            group_id,
            enabled=True,
            profile=profile.strip() or "safe",
        )
        profiles = await WarmupProfileRepository.get_all(session)
    await safe_edit_message(
        callback.message,
        f"🧠 <b>Профиль прогрева группы #{group_id}</b>\n\n"
        f"Текущий: <code>{profile}</code>",
        reply_markup=get_warmup_group_profile_keyboard(group_id, profiles, profile),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer("Профиль применен к группе")


@router.callback_query(F.data == "warmup_settings")
async def cb_warmup_settings(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    async with session_scope() as session:
        safe_profile = await WarmupProfileRepository.get_by_name(session, "safe")
    chats_preview = (safe_profile.target_chats_text or "").strip() if safe_profile else ""
    if chats_preview:
        chats_preview = chats_preview[:300] + ("..." if len(chats_preview) > 300 else "")
    await safe_edit_message(
        callback.message,
        "⚙️ <b>Настройки прогрева</b>\n\n"
        f"Профиль: <code>safe</code>\n"
        f"Delay: <b>{safe_profile.base_delay_sec if safe_profile else 45}</b> сек\n"
        f"Jitter: <b>{safe_profile.jitter_sec if safe_profile else 25}</b> сек\n"
        f"Дневной лимит: <b>{safe_profile.daily_action_limit if safe_profile else 40}</b>\n\n"
        f"Сообщества:\n<pre>{chats_preview or 'не задано'}</pre>",
        reply_markup=get_warmup_settings_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data == "warmup_copy_profile_start")
async def cb_warmup_copy_profile_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    async with session_scope() as session:
        profiles = await WarmupProfileRepository.get_all(session)
    await safe_edit_message(
        callback.message,
        "🧩 <b>Копирование профиля по шаблону</b>\n\n"
        "Выбери профиль-источник.",
        reply_markup=get_warmup_copy_source_keyboard(profiles),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("warmup_copy_source_idx_"))
async def cb_warmup_copy_source(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    idx_raw = callback.data[len("warmup_copy_source_idx_"):].strip()
    if not idx_raw.isdigit():
        await callback.answer("Неверный шаблон", show_alert=True)
        return
    idx = int(idx_raw)
    async with session_scope() as session:
        profiles = await WarmupProfileRepository.get_all(session)
    names = [str(p.name).strip() for p in profiles if getattr(p, "name", None)]
    if idx < 0 or idx >= len(names):
        await callback.answer("Неверный шаблон", show_alert=True)
        return
    source_name = names[idx]
    # Сохраняем выбор источника в FSM для следующих шагов.
    await state.update_data(copy_source=source_name)
    await safe_edit_message(
        callback.message,
        f"🧩 <b>Шаблон:</b> <code>{source_name}</code>\n\nВыбери режим копирования.",
        reply_markup=get_warmup_copy_mode_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data == "warmup_copy_mode_new")
async def cb_warmup_copy_mode_new(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    data = await state.get_data()
    source_name = str(data.get("copy_source") or "").strip()
    if not source_name:
        await callback.answer("Неверный шаблон", show_alert=True)
        return
    await state.set_state(WarmupSettingsFSM.waiting_copy_new_name)
    await state.update_data(copy_source=source_name)
    await callback.message.answer(
        f"Введите имя нового профиля для копии из <code>{source_name}</code>.\n"
        "Если имя занято, добавим авто-суффикс.",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data == "warmup_copy_mode_overwrite")
async def cb_warmup_copy_mode_overwrite(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    data = await state.get_data()
    source_name = str(data.get("copy_source") or "").strip()
    if not source_name:
        await callback.answer("Неверный шаблон", show_alert=True)
        return
    async with session_scope() as session:
        profiles = await WarmupProfileRepository.get_all(session)
    await safe_edit_message(
        callback.message,
        f"♻️ <b>Обновление существующего профиля</b>\n\n"
        f"Источник: <code>{source_name}</code>\n"
        "Выбери профиль-получатель (safe недоступен).",
        reply_markup=get_warmup_copy_target_keyboard(profiles),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data == "warmup_copy_mode_back")
async def cb_warmup_copy_mode_back(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    data = await state.get_data()
    source_name = str(data.get("copy_source") or "").strip()
    if not source_name:
        await callback.answer("Шаблон не выбран", show_alert=True)
        return
    await safe_edit_message(
        callback.message,
        f"🧩 <b>Шаблон:</b> <code>{source_name}</code>\n\nВыбери режим копирования.",
        reply_markup=get_warmup_copy_mode_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data == "warmup_copy_confirm_back")
async def cb_warmup_copy_confirm_back(callback: CallbackQuery, state: FSMContext):
    """С экрана подтверждения перезаписи — назад к выбору профиля-получателя."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    data = await state.get_data()
    source_name = str(data.get("copy_source") or "").strip()
    if not source_name:
        await callback.answer("Неверный шаблон", show_alert=True)
        return
    async with session_scope() as session:
        profiles = await WarmupProfileRepository.get_all(session)
    await safe_edit_message(
        callback.message,
        f"♻️ <b>Обновление существующего профиля</b>\n\n"
        f"Источник: <code>{source_name}</code>\n"
        "Выбери профиль-получатель (safe недоступен).",
        reply_markup=get_warmup_copy_target_keyboard(profiles),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("warmup_copy_target_idx_"))
async def cb_warmup_copy_target(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    idx_raw = callback.data[len("warmup_copy_target_idx_"):].strip()
    if not idx_raw.isdigit():
        await callback.answer("Некорректные данные", show_alert=True)
        return
    idx = int(idx_raw)
    data = await state.get_data()
    source_name = str(data.get("copy_source") or "").strip()
    async with session_scope() as session:
        profiles = await WarmupProfileRepository.get_all(session)
    names = [str(p.name).strip() for p in profiles if getattr(p, "name", None) and str(p.name).strip().lower() != "safe"]
    if idx < 0 or idx >= len(names):
        await callback.answer("Некорректные данные", show_alert=True)
        return
    target_name = names[idx]
    if not source_name or not target_name:
        await callback.answer("Некорректные данные", show_alert=True)
        return
    if target_name.lower() == "safe":
        await callback.answer("Профиль safe нельзя перезаписывать", show_alert=True)
        return
    await state.update_data(copy_target=target_name)
    await safe_edit_message(
        callback.message,
        "⚠️ <b>Подтверждение перезаписи</b>\n\n"
        f"Источник: <code>{source_name}</code>\n"
        f"Получатель: <code>{target_name}</code>\n\n"
        "Будут скопированы все поля профиля.",
        reply_markup=get_warmup_copy_confirm_keyboard("overwrite"),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data == "warmup_copy_confirm_overwrite")
async def cb_warmup_copy_confirm(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    data = await state.get_data()
    source_name = str(data.get("copy_source") or "").strip()
    target_name = str(data.get("copy_target") or "").strip()
    if not source_name or not target_name:
        await callback.answer("Некорректные данные", show_alert=True)
        return
    if target_name.lower() == "safe":
        await callback.answer("Профиль safe нельзя перезаписывать", show_alert=True)
        return
    async with session_scope() as session:
        ok = await WarmupProfileRepository.overwrite_profile_from_template(
            session, source_name=source_name, target_name=target_name
        )
    if not ok:
        await callback.answer("Не удалось выполнить копирование", show_alert=True)
        return
    await state.clear()
    await safe_edit_message(
        callback.message,
        "✅ <b>Профиль обновлен из шаблона</b>\n\n"
        f"<code>{target_name}</code> ← <code>{source_name}</code>",
        reply_markup=get_warmup_settings_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer("Готово")


@router.callback_query(F.data == "warmup_settings_delay")
async def cb_warmup_settings_delay(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.set_state(WarmupSettingsFSM.waiting_delay)
    await callback.message.answer(
        "Введите delay и jitter через пробел, например: <code>45 25</code>",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data == "warmup_settings_limit")
async def cb_warmup_settings_limit(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.set_state(WarmupSettingsFSM.waiting_limit)
    await callback.message.answer("Введите дневной лимит (число), например: <code>40</code>", parse_mode=ParseMode.HTML)
    await callback.answer()


@router.callback_query(F.data == "warmup_settings_chats")
async def cb_warmup_settings_chats(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.set_state(WarmupSettingsFSM.waiting_chats)
    await callback.message.answer(
        "Пришли сообщества по одному на строку (username/ссылки). Пример:\n"
        "<code>@chat1\n@chat2\nhttps://t.me/somegroup</code>",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.message(WarmupSettingsFSM.waiting_delay)
async def process_warmup_delay(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return
    parts = (message.text or "").strip().split()
    if len(parts) != 2:
        await message.answer("Нужен формат: <code>delay jitter</code>", parse_mode=ParseMode.HTML)
        return
    try:
        delay = float(parts[0]); jitter = float(parts[1])
    except ValueError:
        await message.answer("Нужно ввести числа.")
        return
    async with session_scope() as session:
        await WarmupProfileRepository.update_profile_settings(
            session, "safe", base_delay_sec=delay, jitter_sec=jitter
        )
    await state.clear()
    await message.answer("✅ Обновил delay/jitter профиля safe.")


@router.message(WarmupSettingsFSM.waiting_limit)
async def process_warmup_limit(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Нужно целое число.")
        return
    limit = int(raw)
    async with session_scope() as session:
        await WarmupProfileRepository.update_profile_settings(
            session, "safe", daily_action_limit=limit
        )
    await state.clear()
    await message.answer("✅ Обновил дневной лимит профиля safe.")


@router.message(WarmupSettingsFSM.waiting_chats)
async def process_warmup_chats(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return
    text = (message.text or "").strip()
    async with session_scope() as session:
        await WarmupProfileRepository.update_profile_settings(
            session, "safe", target_chats_text=text
        )
    await state.clear()
    await message.answer("✅ Сохранил список сообществ для активности (профиль safe).")


@router.message(WarmupSettingsFSM.waiting_copy_new_name)
async def process_copy_new_profile_name(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return
    raw_name = (message.text or "").strip()
    if not raw_name:
        await message.answer("Имя не может быть пустым.")
        return
    if "|" in raw_name:
        await message.answer("Символ '|' запрещен в имени профиля.")
        return
    data = await state.get_data()
    source_name = str(data.get("copy_source") or "").strip()
    if not source_name:
        await state.clear()
        await message.answer("Источник шаблона потерян, начни заново из настроек.")
        return
    async with session_scope() as session:
        created = await WarmupProfileRepository.create_profile_from_template(
            session, source_name=source_name, new_name=raw_name
        )
    await state.clear()
    if not created:
        await message.answer("Не удалось создать профиль из шаблона.")
        return
    await message.answer(
        "✅ Профиль создан из шаблона.\n"
        f"Источник: <code>{source_name}</code>\n"
        f"Новый профиль: <code>{created.name}</code>",
        parse_mode=ParseMode.HTML,
    )
