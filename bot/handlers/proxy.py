"""
Хендлеры для управления прокси.
Добавление, просмотр списка, карточка прокси, редактирование, проверка, удаление.
"""
import re
from io import BytesIO

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery

from bot.config import OWNER_ID
from bot.keyboards.main import (
    get_proxy_keyboard,
    get_proxy_list_keyboard,
    get_proxy_card_keyboard,
    get_edit_proxy_keyboard,
    get_confirm_delete_proxy_keyboard,
    get_context_back_keyboard,
    get_cancel_with_back_keyboard,
    get_proxy_groups_keyboard,
    get_proxy_group_card_keyboard,
    get_proxy_group_delete_confirm_keyboard,
    PROXY_LIST_PAGE_SIZE,
)
from database.session import session_scope
from database.models import ProxyType
from database.repositories import ProxyRepository, AccountRepository, ProxyGroupRepository
from utils.logger import log
from utils.time import utcnow_naive

router = Router()


# ==================== FSM Состояния ====================

class ProxyAdd(StatesGroup):
    """Состояния для добавления прокси."""
    waiting_for_name = State()
    waiting_for_proxy = State()


class ProxyEdit(StatesGroup):
    """Состояния для редактирования прокси."""
    waiting_for_name = State()
    waiting_for_data = State()


class ProxyBulkAdd(StatesGroup):
    """Состояния массового добавления прокси."""
    waiting_for_lines = State()
    waiting_for_group_name = State()


# ==================== Главное меню прокси ====================

@router.callback_query(F.data == "menu_proxy")
async def cb_proxy_menu(callback: CallbackQuery):
    """Главное меню раздела Прокси."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await callback.message.edit_text(
        "🌐 <b>Управление прокси</b>\n\n"
        "Добавляйте прокси и привязывайте их к аккаунтам.\n"
        "Формат: user:pass@host:port",
        reply_markup=get_proxy_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


# ==================== Добавление прокси ====================

@router.callback_query(F.data == "proxy_add")
async def cb_proxy_add(callback: CallbackQuery, state: FSMContext):
    """Начало добавления прокси."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await callback.message.edit_text(
        "🌐 <b>Добавление прокси</b>\n\n"
        "Введите <b>название прокси</b> (например: usa1, ger2):\n\n"
        "❌ Отмена: /start",
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(ProxyAdd.waiting_for_name)
    await callback.answer()


@router.callback_query(F.data == "proxy_bulk_add")
async def cb_proxy_bulk_add(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await callback.message.edit_text(
        "📥 <b>Массовое добавление прокси</b>\n\n"
        "Пришлите <b>файл .txt</b> со списком прокси — по одному в строке.\n\n"
        "Поддерживаемые форматы строк:\n"
        "• <code>user:pass@host:port</code>\n"
        "• <code>host:port</code>\n\n"
        "Так список не разбивается на несколько сообщений и не перемешивается.\n\n"
        "❌ Отмена: /start",
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(ProxyBulkAdd.waiting_for_lines)
    await callback.answer()


@router.message(ProxyAdd.waiting_for_name)
async def process_name(message: Message, state: FSMContext):
    """Обработка имени прокси."""
    if message.from_user.id != OWNER_ID:
        return

    name = message.text.strip()

    # Проверка на отмену
    if name.lower() in ('/start', 'отмена', 'cancel'):
        await state.clear()
        await message.answer("❌ Добавление прокси отменено.", reply_markup=get_proxy_keyboard())
        return

    # Проверка на дубликат
    async with session_scope() as session:
        existing = await ProxyRepository.get_by_name(session, name)

    if existing:
        await message.answer("❌ Прокси с таким именем уже существует.\n\nВведите другое имя:")
        return

    await state.update_data(name=name)
    await state.set_state(ProxyAdd.waiting_for_proxy)

    await message.answer(
        "🌐 <b>Введите данные прокси</b>\n\n"
        "Формат: <code>username:password@host:port</code>\n"
        "Тип: SOCKS5\n\n"
        "Пример: <code>user123:pass456@1.2.3.4:1080</code>\n\n"
        "❌ Отмена: /start",
        parse_mode=ParseMode.HTML,
    )


@router.message(ProxyAdd.waiting_for_proxy)
async def process_proxy(message: Message, state: FSMContext):
    """Обработка данных прокси."""
    if message.from_user.id != OWNER_ID:
        return

    proxy_str = message.text.strip()

    # Проверка на отмену
    if proxy_str.lower() in ('/start', 'отмена', 'cancel'):
        await state.clear()
        await message.answer("❌ Добавление прокси отменено.")
        return

    # Парсинг: user:pass@host:port
    pattern = r'^([^:]+):([^@]+)@([^:]+):(\d+)$'
    match = re.match(pattern, proxy_str)

    if not match:
        await message.answer(
            "❌ Неверный формат.\n\n"
            "Используйте: <code>username:password@host:port</code>\n"
            "Пример: <code>user123:pass456@1.2.3.4:1080</code>\n\n"
            "Попробуйте ещё раз:",
            parse_mode=ParseMode.HTML,
        )
        return

    username, password, host, port = match.groups()

    data = await state.get_data()
    name = data.get('name')

    # Сохранение в БД
    async with session_scope() as session:
        await ProxyRepository.create(
            session=session,
            name=name,
            host=host,
            port=int(port),
            username=username,
            password=password,
            proxy_type=ProxyType.SOCKS5,
        )

    await state.clear()

    await message.answer(
        f"✅ <b>Прокси добавлен!</b>\n\n"
        f"📋 Имя: {name}\n"
        f"🌍 Хост: {host}:{port}\n"
        f"👤 Логин: {username}\n\n"
        "Теперь вы можете привязать его к аккаунту.",
        reply_markup=get_context_back_keyboard("proxy_list"),
        parse_mode=ParseMode.HTML,
    )

    log.info(f"Добавлен прокси: {name}")


def _parse_proxy_line(line: str) -> tuple[str, str, int, str | None, str | None] | None:
    s = line.strip()
    if not s:
        return None
    # user:pass@host:port
    m_auth = re.match(r"^([^:\s]+):([^@\s]+)@([^:\s]+):(\d+)$", s)
    if m_auth:
        user, pwd, host, port = m_auth.groups()
        return host, user, int(port), user, pwd
    # host:port
    m_plain = re.match(r"^([^:\s]+):(\d+)$", s)
    if m_plain:
        host, port = m_plain.groups()
        return host, host, int(port), None, None
    return None


@router.message(ProxyBulkAdd.waiting_for_lines, F.document)
async def process_bulk_proxy_document(message: Message, state: FSMContext, bot: Bot):
    if message.from_user.id != OWNER_ID:
        return
    doc = message.document
    fname = (doc.file_name or "").lower()
    if not fname.endswith(".txt"):
        await message.answer(
            "Нужен файл с расширением <b>.txt</b>.\n"
            "Если файл в другом формате, переименуйте или сохраните как текстовый.",
            parse_mode=ParseMode.HTML,
        )
        return
    max_bytes = 15 * 1024 * 1024
    if doc.file_size and doc.file_size > max_bytes:
        await message.answer("Файл слишком большой (макс. 15 МБ).")
        return
    buf = BytesIO()
    await bot.download(doc, destination=buf)
    raw = buf.getvalue()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")

    lines = [x.strip() for x in text.splitlines() if x.strip()]
    if not lines:
        await message.answer("В файле нет ни одной непустой строки с прокси.")
        return
    await state.update_data(proxy_lines=lines)
    await state.set_state(ProxyBulkAdd.waiting_for_group_name)
    await message.answer(
        f"Принято строк: <b>{len(lines)}</b>.\n\n"
        "Введите название группы прокси (например: <code>USA</code>):",
        parse_mode=ParseMode.HTML,
    )


@router.message(ProxyBulkAdd.waiting_for_lines)
async def process_bulk_proxy_lines_not_document(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return
    text_cmd = (message.text or "").strip().lower()
    if text_cmd in ("/start", "cancel", "отмена"):
        await state.clear()
        await message.answer("❌ Добавление прокси отменено.", reply_markup=get_proxy_keyboard())
        return
    await message.answer(
        "Пришлите <b>.txt</b> файл со списком прокси (по одному в строке).\n\n"
        "Сообщения с текстом не принимаются — из‑за лимита Telegram длинный список "
        "разбивается на части и строки могут перемешаться.",
        parse_mode=ParseMode.HTML,
    )


@router.message(ProxyBulkAdd.waiting_for_group_name)
async def process_bulk_proxy_group(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return
    group_name = (message.text or "").strip()
    if not group_name:
        await message.answer("Введите непустое название группы.")
        return

    data = await state.get_data()
    lines = data.get("proxy_lines", [])
    added = 0
    skipped = 0
    bad = 0

    async with session_scope() as session:
        group = await ProxyGroupRepository.get_or_create(session, group_name)
        existing = await ProxyRepository.get_all(session)
        existing_keys = {(p.host, int(p.port), p.username or "", p.password or "") for p in existing}

        for i, raw in enumerate(lines, start=1):
            parsed = _parse_proxy_line(raw)
            if not parsed:
                bad += 1
                continue
            host, default_name, port, username, password = parsed
            key = (host, int(port), username or "", password or "")
            if key in existing_keys:
                skipped += 1
                continue
            name = f"{group.name.lower()}_{i}_{host}:{port}"
            await ProxyRepository.create(
                session=session,
                name=name[:100],
                host=host,
                port=int(port),
                username=username,
                password=password,
                group_id=group.id,
                proxy_type=ProxyType.SOCKS5,
            )
            existing_keys.add(key)
            added += 1

    await state.clear()
    await message.answer(
        f"✅ Массовый импорт завершён.\n\n"
        f"Группа: <b>{group_name}</b>\n"
        f"Добавлено: <b>{added}</b>\n"
        f"Пропущено дубликатов: <b>{skipped}</b>\n"
        f"Ошибочных строк: <b>{bad}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=get_proxy_keyboard(),
    )


# ==================== Список прокси ====================

@router.callback_query(F.data == "proxy_groups")
async def cb_proxy_groups(callback: CallbackQuery):
    """Список групп прокси c used/total/free."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    async with session_scope() as session:
        groups_usage = await ProxyGroupRepository.list_with_usage(session)
    await callback.message.edit_text(
        "📂 <b>Группы прокси</b>\n\n"
        "Выберите группу для просмотра состава и занятости.",
        reply_markup=get_proxy_groups_keyboard(groups_usage),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("proxy_group_view_"))
async def cb_proxy_group_view(callback: CallbackQuery):
    """Карточка группы прокси."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    group_id = int(callback.data.split("_")[-1])

    async with session_scope() as session:
        group = await ProxyGroupRepository.get_by_id(session, group_id)
        if not group:
            await callback.answer("Группа не найдена", show_alert=True)
            return
        proxies = await ProxyRepository.get_by_group(session, group_id)
        lines = [
            f"📂 <b>Группа:</b> {group.name}",
            "",
            f"Всего прокси: <b>{len(proxies)}</b>",
        ]
        used = 0
        counts_by_proxy: dict[int, int] = {}
        for p in proxies:
            c = await AccountRepository.count_by_proxy_id(session, p.id)
            counts_by_proxy[p.id] = c
            if c > 0:
                used += 1
        lines.append(f"Занято: <b>{used}</b> · Свободно: <b>{max(0, len(proxies)-used)}</b>")
        if proxies:
            lines.append("")
            lines.append("<b>Состав (до 25):</b>")
            for p in proxies[:25]:
                c = counts_by_proxy.get(p.id, 0)
                status = "🟢 free" if c == 0 else f"🔒 busy x{c}"
                lines.append(f"• <code>{p.host}:{p.port}</code> — {status}")
            if len(proxies) > 25:
                lines.append(f"... и еще {len(proxies)-25}")

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=get_proxy_group_card_keyboard(group_id),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


def _proxy_list_page_from_data(data: str) -> int:
    if data == "proxy_list":
        return 0
    if data.startswith("proxy_list_p_"):
        return int(data.rsplit("_", 1)[-1])
    return 0


@router.callback_query(F.data == "proxy_list_page_info")
async def cb_proxy_list_page_info(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔", show_alert=True)
        return
    await callback.answer("Номер страницы · листайте ◀ ▶", show_alert=True)


@router.callback_query(F.data == "proxy_list")
@router.callback_query(F.data.startswith("proxy_list_p_"))
async def cb_proxy_list(callback: CallbackQuery):
    """Список прокси в виде inline-кнопок (с пагинацией)."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    page = _proxy_list_page_from_data(callback.data)

    try:
        async with session_scope() as session:
            proxies = await ProxyRepository.get_all(session)

        if not proxies:
            await callback.message.edit_text(
                "🌐 <b>Список прокси</b>\n\n"
                "Прокси пока нет.\n"
                "Добавьте первый прокси.",
                reply_markup=get_context_back_keyboard("proxy_list"),
                parse_mode=ParseMode.HTML,
            )
            await callback.answer()
            return

        total = len(proxies)
        total_pages = max(1, (total + PROXY_LIST_PAGE_SIZE - 1) // PROXY_LIST_PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))
        start = page * PROXY_LIST_PAGE_SIZE + 1
        end = min((page + 1) * PROXY_LIST_PAGE_SIZE, total)

        await callback.message.edit_text(
            "🌐 <b>Список прокси:</b>\n\n"
            f"Всего: {total}\n"
            f"Страница {page + 1} из {total_pages} · строки {start}–{end}\n\n"
            "Нажмите на прокси для просмотра деталей:",
            reply_markup=get_proxy_list_keyboard(proxies, page=page),
            parse_mode=ParseMode.HTML,
        )
        await callback.answer()

    except Exception as e:
        log.error(f"Ошибка при получении списка прокси: {e}")
        await callback.message.edit_text(
            "⚠️ <b>Ошибка при загрузке списка прокси</b>\n\n"
            "Попробуйте ещё раз.",
            reply_markup=get_context_back_keyboard("proxy_list"),
            parse_mode=ParseMode.HTML,
        )
        await callback.answer()


@router.callback_query(F.data.startswith("proxy_group_delete_do_"))
async def cb_proxy_group_delete_execute(callback: CallbackQuery):
    """Удаление группы прокси и всех её прокси (после подтверждения)."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    group_id = int(callback.data.removeprefix("proxy_group_delete_do_"))

    async with session_scope() as session:
        group = await ProxyGroupRepository.get_by_id(session, group_id)
        if not group:
            await callback.answer("Группа не найдена", show_alert=True)
            return
        in_use = await AccountRepository.count_using_proxy_group(session, group_id)
        if in_use > 0:
            await callback.answer(
                "Сначала отвяжите прокси у аккаунтов",
                show_alert=True,
            )
            return
        n = len(await ProxyRepository.get_by_group(session, group_id))
        ok = await ProxyGroupRepository.delete_with_proxies(session, group_id)

    if ok:
        await callback.message.edit_text(
            f"🗑 Группа <b>{group.name}</b> удалена вместе с <b>{n}</b> прокси.",
            reply_markup=get_proxy_keyboard(),
            parse_mode=ParseMode.HTML,
        )
    else:
        async with session_scope() as session:
            groups_usage = await ProxyGroupRepository.list_with_usage(session)
        await callback.message.edit_text(
            "Не удалось удалить группу.",
            reply_markup=get_proxy_groups_keyboard(groups_usage),
            parse_mode=ParseMode.HTML,
        )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^proxy_group_delete_(\d+)$"))
async def cb_proxy_group_delete_prompt(callback: CallbackQuery):
    """Запрос на удаление группы: предупреждение, если прокси заняты."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    group_id = int(callback.data.rsplit("_", 1)[-1])

    async with session_scope() as session:
        group = await ProxyGroupRepository.get_by_id(session, group_id)
        if not group:
            await callback.answer("Группа не найдена", show_alert=True)
            return
        in_use = await AccountRepository.count_using_proxy_group(session, group_id)
        proxies = await ProxyRepository.get_by_group(session, group_id)
        sample = await AccountRepository.sample_labels_using_proxy_group(session, group_id, limit=5)

    if in_use > 0:
        extra = ""
        if sample:
            extra = "\n\nНапример: " + ", ".join(sample)
            if in_use > len(sample):
                extra += f" … (+{in_use - len(sample)})"
        await callback.message.edit_text(
            "⚠️ <b>Нельзя удалить группу</b>\n\n"
            f"Группа: <b>{group.name}</b>\n"
            f"К прокси из этой группы привязано аккаунтов: <b>{in_use}</b>."
            f"{extra}\n\n"
            "Сначала смените или отвяжите прокси у этих аккаунтов.",
            reply_markup=get_proxy_group_card_keyboard(group_id),
            parse_mode=ParseMode.HTML,
        )
        await callback.answer("Прокси заняты", show_alert=True)
        return

    await callback.message.edit_text(
        "⚠️ <b>Удалить группу прокси?</b>\n\n"
        f"Группа: <b>{group.name}</b>\n"
        f"Будет удалено прокси: <b>{len(proxies)}</b>\n\n"
        "Действие необратимо.",
        reply_markup=get_proxy_group_delete_confirm_keyboard(group_id),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


# ==================== Карточка прокси ====================

@router.callback_query(F.data.startswith("proxy_view_"))
async def cb_proxy_view(callback: CallbackQuery, state: FSMContext):
    """Детальная карточка прокси."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await state.clear()
    try:
        proxy_id = int(callback.data.split("_")[-1])

        async with session_scope() as session:
            proxy = await ProxyRepository.get_by_id(session, proxy_id)

        if not proxy:
            await callback.message.edit_text(
                "❌ Прокси не найден.",
                reply_markup=get_context_back_keyboard("proxy_list"),
                parse_mode=ParseMode.HTML,
            )
            await callback.answer()
            return

        # Считаем количество привязанных аккаунтов
        async with session_scope() as session:
            from sqlalchemy import select, func
            from database.models import Account
            result = await session.execute(
                select(func.count(Account.id)).where(Account.proxy_id == proxy_id)
            )
            accounts_count = result.scalar()

        # Формирование текста карточки
        status_emoji = "✅" if proxy.is_working else "❌"
        status_text = "Работает" if proxy.is_working else "Не работает"

        # Скрываем пароль частично
        auth_info = "Нет"
        if proxy.username and proxy.password:
            pass_display = proxy.password[:2] + "***" if len(proxy.password) > 2 else "***"
            auth_info = f"{proxy.username}:{pass_display}"

        text = (
            f"🌐 <b>Карточка прокси</b>\n\n"
            f"📋 <b>Название:</b> {proxy.name}\n"
            f"🌍 <b>Адрес:</b> {proxy.host}:{proxy.port}\n"
            f"🔐 <b>Тип:</b> {proxy.proxy_type.value}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👤 <b>Авторизация:</b> {auth_info}\n"
            f"📊 <b>Статус:</b> {status_emoji} {status_text}\n"
            f"🔗 <b>Привязано аккаунтов:</b> {accounts_count}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📅 <b>Создан:</b> {proxy.created_at.strftime('%d.%m.%Y %H:%M') if proxy.created_at else 'N/A'}\n"
            f"🕐 <b>Проверен:</b> {proxy.last_checked.strftime('%d.%m.%Y %H:%M') if proxy.last_checked else 'Не проверялся'}\n"
        )

        await callback.message.answer(
            text=text,
            reply_markup=get_proxy_card_keyboard(proxy),
            parse_mode=ParseMode.HTML,
        )
        await callback.answer()

    except Exception as e:
        log.error(f"Ошибка при открытии карточки прокси {proxy_id}: {e}")
        await callback.message.edit_text(
            "⚠️ <b>Ошибка при загрузке карточки прокси</b>\n\n"
            "Попробуйте ещё раз.",
            reply_markup=get_context_back_keyboard("proxy_list"),
            parse_mode=ParseMode.HTML,
        )
        await callback.answer()


# ==================== Редактирование прокси ====================

@router.callback_query(F.data.startswith("proxy_edit_"))
async def cb_proxy_edit(callback: CallbackQuery):
    """Меню редактирования прокси."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    proxy_id = int(callback.data.split("_")[-1])

    await callback.message.edit_text(
        "✏️ <b>Редактирование прокси</b>\n\n"
        "Выберите, что хотите изменить:",
        reply_markup=get_edit_proxy_keyboard(proxy_id),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("proxy_edit_name_"))
async def cb_edit_name(callback: CallbackQuery, state: FSMContext):
    """Изменение названия прокси."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    proxy_id = int(callback.data.split("_")[-1])
    await state.update_data(proxy_id=proxy_id)
    await state.set_state(ProxyEdit.waiting_for_name)

    await callback.message.edit_text(
        "✏️ <b>Изменение названия</b>\n\n"
        "Введите новое название прокси:\n\n"
        "❌ Отмена: /start",
        reply_markup=get_cancel_with_back_keyboard("cancel_proxy", f"proxy_view_{proxy_id}"),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.message(ProxyEdit.waiting_for_name)
async def process_edit_name(message: Message, state: FSMContext):
    """Обработка нового названия."""
    if message.from_user.id != OWNER_ID:
        return

    new_name = message.text.strip()

    # Проверка на отмену
    if new_name.lower() in ('/start', 'отмена', 'cancel'):
        await state.clear()
        await message.answer("❌ Редактирование отменено.")
        return

    data = await state.get_data()
    proxy_id = data.get('proxy_id')

    async with session_scope() as session:
        # Проверка на дубликат
        existing = await ProxyRepository.get_by_name(session, new_name)
        if existing and existing.id != proxy_id:
            await message.answer("❌ Прокси с таким именем уже существует.\n\nВведите другое имя:")
            return

        await ProxyRepository.update(session, proxy_id, name=new_name)
        proxy = await ProxyRepository.get_by_id(session, proxy_id)

    await state.clear()

    log.info(f"Прокси {proxy_id}: название обновлено на '{new_name}'")

    await message.answer(
        f"✅ Название обновлено: {new_name}\n\n"
        "Карточка прокси:",
        reply_markup=get_proxy_card_keyboard(proxy),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data.startswith("proxy_edit_data_"))
async def cb_edit_data(callback: CallbackQuery, state: FSMContext):
    """Изменение данных прокси."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    proxy_id = int(callback.data.split("_")[-1])
    await state.update_data(proxy_id=proxy_id)
    await state.set_state(ProxyEdit.waiting_for_data)

    await callback.message.edit_text(
        "🌐 <b>Изменение данных прокси</b>\n\n"
        "Введите новые данные в формате:\n"
        "<code>username:password@host:port</code>\n\n"
        "❌ Отмена: /start",
        reply_markup=get_cancel_with_back_keyboard("cancel_proxy", f"proxy_view_{proxy_id}"),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.message(ProxyEdit.waiting_for_data)
async def process_edit_data(message: Message, state: FSMContext):
    """Обработка новых данных прокси."""
    if message.from_user.id != OWNER_ID:
        return

    proxy_str = message.text.strip()

    # Проверка на отмену
    if proxy_str.lower() in ('/start', 'отмена', 'cancel'):
        await state.clear()
        await message.answer("❌ Редактирование отменено.")
        return

    # Парсинг: user:pass@host:port
    pattern = r'^([^:]+):([^@]+)@([^:]+):(\d+)$'
    match = re.match(pattern, proxy_str)

    if not match:
        await message.answer(
            "❌ Неверный формат.\n\n"
            "Используйте: <code>username:password@host:port</code>\n"
            "Пример: <code>user123:pass456@1.2.3.4:1080</code>\n\n"
            "Попробуйте ещё раз:",
            parse_mode=ParseMode.HTML,
        )
        return

    username, password, host, port = match.groups()

    data = await state.get_data()
    proxy_id = data.get('proxy_id')

    async with session_scope() as session:
        await ProxyRepository.update(
            session, proxy_id,
            host=host,
            port=int(port),
            username=username,
            password=password,
        )
        proxy = await ProxyRepository.get_by_id(session, proxy_id)

    await state.clear()

    log.info(f"Прокси {proxy_id}: данные обновлены")

    await message.answer(
        "✅ Данные прокси обновлены\n\n"
        "Карточка прокси:",
        reply_markup=get_proxy_card_keyboard(proxy),
        parse_mode=ParseMode.HTML,
    )


# ==================== Проверка прокси ====================

@router.callback_query(F.data.startswith("proxy_check_"))
async def cb_proxy_check(callback: CallbackQuery):
    """Проверка работоспособности прокси."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    proxy_id = int(callback.data.split("_")[-1])

    status_msg = await callback.message.answer("🔄 Проверка прокси...")

    try:
        async with session_scope() as session:
            proxy = await ProxyRepository.get_by_id(session, proxy_id)

        if not proxy:
            await status_msg.edit_text("❌ Прокси не найден.")
            await callback.answer()
            return

        # Проверка через утилиту
        from utils.proxy_checker import check_proxy

        log.info(f"🔍 Проверка прокси {proxy.name} ({proxy.host}:{proxy.port})...")

        is_working, exit_ip = await check_proxy(
            proxy_type=proxy.proxy_type.value,
            host=proxy.host,
            port=proxy.port,
            username=proxy.username,
            password=proxy.password,
            timeout=15,
        )

        # Обновляем статус в БД
        async with session_scope() as session:
            await ProxyRepository.update_status(
                session, proxy_id,
                is_working=is_working,
                last_checked=utcnow_naive(),
            )
            proxy = await ProxyRepository.get_by_id(session, proxy_id)

        status_emoji = "✅" if is_working else "❌"
        status_text = "Работает" if is_working else "Не работает"

        # Формируем ответ
        if is_working:
            result_text = (
                f"🔍 <b>Проверка завершена</b>\n\n"
                f"📋 {proxy.name}\n"
                f"🌍 {proxy.host}:{proxy.port}\n\n"
                f"Статус: {status_emoji} {status_text}\n"
                f"🌐 <b>Exit IP:</b> <code>{exit_ip}</code>\n\n"
                f"⚠️ <b>Внимание:</b> Если IP отличается от ожидаемого,\n"
                f"возможно прокси имеет другой выходной узел."
            )
        else:
            result_text = (
                f"🔍 <b>Проверка завершена</b>\n\n"
                f"📋 {proxy.name}\n"
                f"🌍 {proxy.host}:{proxy.port}\n\n"
                f"Статус: {status_emoji} {status_text}\n\n"
                f"❌ Прокси не ответил или недоступен."
            )

        await status_msg.edit_text(
            result_text,
            reply_markup=get_proxy_card_keyboard(proxy),
            parse_mode=ParseMode.HTML,
        )

    except Exception as e:
        log.error(f"Ошибка проверки прокси {proxy_id}: {e}")
        import traceback
        log.error(traceback.format_exc())
        await status_msg.edit_text(
            f"❌ Ошибка проверки: {e}",
            reply_markup=get_context_back_keyboard("proxy_list"),
        )

    await callback.answer()


# ==================== Удаление прокси ====================

@router.callback_query(F.data.startswith("proxy_delete_confirm_"))
async def cb_delete_confirm(callback: CallbackQuery):
    """Подтверждение удаления прокси."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    proxy_id = int(callback.data.split("_")[-1])

    async with session_scope() as session:
        from sqlalchemy import select, func
        from database.models import Account
        result = await session.execute(
            select(func.count(Account.id)).where(Account.proxy_id == proxy_id)
        )
        accounts_count = result.scalar()

    if accounts_count > 0:
        await callback.message.edit_text(
            "⚠️ <b>Нельзя удалить прокси!</b>\n\n"
            f"К этому прокси привязано <b>{accounts_count}</b> аккаунтов.\n\n"
            "Сначала отвяжите аккаунты или удалите их.",
            reply_markup=get_context_back_keyboard("proxy_list"),
            parse_mode=ParseMode.HTML,
        )
        await callback.answer()
        return

    await callback.message.edit_text(
        "⚠️ <b>Подтверждение удаления</b>\n\n"
        "Вы уверены, что хотите удалить прокси?\n\n"
        "Это действие нельзя отменить.",
        reply_markup=get_confirm_delete_proxy_keyboard(proxy_id),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("proxy_delete_"))
async def cb_delete_proxy(callback: CallbackQuery):
    """Удаление прокси."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    proxy_id = int(callback.data.split("_")[-1])

    async with session_scope() as session:
        proxy = await ProxyRepository.get_by_id(session, proxy_id)

        if proxy:
            await ProxyRepository.delete(session, proxy_id)
            log.info(f"Удалён прокси {proxy_id} ({proxy.name})")

    await callback.message.edit_text(
        f"🗑 Прокси <b>{proxy.name}</b> удалён.",
        reply_markup=get_proxy_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


# ==================== Отмена ====================

@router.callback_query(F.data == "cancel_proxy")
async def cb_cancel_proxy(callback: CallbackQuery, state: FSMContext):
    """Отмена сценария прокси (отдельный callback от аккаунтов/рассылки)."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await state.clear()

    await callback.message.edit_text(
        "❌ <b>Отменено</b>\n\n"
        "Операция отменена.",
        reply_markup=get_proxy_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()
