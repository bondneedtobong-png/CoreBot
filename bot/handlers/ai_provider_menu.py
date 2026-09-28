"""Owner-only Telegram menus for OpenAI-compatible provider profiles."""
from __future__ import annotations

import html
import json
import os
from contextlib import suppress

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from cryptography.fernet import Fernet
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from bot.config import OPENROUTER_API_KEY, is_authorized_user
from bot.keyboards.main import (
    get_ai_api_keys_keyboard,
    get_ai_custom_menu_keyboard,
    get_ai_model_settings_keyboard,
    get_context_back_keyboard,
)
from database.models import AIProvider, InstanceSettings, Mailing
from database.repositories import InstanceSettingsRepository
from database.session import session_scope
from services.neurochat.provider_registry import (
    create_provider,
    get_provider,
    list_providers,
    select_default_provider,
    select_mailing_provider,
    update_provider,
)

router = Router()
_PAGE_SIZE = 5
_KINDS = {"openai": "OpenAI", "deepseek": "DeepSeek", "custom": "Свой провайдер"}


class AIProviderFSM(StatesGroup):
    new_name = State()
    new_url = State()
    new_model = State()
    new_key = State()
    edit_value = State()
    openrouter_key = State()


def _button(label: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=label, callback_data=data)


def _markup(*rows: tuple[str, str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_button(label, data)] for label, data in rows])


def _allowed(user_id: int | None) -> bool:
    return is_authorized_user(user_id)


def _encryption_ready(*, openrouter: bool = False) -> bool:
    raw = (
        os.getenv("OPENROUTER_KEY_ENCRYPTION_KEY")
        if openrouter else os.getenv("AI_PROVIDER_ENCRYPTION_KEY") or os.getenv("OPENROUTER_KEY_ENCRYPTION_KEY")
    ) or ""
    try:
        Fernet(raw.strip().encode("ascii"))
        return True
    except (ValueError, TypeError, UnicodeError):
        return False


async def _edit(callback: CallbackQuery, text: str, keyboard: InlineKeyboardMarkup) -> None:
    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    except TelegramBadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise
    await callback.answer()


async def _reject(callback: CallbackQuery) -> bool:
    if _allowed(callback.from_user.id):
        return False
    await callback.answer("⛔ Доступ запрещён", show_alert=True)
    return True


def _provider_status(item: AIProvider) -> str:
    return (
        f"<b>{html.escape(item.name)}</b>\n"
        f"Тип: {_KINDS.get(item.kind, html.escape(item.kind))}\n"
        f"Base URL: <code>{html.escape(item.base_url)}</code>\n"
        f"Модель по умолчанию: <code>{html.escape(item.default_model)}</code>\n"
        "API ключ: <b>сохранён</b>\n"
        f"Тип обращения: <code>{html.escape(item.request_type)}</code>"
    )


@router.callback_query(F.data == "ai_model_settings")
async def model_settings(callback: CallbackQuery, state: FSMContext) -> None:
    if await _reject(callback):
        return
    await state.clear()
    await _edit(callback, "🧠 <b>Настройка модели</b>\n\nКлючи, провайдеры и параметры нейрочата.", get_ai_model_settings_keyboard())


@router.callback_query(F.data == "ai_api_keys")
async def api_keys(callback: CallbackQuery, state: FSMContext) -> None:
    if await _reject(callback):
        return
    await state.clear()
    await _edit(callback, "🔑 <b>API ключи</b>\n\nВыберите провайдера. Ключи в меню не отображаются.", get_ai_api_keys_keyboard())


@router.callback_query(F.data == "ai_custom_menu")
async def custom_menu(callback: CallbackQuery, state: FSMContext) -> None:
    if await _reject(callback):
        return
    await state.clear()
    await _edit(callback, "🔌 <b>Свой провайдер</b>\n\nOpenAI-совместимый Chat Completions API.", get_ai_custom_menu_keyboard())


@router.callback_query(F.data == "ai_builtin_openrouter")
async def builtin_openrouter(callback: CallbackQuery, state: FSMContext) -> None:
    if await _reject(callback):
        return
    await state.clear()
    async with session_scope() as session:
        stored = await InstanceSettingsRepository.has_stored_key(session)
    configured = stored or bool(OPENROUTER_API_KEY)
    await _edit(
        callback,
        "🔑 <b>OpenRouter</b>\n\n"
        f"Статус: <b>{'ключ задан' if configured else 'ключ не задан'}</b>"
        + (" (сохранён в боте)" if stored else " (из .env)" if configured else "")
        + "\nДля нового ключа требуется OPENROUTER_KEY_ENCRYPTION_KEY в .env.",
        _markup(("✏️ Изменить API ключ", "ai_openrouter_set"), ("⬅️ К API ключам", "ai_api_keys")),
    )


@router.callback_query(F.data == "ai_openrouter_set")
async def openrouter_set(callback: CallbackQuery, state: FSMContext) -> None:
    if await _reject(callback):
        return
    if not _encryption_ready(openrouter=True):
        await _edit(callback, "Сначала задайте корректный OPENROUTER_KEY_ENCRYPTION_KEY в .env и перезапустите бот.", get_context_back_keyboard("ai_builtin_openrouter"))
        return
    await state.set_state(AIProviderFSM.openrouter_key)
    await _edit(
        callback,
        "Отправьте новый ключ OpenRouter одним сообщением. Бот удалит сообщение после чтения, если сможет.",
        get_context_back_keyboard("ai_builtin_openrouter"),
    )


@router.message(AIProviderFSM.openrouter_key)
async def save_openrouter_key(message: Message, state: FSMContext) -> None:
    if not _allowed(message.from_user.id):
        return
    raw = (message.text or "").strip()
    with suppress(Exception):
        await message.delete()
    if not raw or len(raw) > 4096 or any(ord(c) < 32 for c in raw):
        await message.answer("Ключ пустой или содержит недопустимые символы.")
        return
    if not _encryption_ready(openrouter=True):
        await message.answer("Сначала задайте корректный OPENROUTER_KEY_ENCRYPTION_KEY в .env и перезапустите бот.")
        return
    async with session_scope() as session:
        await InstanceSettingsRepository.set_openrouter_key(session, raw)
    await state.clear()
    await message.answer("✅ Ключ OpenRouter сохранён.", reply_markup=get_context_back_keyboard("ai_builtin_openrouter"))


async def _built_in(callback: CallbackQuery, state: FSMContext, kind: str) -> None:
    if await _reject(callback):
        return
    await state.clear()
    async with session_scope() as session:
        item = (await session.execute(select(AIProvider).where(AIProvider.kind == kind).order_by(AIProvider.id))).scalars().first()
    if item:
        await _edit(callback, _provider_status(item), _markup(("⚙️ Управление", f"ai_prov_view_{item.id}"), ("⬅️ К API ключам", "ai_api_keys")))
    else:
        await _edit(
            callback,
            f"🔑 <b>{_KINDS[kind]}</b>\n\nПрофиль ещё не настроен.",
            _markup(("➕ Подключить", f"ai_provider_create_{kind}"), ("⬅️ К API ключам", "ai_api_keys")),
        )


@router.callback_query(F.data == "ai_builtin_openai")
async def builtin_openai(callback: CallbackQuery, state: FSMContext) -> None:
    await _built_in(callback, state, "openai")


@router.callback_query(F.data == "ai_builtin_deepseek")
async def builtin_deepseek(callback: CallbackQuery, state: FSMContext) -> None:
    await _built_in(callback, state, "deepseek")


@router.callback_query(F.data.regexp(r"^ai_provider_create_(openai|deepseek)$"))
@router.callback_query(F.data == "ai_provider_new")
async def provider_create(callback: CallbackQuery, state: FSMContext) -> None:
    if await _reject(callback):
        return
    if not _encryption_ready():
        await _edit(callback, "Сначала задайте корректный AI_PROVIDER_ENCRYPTION_KEY или OPENROUTER_KEY_ENCRYPTION_KEY в .env и перезапустите бот.", get_context_back_keyboard("ai_api_keys"))
        return
    kind = callback.data.removeprefix("ai_provider_create_") if callback.data != "ai_provider_new" else "custom"
    await state.clear()
    await state.update_data(kind=kind)
    if kind == "custom":
        await state.set_state(AIProviderFSM.new_name)
        text = "Введите название провайдера (до 80 символов)."
        back = "ai_custom_menu"
    else:
        await state.update_data(name=_KINDS[kind], base_url="")
        await state.set_state(AIProviderFSM.new_model)
        text = f"Введите идентификатор модели для {_KINDS[kind]}."
        back = f"ai_builtin_{kind}"
    await _edit(callback, text, get_context_back_keyboard(back))


@router.message(AIProviderFSM.new_name)
async def provider_name(message: Message, state: FSMContext) -> None:
    if not _allowed(message.from_user.id):
        return
    name = (message.text or "").strip()
    if not name or len(name) > 80 or any(ord(c) < 32 for c in name) or name.casefold() in {"openai", "deepseek", "openrouter"}:
        await message.answer("Название должно содержать 1–80 символов и отличаться от встроенных провайдеров.")
        return
    await state.update_data(name=name)
    await state.set_state(AIProviderFSM.new_url)
    await message.answer("Отправьте Base URL OpenAI-совместимого API (HTTPS). Для локального адреса допускается HTTP.", reply_markup=get_context_back_keyboard("ai_custom_menu"))


@router.message(AIProviderFSM.new_url)
async def provider_url(message: Message, state: FSMContext) -> None:
    if not _allowed(message.from_user.id):
        return
    url = (message.text or "").strip()
    from utils.openai_compatible import validate_base_url
    try:
        validated = validate_base_url(url, allow_loopback_http=True)
    except ValueError:
        await message.answer("Некорректный Base URL. Нужен безопасный адрес API, например https://api.example.com/v1.")
        return
    await state.update_data(base_url=validated)
    await state.set_state(AIProviderFSM.new_model)
    await message.answer("Введите идентификатор модели (как у поставщика API).", reply_markup=get_context_back_keyboard("ai_custom_menu"))


@router.message(AIProviderFSM.new_model)
async def provider_model(message: Message, state: FSMContext) -> None:
    if not _allowed(message.from_user.id):
        return
    model = (message.text or "").strip()
    if not model or len(model) > 255 or any(ord(c) < 32 for c in model):
        await message.answer("Модель должна содержать 1–255 символов без управляющих символов.")
        return
    await state.update_data(default_model=model)
    await state.set_state(AIProviderFSM.new_key)
    await message.answer("Отправьте API ключ одним сообщением. Бот удалит сообщение после чтения, если сможет.", reply_markup=get_context_back_keyboard("ai_api_keys"))


@router.message(AIProviderFSM.new_key)
async def provider_key(message: Message, state: FSMContext) -> None:
    if not _allowed(message.from_user.id):
        return
    key = (message.text or "").strip()
    with suppress(Exception):
        await message.delete()
    data = await state.get_data()
    if not key or len(key) > 4096 or any(ord(c) < 32 for c in key):
        await message.answer("Ключ пустой или содержит недопустимые символы.")
        return
    try:
        async with session_scope() as session:
            item = await create_provider(
                session, name=data["name"], kind=data["kind"],
                base_url=data["base_url"], default_model=data["default_model"], api_key=key,
            )
    except (ValueError, KeyError, IntegrityError) as exc:
        if "ENCRYPTION_KEY" in str(exc):
            await message.answer("Для хранения ключа задайте корректный AI_PROVIDER_ENCRYPTION_KEY или OPENROUTER_KEY_ENCRYPTION_KEY в .env и перезапустите бот.")
        else:
            await message.answer("Не удалось создать профиль: проверьте название, URL и модель.")
        return
    await state.clear()
    await message.answer("✅ Провайдер сохранён.", reply_markup=_markup(("Открыть профиль", f"ai_prov_view_{item.id}"), ("⬅️ К API ключам", "ai_api_keys")))


@router.callback_query(F.data.regexp(r"^ai_provider_list_(\d+)$"))
async def provider_list(callback: CallbackQuery, state: FSMContext) -> None:
    if await _reject(callback):
        return
    await state.clear()
    page = min(int(callback.data.split("_")[-1]), 100000)
    async with session_scope() as session:
        query = select(AIProvider).where(AIProvider.kind == "custom").order_by(AIProvider.id)
        items = (await session.execute(query.offset(page * _PAGE_SIZE).limit(_PAGE_SIZE))).scalars().all()
        if page and not items:
            page -= 1
            items = (await session.execute(query.offset(page * _PAGE_SIZE).limit(_PAGE_SIZE))).scalars().all()
        more = (await session.execute(query.offset((page + 1) * _PAGE_SIZE).limit(1))).scalars().first()
    rows = [[_button(f"🔌 {item.name[:45]}", f"ai_prov_view_{item.id}")] for item in items]
    if not rows:
        rows.append([_button("Нет провайдеров", "ai_custom_menu")])
    nav = []
    if page:
        nav.append(_button("◀️", f"ai_provider_list_{page - 1}"))
    if more:
        nav.append(_button("▶️", f"ai_provider_list_{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([_button("⬅️ К своему провайдеру", "ai_custom_menu")])
    await _edit(callback, f"📋 <b>Провайдеры</b> · страница {page + 1}", InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.regexp(r"^ai_prov_view_(\d+)$"))
async def provider_view(callback: CallbackQuery, state: FSMContext) -> None:
    if await _reject(callback):
        return
    await state.clear()
    provider_id = int(callback.data.split("_")[-1])
    async with session_scope() as session:
        item = await get_provider(session, provider_id)
    if item is None:
        await callback.answer("Провайдер не найден", show_alert=True)
        return
    keyboard = _markup(
        ("🔑 Изменить API", f"ai_prov_edit_{provider_id}_key"),
        ("🌐 Изменить Base URL", f"ai_prov_edit_{provider_id}_url"),
        ("🧠 Изменить модель", f"ai_prov_edit_{provider_id}_model"),
        ("🔌 Тип обращения", f"ai_prov_type_{provider_id}"),
        ("🧩 Редактировать config", f"ai_prov_edit_{provider_id}_config"),
        ("⬅️ К провайдерам", "ai_provider_list_0"),
    )
    await _edit(callback, _provider_status(item), keyboard)


@router.callback_query(F.data.regexp(r"^ai_prov_type_(\d+)$"))
async def provider_type(callback: CallbackQuery, state: FSMContext) -> None:
    if await _reject(callback):
        return
    provider_id = int(callback.data.split("_")[-1])
    await _edit(callback, "Сейчас поддерживается тип обращения <code>chat_completions</code>. Другие типы пока не подключены.", get_context_back_keyboard(f"ai_prov_view_{provider_id}"))


@router.callback_query(F.data.regexp(r"^ai_prov_edit_(\d+)_(key|url|model|config)$"))
async def provider_edit(callback: CallbackQuery, state: FSMContext) -> None:
    if await _reject(callback):
        return
    _, _, _, pid, field = callback.data.split("_")
    provider_id = int(pid)
    if field == "key" and not _encryption_ready():
        await _edit(callback, "Сначала задайте корректный ключ шифрования AI_PROVIDER_ENCRYPTION_KEY или OPENROUTER_KEY_ENCRYPTION_KEY в .env.", get_context_back_keyboard(f"ai_prov_view_{provider_id}"))
        return
    async with session_scope() as session:
        item = await get_provider(session, provider_id)
    if item is None:
        await callback.answer("Провайдер не найден", show_alert=True)
        return
    await state.set_state(AIProviderFSM.edit_value)
    await state.update_data(provider_id=provider_id, field=field)
    if field == "key":
        text = "Отправьте новый API ключ. Бот удалит сообщение после чтения, если сможет."
    elif field == "url":
        text = f"Текущий Base URL: <code>{html.escape(item.base_url)}</code>\nОтправьте новый URL."
    elif field == "model":
        text = f"Текущая модель: <code>{html.escape(item.default_model)}</code>\nОтправьте новую модель."
    else:
        current = json.dumps(json.loads(item.config_json or "{}"), ensure_ascii=False, indent=2)
        text = (
            "Отправьте JSON с разрешёнными полями <code>headers</code> и <code>generation</code>. "
            "Можно менять только указанные поля, произвольные строки и команды не выполняются.\n\n"
            f"Текущий config:\n<pre>{html.escape(current[:3000])}</pre>"
        )
    await _edit(callback, text, get_context_back_keyboard(f"ai_prov_view_{provider_id}"))


@router.message(AIProviderFSM.edit_value)
async def provider_save_edit(message: Message, state: FSMContext) -> None:
    if not _allowed(message.from_user.id):
        return
    data = await state.get_data()
    provider_id, field = data.get("provider_id"), data.get("field")
    if not isinstance(provider_id, int) or field not in {"key", "url", "model", "config"}:
        await state.clear()
        await message.answer("Сессия устарела.")
        return
    value = (message.text or "").strip()
    if field == "key":
        with suppress(Exception):
            await message.delete()
    if not value:
        await message.answer("Отправьте непустое значение.")
        return
    kwargs = {"key": {"api_key": value}, "url": {"base_url": value}, "model": {"default_model": value}, "config": {"config": value}}[field]
    try:
        async with session_scope() as session:
            await update_provider(session, provider_id, **kwargs)
    except ValueError as exc:
        if "ENCRYPTION_KEY" in str(exc):
            await message.answer("Нужен корректный AI_PROVIDER_ENCRYPTION_KEY или OPENROUTER_KEY_ENCRYPTION_KEY в .env.")
        else:
            await message.answer("Значение не принято. Проверьте формат URL, модели или JSON config.")
        return
    await state.clear()
    await message.answer("✅ Изменение сохранено.", reply_markup=get_context_back_keyboard(f"ai_prov_view_{provider_id}"))


@router.callback_query(F.data.regexp(r"^ai_default_providers_(\d+)$"))
async def default_providers(callback: CallbackQuery, state: FSMContext) -> None:
    if await _reject(callback):
        return
    await state.clear()
    page = min(int(callback.data.split("_")[-1]), 100000)
    async with session_scope() as session:
        settings = await session.get(InstanceSettings, 1)
        items = await list_providers(session, page=page, page_size=_PAGE_SIZE)
        if page and not items:
            page -= 1
            items = await list_providers(session, page=page, page_size=_PAGE_SIZE)
        more = await list_providers(session, page=page + 1, page_size=_PAGE_SIZE)
    selected = settings.default_ai_provider_id if settings else None
    rows = [[_button(("✅ " if selected is None else "") + "OpenRouter", "ai_default_set_0")]]
    rows += [[_button(("✅ " if selected == item.id else "") + item.name[:43], f"ai_default_set_{item.id}")] for item in items]
    nav = []
    if page:
        nav.append(_button("◀️", f"ai_default_providers_{page - 1}"))
    if more:
        nav.append(_button("▶️", f"ai_default_providers_{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([_button("⬅️ К настройке модели", "ai_model_settings")])
    await _edit(callback, "🌐 <b>Провайдер по умолчанию</b>\n\nИспользуется в общих сценариях ИИ и назначается новым рассылкам. Уже созданные рассылки сохраняют свой выбор.", InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.regexp(r"^ai_default_set_(\d+)$"))
async def default_set(callback: CallbackQuery, state: FSMContext) -> None:
    if await _reject(callback):
        return
    provider_id = int(callback.data.split("_")[-1])
    try:
        async with session_scope() as session:
            await select_default_provider(session, provider_id or None)
    except ValueError:
        await callback.answer("Провайдер не найден", show_alert=True)
        return
    await state.clear()
    await _edit(callback, "✅ Провайдер по умолчанию обновлён.", get_context_back_keyboard("ai_default_providers_0"))


@router.callback_query(F.data.regexp(r"^ai_mailing_select_(\d+)$"))
async def mailing_select(callback: CallbackQuery, state: FSMContext) -> None:
    if await _reject(callback):
        return
    await state.clear()
    page = min(int(callback.data.split("_")[-1]), 100000)
    async with session_scope() as session:
        total = await session.scalar(select(func.count(Mailing.id))) or 0
        page = min(page, max(0, (total - 1) // 10))
        mailings = (await session.execute(select(Mailing).order_by(Mailing.id).offset(page * 10).limit(10))).scalars().all()
    rows = [[_button(f"📋 {(m.name or '#'+str(m.id))[:45]}", f"ai_mailing_providers_{m.id}_0")] for m in mailings]
    if not rows:
        rows.append([_button("Нет рассылок", "ai_model_settings")])
    nav = []
    if page:
        nav.append(_button("◀️", f"ai_mailing_select_{page - 1}"))
    if (page + 1) * 10 < total:
        nav.append(_button("▶️", f"ai_mailing_select_{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([_button("⬅️ К настройке модели", "ai_model_settings")])
    await _edit(callback, "📋 <b>Выберите рассылку</b> для назначения провайдера.", InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.regexp(r"^ai_mailing_providers_(\d+)_(\d+)$"))
async def mailing_providers(callback: CallbackQuery, state: FSMContext) -> None:
    if await _reject(callback):
        return
    await state.clear()
    parts = callback.data.split("_")
    mailing_id, page = int(parts[3]), min(int(parts[4]), 100000)
    async with session_scope() as session:
        mailing = await session.get(Mailing, mailing_id)
        if mailing is None:
            await callback.answer("Рассылка не найдена", show_alert=True)
            return
        items = await list_providers(session, page=page, page_size=_PAGE_SIZE)
        if page and not items:
            page -= 1
            items = await list_providers(session, page=page, page_size=_PAGE_SIZE)
        more = await list_providers(session, page=page + 1, page_size=_PAGE_SIZE)
    selected = mailing.neuro_provider_id
    rows = [[_button(("✅ " if selected is None else "") + "OpenRouter", f"ai_mailing_set_{mailing_id}_0")]]
    rows += [[_button(("✅ " if selected == p.id else "") + p.name[:43], f"ai_mailing_set_{mailing_id}_{p.id}")] for p in items]
    nav = []
    if page:
        nav.append(_button("◀️", f"ai_mailing_providers_{mailing_id}_{page - 1}"))
    if more:
        nav.append(_button("▶️", f"ai_mailing_providers_{mailing_id}_{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([_button("⬅️ К рассылкам", "ai_mailing_select_0")])
    await _edit(callback, f"🔌 <b>Провайдер рассылки</b> · {html.escape(mailing.name or str(mailing_id))}\nВыберите профиль. Модель переключится на значение по умолчанию выбранного провайдера.", InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.regexp(r"^ai_mailing_set_(\d+)_(\d+)$"))
async def mailing_set(callback: CallbackQuery, state: FSMContext) -> None:
    if await _reject(callback):
        return
    parts = callback.data.split("_")
    mailing_id, provider_id = int(parts[3]), int(parts[4])
    try:
        async with session_scope() as session:
            await select_mailing_provider(session, mailing_id, provider_id or None)
    except ValueError:
        await callback.answer("Рассылка или провайдер не найден", show_alert=True)
        return
    await state.clear()
    await _edit(
        callback,
        "✅ Провайдер рассылки обновлён. Модель будет взята из выбранного профиля.",
        get_context_back_keyboard(f"ai_mailing_providers_{mailing_id}_0", "⬅️ К провайдерам"),
    )


@router.callback_query(F.data == "ai_sampling_intro")
async def sampling_intro(callback: CallbackQuery, state: FSMContext) -> None:
    if await _reject(callback):
        return
    await state.clear()
    text = (
        "🎛 <b>Параметры сэмплирования</b>\n\n"
        "<b>Temperature</b> — разнообразие ответов; выше значит менее предсказуемо.\n"
        "<b>Top P</b> — доля вероятных вариантов следующего слова.\n"
        "<b>Top K</b> — число рассматриваемых вариантов.\n"
        "<b>Min P / Top A</b> — дополнительные пороги отбора вариантов.\n"
        "<b>Frequency penalty</b> — уменьшает повторы слов.\n"
        "<b>Presence penalty</b> — помогает сменить тему.\n"
        "<b>Repetition penalty</b> — уменьшает повторение фраз.\n"
        "<b>Max tokens</b> — верхняя граница длины ответа.\n\n"
        "Поддержка зависит от провайдера и модели. Настройки задаются для каждой рассылки."
    )
    await _edit(callback, text, _markup(("📋 Выбрать рассылку", "ai_sampling_mailings_0"), ("⬅️ К настройке модели", "ai_model_settings")))


async def _pick_mailing(callback: CallbackQuery, state: FSMContext, mode: str) -> None:
    if await _reject(callback):
        return
    await state.clear()
    page = min(int(callback.data.split("_")[-1]), 100000)
    async with session_scope() as session:
        total = await session.scalar(select(func.count(Mailing.id))) or 0
        page = min(page, max(0, (total - 1) // 10))
        items = (await session.execute(select(Mailing).order_by(Mailing.id).offset(page * 10).limit(10))).scalars().all()
    target = "mailing_neuro_sampling" if mode == "sampling" else "mailing_neuro_prompt"
    prefix = "ai_sampling_mailings" if mode == "sampling" else "ai_prompt_mailings"
    rows = [[_button((m.name or f"#{m.id}")[:48], f"{target}_{m.id}")] for m in items]
    if not rows:
        rows.append([_button("Нет рассылок", "menu_ai")])
    nav = []
    if page:
        nav.append(_button("◀️", f"{prefix}_{page - 1}"))
    if (page + 1) * 10 < total:
        nav.append(_button("▶️", f"{prefix}_{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([_button("⬅️ Назад", "ai_sampling_intro" if mode == "sampling" else "ai_prompt_settings")])
    title = "параметров" if mode == "sampling" else "промпта"
    await _edit(callback, f"Выберите рассылку для настройки {title}.", InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.regexp(r"^ai_sampling_mailings_(\d+)$"))
async def sampling_mailings(callback: CallbackQuery, state: FSMContext) -> None:
    await _pick_mailing(callback, state, "sampling")


@router.callback_query(F.data == "ai_prompt_settings")
async def prompt_settings(callback: CallbackQuery, state: FSMContext) -> None:
    if await _reject(callback):
        return
    await state.clear()
    await _edit(callback, "📄 <b>Настройка промпта</b>\n\nСоздайте или замените system.txt для выбранной рассылки. История версий доступна в веб-панели.", _markup(("➕ Создать промпт", "ai_prompt_mailings_0"), ("⬅️ К ИИ", "menu_ai")))


@router.callback_query(F.data.regexp(r"^ai_prompt_mailings_(\d+)$"))
async def prompt_mailings(callback: CallbackQuery, state: FSMContext) -> None:
    await _pick_mailing(callback, state, "prompt")
