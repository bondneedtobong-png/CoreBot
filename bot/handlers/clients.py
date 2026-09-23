"""
Хендлеры для управления базой клиентов.
Загрузка TXT, просмотр, очистка.
"""
import re
import json

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import update

from bot.config import OWNER_ID, FILES_DIR
from bot.handlers.accounts.common import safe_edit_message
from bot.keyboards.main import get_clients_keyboard, get_cancel_with_back_keyboard, get_context_back_keyboard
from database.session import session_scope
from database.models import Client, ClientStatus
from database.repositories import ClientRepository
from utils.logger import log

router = Router()


class ClientUpload(StatesGroup):
    """Состояния для загрузки базы клиентов."""
    waiting_for_file = State()
    processing = State()


CLIENT_STATS_FILE = FILES_DIR / "clients_stats_reset.json"


def _clients_dashboard_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📥 Загрузить базу (TXT)", callback_data="clients_upload")],
            [InlineKeyboardButton(text="⬇️ Скачать необработанных (NEW)", callback_data="clients_export_new")],
            [InlineKeyboardButton(text="♻️ Сбросить статистику", callback_data="clients_stats_reset_confirm")],
            [InlineKeyboardButton(text="🗑 Очистить базу", callback_data="clients_clear")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="db_sheet_213")],
        ]
    )


def _load_clients_stats_baseline() -> dict:
    try:
        if CLIENT_STATS_FILE.exists():
            return json.loads(CLIENT_STATS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"processed": 0, "skipped": 0}


def _save_clients_stats_baseline(processed: int, skipped: int) -> None:
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    CLIENT_STATS_FILE.write_text(
        json.dumps({"processed": int(processed), "skipped": int(skipped)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


async def render_clients_dashboard(callback: CallbackQuery) -> None:
    async with session_scope() as session:
        from sqlalchemy import select, func
        total = await session.execute(select(func.count(Client.id)))
        total_count = int(total.scalar() or 0)
        status_result = await session.execute(
            select(Client.status, func.count(Client.id)).group_by(Client.status)
        )
        status_stats = {row[0].value: int(row[1]) for row in status_result.all()}

    contacted = status_stats.get("contacted", 0)
    blocked = status_stats.get("blocked", 0)
    new_count = status_stats.get("new", 0)
    invalid = status_stats.get("invalid", 0)

    baseline = _load_clients_stats_baseline()
    processed_now = max(0, contacted - int(baseline.get("processed", 0)))
    skipped_now = max(0, blocked - int(baseline.get("skipped", 0)))

    text = (
        "📁 <b>База клиентов</b>\n\n"
        f"📊 Всего в базе: <b>{total_count}</b>\n"
        f"✅ Обработано (с момента сброса): <b>{processed_now}</b>\n"
        f"🚫 Пропущено (STOP-лист, с момента сброса): <b>{skipped_now}</b>\n"
        f"🆕 Осталось сейчас (NEW): <b>{new_count}</b>\n\n"
        f"ℹ️ Дополнительно: невалидные <b>{invalid}</b>"
    )
    await callback.message.answer(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=_clients_dashboard_keyboard(),
    )


@router.callback_query(F.data == "clients_upload")
async def cb_clients_upload(callback: CallbackQuery, state: FSMContext):
    """Начало загрузки базы клиентов."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await state.clear()
    await safe_edit_message(
        callback.message,
        "📥 <b>Загрузка базы клиентов</b>\n\n"
        "Отправьте TXT-файл со списком @username.\n\n"
        "📝 Формат:\n"
        "• Каждый username с новой строки\n"
        "• Можно с @ или без\n"
        "• Пример:\n"
        "  @username1\n"
        "  username2\n"
        "  @username3\n\n"
        "❌ Отмена: /start",
        reply_markup=get_cancel_with_back_keyboard("cancel_clients_upload", "db_sec_sheets"),
    )
    await state.set_state(ClientUpload.waiting_for_file)
    await callback.answer()


@router.callback_query(F.data == "cancel_clients_upload")
async def cb_cancel_clients_upload(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    await safe_edit_message(
        callback.message,
        "❌ <b>Отменено</b>\n\n"
        "Загрузка базы клиентов отменена.",
        reply_markup=get_clients_keyboard(),
    )
    await callback.answer()


@router.message(ClientUpload.waiting_for_file, F.document)
async def process_clients_txt(message: Message, state: FSMContext):
    """Обработка загруженного TXT с клиентами."""
    if message.from_user.id != OWNER_ID:
        return
    
    await state.set_state(ClientUpload.processing)
    
    document = message.document
    
    # Проверка расширения
    if not document.file_name.lower().endswith('.txt'):
        await message.answer("❌ Пожалуйста, отправьте TXT-файл")
        await state.clear()
        return
    
    status_msg = await message.answer("⏳ Загрузка файла...")
    
    try:
        # Скачивание файла
        file = await message.bot.get_file(document.file_id)
        file_path = FILES_DIR / f"clients_{document.file_name}"
        
        FILES_DIR.mkdir(parents=True, exist_ok=True)
        
        await message.bot.download_file(file.file_path, file_path)
        await status_msg.edit_text("📖 Чтение файла...")
        
        # Чтение и парсинг
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Извлечение username
        usernames = set()
        pattern = r'@?([a-zA-Z0-9_]{5,32})'
        
        for match in re.finditer(pattern, content):
            username = match.group(1).lower()
            # Пропускаем системные
            if not username.startswith('telegram') and not username.startswith('bot'):
                usernames.add(username)
        
        if not usernames:
            await status_msg.edit_text(
                "❌ Не найдено валидных username.\n"
                "Проверьте формат файла."
            )
            await state.clear()
            return
        
        await status_msg.edit_text(f"📊 Найдено username: {len(usernames)}\n\nСохранение в БД...")
        
        # Сохранение в БД
        async with session_scope() as session:
            added = await ClientRepository.create_many(session, list(usernames))
        
        # Очистка файла
        try:
            file_path.unlink()
        except Exception as e:
            log.warning(f"Не удалось удалить файл: {e}")
        
        await status_msg.edit_text(
            f"✅ <b>Готово!</b>\n\n"
            f"📊 Добавлено клиентов: {added}\n"
            f"📁 Файл сохранён: {file_path.name}",
            parse_mode=ParseMode.HTML,
        )
        
    except Exception as e:
        log.error(f"Ошибка обработки файла клиентов: {e}")
        await status_msg.edit_text(f"❌ Ошибка: {e}")
    
    finally:
        await state.clear()


@router.callback_query(F.data == "clients_list")
async def cb_clients_list(callback: CallbackQuery):
    """Дашборд клиентов: статистика и действия без длинного списка."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await render_clients_dashboard(callback)
    await callback.answer()


@router.callback_query(F.data == "clients_export_new")
async def cb_clients_export_new(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    async with session_scope() as session:
        from sqlalchemy import select
        rows = await session.execute(
            select(Client.username)
            .where(Client.status == ClientStatus.NEW)
            .order_by(Client.id.asc())
        )
        usernames = [str(r[0]).strip() for r in rows.fetchall() if r[0]]

    if not usernames:
        await callback.answer("Нет необработанных клиентов (NEW).", show_alert=True)
        return

    body = "\n".join(f"@{u.lstrip('@')}" for u in usernames) + "\n"
    file = BufferedInputFile(
        body.encode("utf-8"),
        filename="clients_new_export.txt",
    )
    await callback.message.answer_document(
        document=file,
        caption=f"⬇️ Выгрузка NEW-клиентов: <b>{len(usernames)}</b>",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer("Файл отправлен")


@router.callback_query(F.data == "clients_stats_reset_confirm")
async def cb_clients_stats_reset_confirm(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="♻️ Да, сбросить", callback_data="clients_stats_reset_apply")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="db_m_2131")],
        ]
    )
    await callback.message.answer(
        "♻️ <b>Сброс статистики клиентов</b>\n\n"
        "Сбросятся счётчики «Обработано» и «Пропущено (STOP-лист)».\n"
        "Статусы клиентов в базе не изменятся.",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data == "clients_stats_reset_apply")
async def cb_clients_stats_reset_apply(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    async with session_scope() as session:
        from sqlalchemy import select, func
        status_result = await session.execute(
            select(Client.status, func.count(Client.id)).group_by(Client.status)
        )
        status_stats = {row[0].value: int(row[1]) for row in status_result.all()}
    _save_clients_stats_baseline(
        processed=status_stats.get("contacted", 0),
        skipped=status_stats.get("blocked", 0),
    )
    await callback.answer("Статистика сброшена")
    await render_clients_dashboard(callback)


@router.callback_query(F.data == "clients_clear")
async def cb_clients_clear(callback: CallbackQuery):
    """Очистка базы клиентов."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    
    # Отправляем подтверждение
    await callback.message.answer(
        "⚠️ <b>Подтверждение</b>\n\n"
        "Вы уверены, что хотите очистить всю базу клиентов?\n"
        "Это действие нельзя отменить.\n\n"
        "Нажмите ещё раз для подтверждения.",
        reply_markup=get_confirm_clear_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data == "clients_reset_contacted")
async def cb_clients_reset_contacted(callback: CallbackQuery):
    """Запрос подтверждения массового сброса CONTACTED -> NEW."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await callback.message.answer(
        "♻️ <b>Повторный прогон клиентов</b>\n\n"
        "Сбросить статус всех клиентов <code>CONTACTED</code> обратно в <code>NEW</code>?\n"
        "Это удобно для повторного теста рассылки без переимпорта.\n\n"
        "Нажмите ещё раз для подтверждения.",
        reply_markup=get_confirm_reset_clients_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data == "clients_reset_contacted_confirm")
async def cb_clients_reset_contacted_confirm(callback: CallbackQuery):
    """Подтверждение массового сброса CONTACTED -> NEW."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    async with session_scope() as session:
        result = await session.execute(
            update(Client)
            .where(Client.status == ClientStatus.CONTACTED)
            .values(status=ClientStatus.NEW, last_contacted_at=None)
        )
        await session.commit()
        changed = int(result.rowcount or 0)

    await callback.message.answer(
        f"✅ Готово. Переведено в NEW: <b>{changed}</b> клиентов.",
        parse_mode=ParseMode.HTML,
        reply_markup=get_context_back_keyboard("menu_database"),
    )
    await callback.answer("Сброшено")


@router.callback_query(F.data == "clients_clear_confirm")
async def cb_clients_clear_confirm(callback: CallbackQuery):
    """Подтверждение очистки базы."""
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    
    async with session_scope() as session:
        await ClientRepository.clear_all(session)
    
    await callback.message.answer("🗑 База клиентов очищена.")
    await callback.answer()


def get_confirm_clear_keyboard():
    """Клавиатура подтверждения очистки."""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    
    keyboard = [
        [
            InlineKeyboardButton(text="⚠️ Да, очистить", callback_data="clients_clear_confirm"),
        ],
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data="clients_upload"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_confirm_reset_clients_keyboard():
    """Клавиатура подтверждения сброса CONTACTED -> NEW."""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    keyboard = [
        [
            InlineKeyboardButton(
                text="♻️ Да, сбросить",
                callback_data="clients_reset_contacted_confirm",
            ),
        ],
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data="db_sheet_213"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)
