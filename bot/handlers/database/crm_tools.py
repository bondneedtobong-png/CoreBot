"""
Поиск по классам, выгрузка CRM, бэкап/merge, выгрузка app-логов, краткая статистика.
"""
from __future__ import annotations

import io

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from bot.config import FILES_DIR, OWNER_ID
from bot.keyboards.database_menu import (
    kb_database_debug_windows,
    kb_database_detail,
    kb_database_stats,
)
from database.repositories import ClientRepository
from database.session import session_scope
from services.database.client_search import (
    parse_simple_classes,
    search_clients_by_classes,
    search_clients_dsl,
)
from services.database.crm_backup import export_snapshot, merge_snapshot, snapshot_from_json
from sqlalchemy import func, select
from utils.app_logs import build_debug_excerpt
from utils.logger import log

from database.models import Client, ClientClassCounter

router = Router()

STUB = "\n\n<i>Подробности: <code>docs/DATABASE_MODULE_SPEC.md</code></i>"


class DatabaseSearchFSM(StatesGroup):
    waiting_query = State()


class DatabaseBackupMergeFSM(StatesGroup):
    waiting_json = State()


def _owner(uid: int) -> bool:
    return uid == OWNER_ID


@router.callback_query(F.data == "db_dl2141")
async def db_dl2141_export(callback: CallbackQuery):
    if not _owner(callback.from_user.id):
        await callback.answer("⛔", show_alert=True)
        return
    async with session_scope() as session:
        snap = await export_snapshot(session)
    raw = __import__("json").dumps(snap, ensure_ascii=False, indent=2)
    bio = io.BytesIO(raw.encode("utf-8"))
    bio.name = "crm_export.json"
    await callback.message.answer_document(
        BufferedInputFile(bio.getvalue(), filename="crm_export.json"),
        caption="📥 Экспорт CRM (клиенты, классы, теги).",
    )
    await callback.answer()


@router.callback_query(F.data == "db_search2142")
async def db_search2142_intro(callback: CallbackQuery, state: FSMContext):
    if not _owner(callback.from_user.id):
        await callback.answer("⛔", show_alert=True)
        return
    await state.set_state(DatabaseSearchFSM.waiting_query)
    await callback.message.edit_text(
        "🔎 <b>Поиск по классам</b>\n\n"
        "<b>Простой режим</b> — строка:\n"
        "<code>ru,pulse exclude:bl</code>\n"
        "(классы через запятую, опционально <code>exclude:bl,spam</code>)\n\n"
        "<b>DSL</b> — с префикса <code>dsl:</code>, пример:\n"
        "<code>dsl: class:pulse>=1 class:accept>=1</code>\n\n"
        "Отправьте запрос сообщением. /cancel — выход.",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_database_detail(),
    )
    await callback.answer()


@router.message(DatabaseSearchFSM.waiting_query, F.text)
async def db_search2142_run(message: Message, state: FSMContext):
    if not _owner(message.from_user.id):
        return
    if (message.text or "").strip().lower() in ("/cancel", "cancel"):
        await state.clear()
        await message.answer("Отменено.", reply_markup=kb_database_detail())
        return
    text = (message.text or "").strip()
    try:
        async with session_scope() as session:
            if text.lower().startswith("dsl:"):
                q = text[4:].strip()
                clients = await search_clients_dsl(session, q, limit=2000)
            else:
                inc, exc = parse_simple_classes(text)
                clients = await search_clients_by_classes(
                    session, include=inc, exclude=exc, limit=2000
                )
        lines = [f"@{c.username}" for c in clients]
        body = "\n".join(lines) if lines else "— пусто —"
        if len(body) > 3500:
            bio = io.BytesIO(body.encode("utf-8"))
            await message.answer_document(
                BufferedInputFile(bio.getvalue(), filename="search_result.txt"),
                caption=f"Найдено: {len(lines)}",
            )
        else:
            await message.answer(
                f"Найдено: <b>{len(lines)}</b>\n<pre>{body[:3500]}</pre>",
                parse_mode=ParseMode.HTML,
            )
    except Exception as e:
        log.exception(f"search: {e}")
        await message.answer(f"Ошибка: {e}")
    await state.clear()


@router.callback_query(F.data == "db_st_234")
async def db_st_234_backup_export(callback: CallbackQuery):
    if not _owner(callback.from_user.id):
        await callback.answer("⛔", show_alert=True)
        return
    async with session_scope() as session:
        snap = await export_snapshot(session)
    raw = __import__("json").dumps(snap, ensure_ascii=False, indent=2)
    await callback.message.answer_document(
        BufferedInputFile(raw.encode("utf-8"), filename="crm_backup.json"),
        caption="💾 Снимок CRM для бэкапа.",
    )
    await callback.answer()


@router.callback_query(F.data.in_(("db_st_235", "db_st_236")))
async def db_st_merge_prompt(callback: CallbackQuery, state: FSMContext):
    if not _owner(callback.from_user.id):
        await callback.answer("⛔", show_alert=True)
        return
    await state.set_state(DatabaseBackupMergeFSM.waiting_json)
    await state.update_data(merge_mode=callback.data)
    await callback.message.edit_text(
        "📥 <b>Импорт / merge CRM</b>\n\n"
        "Пришлите <b>.json</b> (как из выгрузки). "
        "Правило: для каждого username — <b>max</b> по счётчикам классов, "
        "теги объединяются.\n\n"
        "/cancel — отмена.",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_database_stats(),
    )
    await callback.answer()


@router.message(DatabaseBackupMergeFSM.waiting_json, F.document)
async def db_st_merge_file(message: Message, state: FSMContext):
    if not _owner(message.from_user.id):
        return
    doc = message.document
    if not (doc.file_name or "").lower().endswith(".json"):
        await message.answer("Нужен .json")
        return
    buf = io.BytesIO()
    await message.bot.download(doc, destination=buf)
    raw = buf.getvalue().decode("utf-8")
    try:
        data = snapshot_from_json(raw)
    except Exception as e:
        await message.answer(f"Невалидный JSON: {e}")
        return
    try:
        async with session_scope() as session:
            n = await merge_snapshot(session, data)
        await message.answer(f"✅ Обработано записей (merge): <b>{n}</b>", parse_mode=ParseMode.HTML)
    except Exception as e:
        log.exception(f"merge: {e}")
        await message.answer(f"Ошибка: {e}")
    await state.clear()


@router.callback_query(
    F.data.in_(("db_dbg_10m", "db_dbg_30m", "db_dbg_2h", "db_dbg_1d", "db_dbg_7d", "db_dbg_30d"))
)
async def db_dbg_window(callback: CallbackQuery):
    if not _owner(callback.from_user.id):
        await callback.answer("⛔", show_alert=True)
        return
    excerpt, name = build_debug_excerpt(callback.data or "")
    if not excerpt:
        await callback.message.edit_text(
            "⚠️ Лог пуст или файл отсутствует.",
            reply_markup=kb_database_debug_windows(),
        )
        await callback.answer()
        return
    await callback.message.answer_document(
        BufferedInputFile(excerpt.encode("utf-8", errors="replace"), filename=f"{name}.txt"),
        caption=f"Окно: <code>{callback.data}</code>",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data.in_(("db_st_231", "db_st_232", "db_st_233")))
async def db_stats_short(callback: CallbackQuery):
    if not _owner(callback.from_user.id):
        await callback.answer("⛔", show_alert=True)
        return
    async with session_scope() as session:
        total_c = await session.execute(select(func.count(Client.id)))
        total = int(total_c.scalar() or 0)
        cc = await session.execute(
            select(func.count(ClientClassCounter.id)).where(ClientClassCounter.count > 0)
        )
        n_ctr = int(cc.scalar() or 0)
    labels = {
        "db_st_231": "По клиентам",
        "db_st_232": "По аккаунтам",
        "db_st_233": "По переписке",
    }
    extra = ""
    if callback.data == "db_st_231":
        extra = f"Всего клиентов: <b>{total}</b>\nСтрок классов (count&gt;0): <b>{n_ctr}</b>"
    else:
        extra = "Агрегаты по аккаунтам/переписке — в следующих версиях; см. мониторинг рассылки."
    await callback.message.edit_text(
        f"📈 <b>{labels.get(callback.data, callback.data)}</b>\n\n{extra}" + STUB,
        reply_markup=kb_database_stats(),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()
