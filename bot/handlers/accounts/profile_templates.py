"""Named profile templates and independent random pools for bot account editing."""
from __future__ import annotations

import asyncio
import shutil
from contextlib import AsyncExitStack
from html import escape
from pathlib import Path
from uuid import uuid4

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select, update

from bot.config import is_authorized_user
from bot.config import DATA_DIR, SESSIONS_DIR
from bot.handlers.accounts.common import safe_edit_message
from bot.keyboards.main import get_cancel_with_back_keyboard, get_edit_profile_keyboard
from database.models import Account, ProfilePoolItem, ProfileTemplate, account_groups
from database.profile_templates import (
    add_pool_batch, create_template, get_template, list_pool_items, list_templates,
    pool_counts, random_profile,
)
from database.repositories import AccountRepository, GroupRepository
from database.session import session_scope
from database.sqlite_pragmas import commit_with_busy_retry, execute_with_busy_retry
from utils.logger import log
from utils.safe_zip import extract_zip_safely
from utils.time import utcnow_naive

router = Router()
ASSETS_DIR = DATA_DIR / "profile_assets"
MAX_PHOTO_BYTES = 10_000_000
CATEGORY_LABELS = {"m": "М", "f": "Ж", "u": "−"}
KIND_LABELS = {"name": "имена", "bio": "BIO", "photo": "фото"}
GROUP_RANDOM_DELAY_SECONDS = 0.35
_group_random_lock = asyncio.Lock()


class TemplateFlow(StatesGroup):
    waiting_name = State()
    waiting_identity = State()
    waiting_photo = State()
    waiting_pool_zip = State()
    waiting_pool_text = State()
    waiting_pool_photo = State()
    waiting_pool_category = State()


def _photo_extension(path: Path) -> str | None:
    with path.open("rb") as file:
        head = file.read(16)
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    return None


def _save_photo(source: Path) -> str:
    if source.stat().st_size > MAX_PHOTO_BYTES:
        raise ValueError("Фото больше 10 МБ")
    extension = _photo_extension(source)
    if not extension:
        raise ValueError("Фото должно быть JPEG или PNG")
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid4().hex}{extension}"
    shutil.copyfile(source, ASSETS_DIR / filename)
    return filename


def _asset_path(filename: str | None) -> Path | None:
    if not filename or Path(filename).name != filename:
        return None
    path = ASSETS_DIR / filename
    return path if path.is_file() else None


def _editor_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать шаблон", callback_data="profile_template_create")],
        [InlineKeyboardButton(text="📦 Загрузить наборы ZIP", callback_data="profile_pool_import")],
        [InlineKeyboardButton(text="📋 Мои шаблоны", callback_data="profile_templates_list")],
        [InlineKeyboardButton(text="🎲 Рандомизация", callback_data="profile_rand_menu")],
        [InlineKeyboardButton(text="👤 Выбрать аккаунт", callback_data="accounts_list")],
        [InlineKeyboardButton(text="⬅️ Управление", callback_data="accounts_manage")],
    ])


def _category_keyboard(prefix: str, back: str = "profile_rand_menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"{prefix}_{code}")
         for code, label in CATEGORY_LABELS.items()],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=back)],
    ])


def _random_menu_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for kind, label in KIND_LABELS.items():
        rows.append([
            InlineKeyboardButton(text=f"⬆️ Загрузить {label}", callback_data=f"profile_pool_upload_{kind}"),
            InlineKeyboardButton(text="📋 Текущие", callback_data=f"profile_pool_view_{kind}"),
        ])
    rows.extend([
        [InlineKeyboardButton(text="👤 Рандом для аккаунтов", callback_data="profile_pick_accounts_category")],
        [InlineKeyboardButton(text="👥 Рандом для групп", callback_data="profile_pick_groups_category")],
        [InlineKeyboardButton(text="⬅️ Редактор профилей", callback_data="accounts_profile_editor")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _parse_semicolon_values(text: str, kind: str) -> list[str]:
    if kind not in {"name", "bio"} or "\n" in text or "\r" in text:
        raise ValueError("Отправьте одну строку, разделяя значения знаком ;")
    raw = text.split(";")
    if len(raw) > 1000 or any(not item.strip() for item in raw):
        raise ValueError("Нужны 1–1000 непустых значений через ;")
    values = [item.strip() for item in raw]
    if kind == "name" and any(" " in value or "\t" in value or len(value) > 100 for value in values):
        raise ValueError("Имя — без пробелов, до 100 символов")
    if kind == "bio" and any(len(value) > 70 for value in values):
        raise ValueError("Каждое BIO — до 70 символов")
    return list(dict.fromkeys(values))


async def render_profile_editor(callback: CallbackQuery) -> None:
    async with session_scope() as session:
        templates = await list_templates(session)
        counts = await pool_counts(session)
    await safe_edit_message(
        callback.message,
        "🧩 <b>Редактор профилей</b>\n\n"
        f"Готовых шаблонов: <b>{len(templates)}</b>\n"
        f"Наборы: имён <b>{counts.get('name', 0)}</b>, bio <b>{counts.get('bio', 0)}</b>, "
        f"фото <b>{counts.get('photo', 0)}</b>.\n\n"
        "Шаблон задаёт конкретное имя, bio и фото. Случайный профиль выбирает по одному "
        "элементу из каждого загруженного набора.",
        reply_markup=_editor_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "profile_rand_menu")
async def show_random_menu(callback: CallbackQuery, state: FSMContext):
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    async with session_scope() as session:
        counts = await pool_counts(session)
    await safe_edit_message(
        callback.message,
        "🎲 <b>Рандомизация профилей</b>\n\n"
        f"Имён: {counts.get('name', 0)} · BIO: {counts.get('bio', 0)} · фото: {counts.get('photo', 0)}.\n\n"
        "Для каждого набора выберите категорию М, Ж или − (унисекс). "
        "Загружайте женские, мужские и универсальные данные отдельно. "
        "При рандомизации используются элементы только выбранной категории.",
        reply_markup=_random_menu_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("profile_pool_upload_"))
async def start_pool_upload(callback: CallbackQuery, state: FSMContext):
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    kind = callback.data.rsplit("_", 1)[-1]
    if kind not in KIND_LABELS:
        await callback.answer("Неизвестный набор", show_alert=True)
        return
    await state.clear()
    await state.update_data(pool_kind=kind)
    await state.set_state(
        TemplateFlow.waiting_pool_photo if kind == "photo" else TemplateFlow.waiting_pool_text
    )
    instruction = (
        "Пришлите одно фото JPEG/PNG до 10 МБ как изображение или документ. "
        "После загрузки выберите его категорию. Повторите для следующего фото."
        if kind == "photo" else
        "Пришлите значения одной строкой через <code>;</code> (до 1000 за раз). "
        + ("Имена без пробелов, например <code>Анна;Мария;Alex</code>. " if kind == "name" else
           "Пробелы внутри BIO допустимы. ")
        + "После ввода выберите общую категорию М, Ж или −."
    )
    await safe_edit_message(
        callback.message, f"⬆️ <b>Загрузить {KIND_LABELS[kind]}</b>\n\n{instruction}",
        reply_markup=get_cancel_with_back_keyboard("cancel_accounts", "profile_rand_menu"),
    )
    await callback.answer()


@router.message(TemplateFlow.waiting_pool_text)
async def receive_pool_text(message: Message, state: FSMContext):
    if not is_authorized_user(message.from_user.id):
        return
    kind = (await state.get_data()).get("pool_kind")
    try:
        values = _parse_semicolon_values(message.text or "", kind)
    except ValueError as exc:
        await message.answer(escape(str(exc)))
        return
    await state.update_data(pool_values=values)
    await state.set_state(TemplateFlow.waiting_pool_category)
    await message.answer(
        f"Получено {len(values)} значений. Выберите категорию для всего набора:",
        reply_markup=_category_keyboard("profile_pool_category"),
    )


@router.message(TemplateFlow.waiting_pool_photo)
async def receive_pool_photo(message: Message, state: FSMContext):
    if not is_authorized_user(message.from_user.id):
        return
    source = message.photo[-1] if message.photo else message.document
    if source is None:
        await message.answer("Пришлите фото JPEG/PNG как изображение или документ.")
        return
    if source.file_size and source.file_size > MAX_PHOTO_BYTES:
        await message.answer("Фото больше 10 МБ.")
        return
    await state.update_data(pool_file_id=source.file_id)
    await state.set_state(TemplateFlow.waiting_pool_category)
    await message.answer("Выберите категорию фото:", reply_markup=_category_keyboard("profile_pool_category"))


@router.callback_query(F.data.startswith("profile_pool_category_"), TemplateFlow.waiting_pool_category)
async def save_pool_category(callback: CallbackQuery, state: FSMContext):
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    category = callback.data.rsplit("_", 1)[-1]
    if category not in CATEGORY_LABELS:
        await callback.answer("Неизвестная категория", show_alert=True)
        return
    data = await state.get_data()
    kind = data.get("pool_kind")
    if kind not in KIND_LABELS:
        await callback.answer("Загрузка устарела", show_alert=True)
        return
    photo_file = None
    temporary = None
    try:
        if kind == "photo":
            file_id = data.get("pool_file_id")
            if not file_id:
                raise ValueError("Фото не получено")
            temporary = DATA_DIR / "profile_import" / f"photo_{uuid4().hex}"
            temporary.parent.mkdir(parents=True, exist_ok=True)
            await callback.bot.download(file_id, destination=temporary)
            photo_file = _save_photo(temporary)
            values = [photo_file]
        else:
            values = data.get("pool_values") or []
            if not values:
                raise ValueError("Значения не получены")
        async with session_scope() as session:
            counts = await add_pool_batch(session, {kind: values}, category=category)
    except Exception as exc:
        if photo_file:
            (ASSETS_DIR / photo_file).unlink(missing_ok=True)
        log.warning("Profile pool save failed: {}", type(exc).__name__)
        await callback.answer(f"Не удалось сохранить: {type(exc).__name__}", show_alert=True)
        return
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    await state.clear()
    await safe_edit_message(
        callback.message,
        f"✅ Добавлено: {counts[kind]} ({KIND_LABELS[kind]}, категория {CATEGORY_LABELS[category]}).",
        reply_markup=_random_menu_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("profile_pool_view_"))
async def choose_pool_view_category(callback: CallbackQuery):
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    kind = callback.data.rsplit("_", 1)[-1]
    if kind not in KIND_LABELS:
        await callback.answer("Неизвестный набор", show_alert=True)
        return
    await safe_edit_message(
        callback.message, f"📋 <b>Текущие {KIND_LABELS[kind]}</b>\n\nВыберите категорию:",
        reply_markup=_category_keyboard(f"profile_pool_current_{kind}"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("profile_pool_current_"))
async def show_pool_items(callback: CallbackQuery):
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    parts = callback.data.split("_")
    if len(parts) not in {5, 6}:
        await callback.answer("Выберите категорию", show_alert=True)
        return
    kind, category = parts[3], parts[4]
    if kind not in KIND_LABELS or category not in CATEGORY_LABELS:
        await callback.answer("Неизвестный набор", show_alert=True)
        return
    page = max(0, int(parts[5])) if len(parts) == 6 and parts[5].isdigit() else 0
    async with session_scope() as session:
        items, total = await list_pool_items(session, kind, category, offset=page * 20)
    pages = max(1, (total + 19) // 20)
    if page >= pages:
        page = pages - 1
        async with session_scope() as session:
            items, total = await list_pool_items(session, kind, category, offset=page * 20)
    lines = [f"📋 <b>{KIND_LABELS[kind]}, {CATEGORY_LABELS[category]}</b>",
             f"Всего: {total} · страница {page + 1}/{pages}", ""]
    rows = []
    if kind == "photo":
        lines.append("Нажмите на номер, чтобы посмотреть фото.")
        for item in items:
            rows.append([InlineKeyboardButton(text=f"🖼 #{item.id}", callback_data=f"profile_pool_photo_{item.id}")])
    else:
        lines.extend(f"• {escape(item.value)}" for item in items)
    if not items:
        lines.append("Набор пуст.")
    nav = []
    if page:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"profile_pool_current_{kind}_{category}_{page - 1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"profile_pool_current_{kind}_{category}_{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ Рандомизация", callback_data="profile_rand_menu")])
    await safe_edit_message(callback.message, "\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(F.data.startswith("profile_pool_photo_"))
async def show_pool_photo(callback: CallbackQuery):
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    item_id = callback.data.rsplit("_", 1)[-1]
    if not item_id.isdigit():
        await callback.answer("Фото не найдено", show_alert=True)
        return
    async with session_scope() as session:
        item = await session.get(ProfilePoolItem, int(item_id))
    path = _asset_path(item.value) if item and item.kind == "photo" else None
    if not path:
        await callback.answer("Файл фото не найден", show_alert=True)
        return
    await callback.message.answer_photo(FSInputFile(path), caption=f"Фото #{item.id} · {CATEGORY_LABELS[item.category]}")
    await callback.answer()


@router.callback_query(F.data == "profile_template_create")
async def start_template(callback: CallbackQuery, state: FSMContext):
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    await state.set_state(TemplateFlow.waiting_name)
    await safe_edit_message(
        callback.message, "➕ <b>Новый шаблон</b>\n\nПришлите название шаблона внутри CoreBot (до 100 символов).",
        reply_markup=get_cancel_with_back_keyboard("cancel_accounts", "accounts_profile_editor"),
    )
    await callback.answer()


@router.message(TemplateFlow.waiting_name)
async def receive_template_name(message: Message, state: FSMContext):
    if not is_authorized_user(message.from_user.id):
        return
    name = (message.text or "").strip()
    if not 1 <= len(name) <= 100:
        await message.answer("Введите название от 1 до 100 символов.")
        return
    async with session_scope() as session:
        found = await session.scalar(select(ProfileTemplate.id).where(ProfileTemplate.name == name))
    if found:
        await message.answer("Такое название уже есть. Введите другое.")
        return
    await state.update_data(template_name=name)
    await state.set_state(TemplateFlow.waiting_identity)
    await message.answer(
        "Напишите имя в первой строке, bio — во второй.\n\n"
        "Пример:\n<code>Мария Иванова\nЛюблю путешествия и книги</code>",
        reply_markup=get_cancel_with_back_keyboard("cancel_accounts", "accounts_profile_editor"),
        parse_mode=ParseMode.HTML,
    )


@router.message(TemplateFlow.waiting_identity)
async def receive_template_identity(message: Message, state: FSMContext):
    if not is_authorized_user(message.from_user.id):
        return
    lines = (message.text or "").strip().splitlines()
    if len(lines) < 2 or not lines[0].strip() or not "\n".join(lines[1:]).strip():
        await message.answer("Нужны две строки: имя и bio.")
        return
    full_name = lines[0].strip()
    first, *rest = full_name.split(maxsplit=1)
    last = rest[0] if rest else None
    bio = "\n".join(lines[1:]).strip()
    if len(first) > 100 or (last and len(last) > 100) or len(bio) > 70:
        await message.answer("Имя/фамилия — до 100 символов, bio — до 70 символов.")
        return
    await state.update_data(first_name=first, last_name=last, bio=bio)
    await state.set_state(TemplateFlow.waiting_photo)
    await message.answer(
        "🖼 Пришлите фото JPEG/PNG (до 10 МБ) или напишите <code>пропустить</code>. "
        "При применении шаблона без фото текущая аватарка останется.",
        reply_markup=get_cancel_with_back_keyboard("cancel_accounts", "accounts_profile_editor"),
        parse_mode=ParseMode.HTML,
    )


@router.message(TemplateFlow.waiting_photo)
async def receive_template_photo(message: Message, state: FSMContext):
    if not is_authorized_user(message.from_user.id):
        return
    skip = (message.text or "").strip().lower() == "пропустить"
    source = message.photo[-1] if message.photo else message.document
    if not skip and source is None:
        await message.answer("Пришлите фото или напишите «пропустить».")
        return
    if source and source.file_size and source.file_size > MAX_PHOTO_BYTES:
        await message.answer("Фото больше 10 МБ.")
        return
    photo_file = None
    if not skip:
        ASSETS_DIR.mkdir(parents=True, exist_ok=True)
        temporary = ASSETS_DIR / f"tmp_{uuid4().hex}"
        try:
            await message.bot.download(source, destination=temporary)
            photo_file = _save_photo(temporary)
        except Exception as exc:
            await message.answer(f"Не удалось сохранить фото: {type(exc).__name__}")
            return
        finally:
            temporary.unlink(missing_ok=True)
    data = await state.get_data()
    try:
        async with session_scope() as session:
            template = await create_template(
                session, name=data["template_name"], first_name=data["first_name"],
                last_name=data.get("last_name"), bio=data["bio"], photo_file=photo_file,
            )
    except Exception as exc:
        if photo_file:
            (ASSETS_DIR / photo_file).unlink(missing_ok=True)
        await message.answer(f"Не удалось создать шаблон: {type(exc).__name__}")
        return
    await state.clear()
    await message.answer(
        f"✅ Шаблон «{escape(template.name)}» сохранён.",
        reply_markup=_editor_keyboard(), parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data == "profile_pool_import")
async def start_pool_import(callback: CallbackQuery, state: FSMContext):
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    await state.set_state(TemplateFlow.waiting_pool_zip)
    await safe_edit_message(
        callback.message,
        "📦 <b>Наборы для рандомизации</b>\n\n"
        "Пришлите ZIP с <code>names.txt</code> (одно имя на строку), "
        "<code>bios.txt</code> (одно bio на строку) и папкой <code>photos/</code> с JPEG/PNG. "
        "Можно заполнить часть наборов; новые элементы добавятся к существующим.",
        reply_markup=get_cancel_with_back_keyboard("cancel_accounts", "accounts_profile_editor"),
    )
    await callback.answer()


@router.message(TemplateFlow.waiting_pool_zip, F.document)
async def receive_pool_zip(message: Message, state: FSMContext):
    if not is_authorized_user(message.from_user.id):
        return
    doc = message.document
    if not (doc.file_name or "").lower().endswith(".zip") or (doc.file_size or 0) > 100_000_000:
        await message.answer("Пришлите ZIP до 100 МБ.")
        return
    temporary = DATA_DIR / "profile_import" / uuid4().hex
    archive = temporary / "pool.zip"
    extracted = temporary / "extracted"
    temporary.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    try:
        await message.bot.download(doc, destination=archive)
        extract_zip_safely(archive, extracted, max_files=3000, max_uncompressed_bytes=250_000_000)
        names: list[str] = []
        bios: list[str] = []
        photos: list[str] = []
        for path in extracted.rglob("*"):
            if not path.is_file():
                continue
            lower = path.name.lower()
            if lower in {"names.txt", "bios.txt"}:
                if path.stat().st_size > 1_000_000:
                    raise ValueError("Текстовый файл больше 1 МБ")
                lines = [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
                if lower == "names.txt":
                    names.extend(lines)
                else:
                    bios.extend(lines)
            elif any(part.lower() == "photos" for part in path.relative_to(extracted).parts[:-1]):
                filename = _save_photo(path)
                copied.append(filename)
                photos.append(filename)
        if not (names or bios or photos):
            raise ValueError("В ZIP нет names.txt, bios.txt или фото в photos/")
        if len(names) > 1000 or len(bios) > 1000 or len(photos) > 1000:
            raise ValueError("Не больше 1000 элементов каждого типа за загрузку")
        if any(any(len(part) > 100 for part in name.split(maxsplit=1)) for name in names) or any(len(bio) > 70 for bio in bios):
            raise ValueError("Имя и фамилия — до 100 символов каждое, bio — до 70 символов")
        async with session_scope() as session:
            counts = await add_pool_batch(session, {"name": names, "bio": bios, "photo": photos})
        await state.clear()
        await message.answer(
            f"✅ Добавлено: имён {counts['name']}, bio {counts['bio']}, фото {counts['photo']}.",
            reply_markup=_editor_keyboard(),
        )
    except Exception as exc:
        for filename in copied:
            (ASSETS_DIR / filename).unlink(missing_ok=True)
        log.warning("Profile pool import failed: {}", exc)
        await message.answer(f"❌ Не удалось импортировать набор: {escape(str(exc))}", parse_mode=ParseMode.HTML)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


@router.callback_query(F.data == "profile_templates_list")
async def show_templates(callback: CallbackQuery):
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    async with session_scope() as session:
        templates = await list_templates(session)
    lines = ["🧩 <b>Шаблоны</b>", ""]
    for template in templates[:30]:
        lines.append(f"• {escape(template.name)} — {escape(template.first_name)} {escape(template.last_name or '')} {'🖼' if template.photo_file else ''}")
    if len(templates) > 30:
        lines.append(f"…ещё {len(templates) - 30}")
    if not templates:
        lines.append("Пока нет шаблонов.")
    await safe_edit_message(callback.message, "\n".join(lines), reply_markup=_editor_keyboard())
    await callback.answer()


def _apply_keyboard(account_id: int, templates: list[ProfileTemplate], page: int) -> InlineKeyboardMarkup:
    pages = max(1, (len(templates) + 4) // 5)
    page = max(0, min(page, pages - 1))
    rows = [[InlineKeyboardButton(text=f"🎲 Категория {label}", callback_data=f"profile_pick_account_{code}_{account_id}")
             for code, label in CATEGORY_LABELS.items()]]
    for template in templates[page * 5:(page + 1) * 5]:
        rows.append([InlineKeyboardButton(text=f"🧩 {template.name[:45]}", callback_data=f"profile_set_{account_id}_{template.id}")])
    if pages > 1:
        nav = []
        if page:
            nav.append(InlineKeyboardButton(text="◀️", callback_data=f"profile_apply_menu_{account_id}_{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="profile_page_info"))
        if page + 1 < pages:
            nav.append(InlineKeyboardButton(text="▶️", callback_data=f"profile_apply_menu_{account_id}_{page + 1}"))
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ Профиль", callback_data=f"account_edit_profile_{account_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("profile_apply_menu_"))
async def show_apply_menu(callback: CallbackQuery, state: FSMContext):
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    parts = callback.data.split("_")
    account_id = int(parts[3])
    page = int(parts[4]) if len(parts) > 4 else 0
    async with session_scope() as session:
        account = await AccountRepository.get_by_id(session, account_id)
        templates = await list_templates(session)
        counts = await pool_counts(session)
    if account is None:
        await callback.answer("Аккаунт не найден", show_alert=True)
        return
    await safe_edit_message(
        callback.message,
        f"🧩 <b>Профиль аккаунта #{account_id}</b>\n\n"
        f"Наборы: {counts.get('name', 0)} имён, {counts.get('bio', 0)} bio, {counts.get('photo', 0)} фото. "
        "Нажатие применит изменения в Telegram.",
        reply_markup=_apply_keyboard(account_id, templates, page),
    )
    await callback.answer()


@router.callback_query(F.data == "profile_page_info")
async def profile_page_info(callback: CallbackQuery):
    await callback.answer()


async def _apply_profile(account_id: int, *, name: str | None, bio: str | None, photo_file: str | None) -> tuple[bool, str]:
    async with session_scope() as session:
        account = await AccountRepository.get_by_id(session, account_id)
    if account is None:
        return False, "Аккаунт не найден"
    if not account.proxy:
        return False, "Аккаунту не назначен SOCKS5"
    if not (name or bio is not None or photo_file):
        return False, "Наборы пусты"
    photo_path = _asset_path(photo_file)
    if photo_file and photo_path is None:
        return False, "Файл фото не найден"
    from workers.manager import _BorrowedWorker, account_worker_for_action, worker_manager
    from telethon.tl.functions.account import UpdateProfileRequest

    if worker_manager.is_mailing_busy():
        return False, "Идёт рассылка; смена профиля отложена"
    worker = account_worker_for_action(account, SESSIONS_DIR / f"{account.session_name}.session", account.proxy)
    async with AsyncExitStack() as locks:
        if isinstance(worker, _BorrowedWorker):
            await locks.enter_async_context(worker._send_lock)
            await locks.enter_async_context(worker._connection_lock)
        try:
            if not await worker.connect(quiet=True) or not worker.client:
                return False, "Не удалось подключить аккаунт через прокси"
            fields = {}
            if name:
                first, *rest = name.split(maxsplit=1)
                fields.update(first_name=first, last_name=rest[0] if rest else "")
            if bio is not None:
                fields["about"] = bio
            if fields:
                await worker.client(UpdateProfileRequest(**fields))
                values = {"updated_at": utcnow_naive()}
                if name:
                    values.update(first_name=fields["first_name"], last_name=fields["last_name"])
                if bio is not None:
                    values["bio"] = bio
                async with session_scope() as session:
                    await execute_with_busy_retry(
                        session, update(Account).where(Account.id == account_id).values(**values),
                        op_name="profile-random-text",
                    )
                    await commit_with_busy_retry(session, op_name="profile-random-text")
            if photo_path:
                if not await worker.set_profile_photo(str(photo_path)):
                    return False, "Текст профиля сохранён, фото Telegram отклонил"
                async with session_scope() as session:
                    await execute_with_busy_retry(
                        session,
                        update(Account).where(Account.id == account_id).values(avatar_path=str(photo_path)),
                        op_name="profile-random-photo",
                    )
                    await commit_with_busy_retry(session, op_name="profile-random-photo")
            return True, "Профиль применён"
        except Exception as exc:
            log.error("Profile apply failed for account {}: {}", account_id, exc)
            return False, f"Ошибка Telegram: {type(exc).__name__}"
        finally:
            await worker.disconnect()


async def _show_random_result(callback: CallbackQuery, account_id: int, selected: dict[str, str | None],
                              ok: bool, detail: str) -> None:
    if not ok:
        await safe_edit_message(
            callback.message, f"❌ {escape(detail)}",
            reply_markup=get_edit_profile_keyboard(account_id),
        )
        return
    name = escape(selected["name"] or "не менялось")
    bio = escape(selected["bio"] or "не менялось")
    photo_path = _asset_path(selected["photo"])
    result = f"✅ {escape(detail)}\n\nИмя: {name}\nBIO: {bio}\nФото: {'обновлено' if photo_path else 'не менялось'}"
    await safe_edit_message(
        callback.message, result, reply_markup=get_edit_profile_keyboard(account_id),
    )
    if photo_path:
        await callback.message.answer_photo(FSInputFile(photo_path), caption="Применённое фото профиля")


@router.callback_query(F.data.regexp(r"^profile_random_\d+$"))
async def apply_random(callback: CallbackQuery):
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    account_id = int(callback.data.rsplit("_", 1)[-1])
    await callback.answer()
    async with session_scope() as session:
        selected = await random_profile(session)
    ok, detail = await _apply_profile(
        account_id, name=selected["name"], bio=selected["bio"], photo_file=selected["photo"],
    )
    await _show_random_result(callback, account_id, selected, ok, detail)


@router.callback_query(F.data.startswith("profile_pick_account_"))
async def apply_random_category(callback: CallbackQuery):
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    parts = callback.data.split("_")
    if len(parts) != 5 or parts[3] not in CATEGORY_LABELS or not parts[4].isdigit():
        await callback.answer("Некорректный выбор", show_alert=True)
        return
    category, account_id = parts[3], int(parts[4])
    await callback.answer()
    async with session_scope() as session:
        selected = await random_profile(session, category)
    ok, detail = await _apply_profile(
        account_id, name=selected["name"], bio=selected["bio"], photo_file=selected["photo"],
    )
    await _show_random_result(callback, account_id, selected, ok, detail)


@router.callback_query(F.data == "profile_pick_accounts_category")
@router.callback_query(F.data == "profile_pick_groups_category")
async def choose_random_target_category(callback: CallbackQuery):
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    target = "accounts" if "accounts" in callback.data else "groups"
    await safe_edit_message(
        callback.message,
        "🎲 <b>Выберите категорию профиля</b>\n\n"
        "Будут выбраны имя, BIO и фото только из указанной категории. "
        "Для групп выбранная категория будет общей для всех аккаунтов.",
        reply_markup=_category_keyboard(f"profile_pick_{target}"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("profile_pick_accounts_"))
async def list_random_accounts(callback: CallbackQuery):
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    parts = callback.data.split("_")
    if len(parts) not in {4, 5} or parts[3] not in CATEGORY_LABELS:
        await callback.answer("Выберите категорию", show_alert=True)
        return
    category = parts[3]
    page = max(0, int(parts[4])) if len(parts) == 5 and parts[4].isdigit() else 0
    async with session_scope() as session:
        accounts = await AccountRepository.get_all(session)
        counts = await pool_counts(session, category)
    pages = max(1, (len(accounts) + 9) // 10)
    page = min(page, pages - 1)
    rows = [
        [InlineKeyboardButton(
            text=f"#{account.id} {account.session_name[:27]}",
            callback_data=f"profile_pick_account_{category}_{account.id}",
        )]
        for account in accounts[page * 10:(page + 1) * 10]
    ]
    nav = []
    if page:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"profile_pick_accounts_{category}_{page - 1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"profile_pick_accounts_{category}_{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ Рандомизация", callback_data="profile_rand_menu")])
    await safe_edit_message(
        callback.message,
        f"🎲 <b>Аккаунты · {CATEGORY_LABELS[category]}</b>\n"
        f"Набор: {counts.get('name', 0)} имён, {counts.get('bio', 0)} BIO, {counts.get('photo', 0)} фото.\n"
        f"Всего аккаунтов: {len(accounts)} · страница {page + 1}/{pages}.\n\n"
        "Нажатие сразу применит случайный профиль.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("profile_pick_groups_"))
async def list_random_groups(callback: CallbackQuery):
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    parts = callback.data.split("_")
    if len(parts) not in {4, 5} or parts[3] not in CATEGORY_LABELS:
        await callback.answer("Выберите категорию", show_alert=True)
        return
    category = parts[3]
    page = max(0, int(parts[4])) if len(parts) == 5 and parts[4].isdigit() else 0
    async with session_scope() as session:
        groups = await GroupRepository.get_all(session)
        counts = await session.execute(
            select(account_groups.c.group_id, func.count(account_groups.c.account_id))
            .group_by(account_groups.c.group_id)
        )
        member_counts = dict(counts.all())
    pages = max(1, (len(groups) + 9) // 10)
    page = min(page, pages - 1)
    rows = [
        [InlineKeyboardButton(
            text=f"{group.name[:35]} ({member_counts.get(group.id, 0)})",
            callback_data=f"profile_group_confirm_{category}_{group.id}",
        )]
        for group in groups[page * 10:(page + 1) * 10]
    ]
    nav = []
    if page:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"profile_pick_groups_{category}_{page - 1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"profile_pick_groups_{category}_{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ Рандомизация", callback_data="profile_rand_menu")])
    await safe_edit_message(
        callback.message,
        f"👥 <b>Группы · {CATEGORY_LABELS[category]}</b>\n\n"
        f"Всего групп: {len(groups)} · страница {page + 1}/{pages}.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("profile_group_confirm_"))
async def confirm_random_group(callback: CallbackQuery):
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    parts = callback.data.split("_")
    if len(parts) != 5 or parts[3] not in CATEGORY_LABELS or not parts[4].isdigit():
        await callback.answer("Некорректный выбор", show_alert=True)
        return
    category, group_id = parts[3], int(parts[4])
    async with session_scope() as session:
        group = await GroupRepository.get_by_id(session, group_id)
    if group is None:
        await callback.answer("Группа не найдена", show_alert=True)
        return
    await safe_edit_message(
        callback.message,
        f"👥 <b>{escape(group.name)}</b>\n\n"
        f"Для каждого из {len(group.accounts)} аккаунтов будут выбраны случайные "
        f"имя, BIO и фото категории {CATEGORY_LABELS[category]}. Продолжить?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"profile_group_run_{category}_{group_id}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"profile_pick_groups_{category}")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("profile_group_run_"))
async def run_random_group(callback: CallbackQuery):
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    parts = callback.data.split("_")
    if len(parts) != 5 or parts[3] not in CATEGORY_LABELS or not parts[4].isdigit():
        await callback.answer("Некорректный выбор", show_alert=True)
        return
    category, group_id = parts[3], int(parts[4])
    if _group_random_lock.locked():
        await callback.answer("Уже выполняется рандомизация группы", show_alert=True)
        return
    async with _group_random_lock:
        async with session_scope() as session:
            group = await GroupRepository.get_by_id(session, group_id)
            counts = await pool_counts(session, category)
        if group is None:
            await callback.answer("Группа не найдена", show_alert=True)
            return
        account_ids = sorted(account.id for account in group.accounts)
        if not account_ids or not any(counts.values()):
            await callback.answer("Группа или набор пусты", show_alert=True)
            return
        await callback.answer()
        await safe_edit_message(callback.message, f"⏳ Рандомизация {len(account_ids)} аккаунтов…")
        succeeded = 0
        failed = 0
        for index, account_id in enumerate(account_ids):
            try:
                async with session_scope() as session:
                    selected = await random_profile(session, category)
                ok, _ = await _apply_profile(
                    account_id, name=selected["name"], bio=selected["bio"],
                    photo_file=selected["photo"],
                )
            except Exception as exc:
                log.warning("Group profile randomization failed for account {}: {}", account_id, type(exc).__name__)
                ok = False
            succeeded += int(ok)
            failed += int(not ok)
            if index + 1 < len(account_ids):
                await asyncio.sleep(GROUP_RANDOM_DELAY_SECONDS)
        await safe_edit_message(
            callback.message,
            f"{'✅' if failed == 0 else '⚠️'} Группа «{escape(group.name)}»: "
            f"успешно {succeeded} из {len(account_ids)}, ошибок {failed}.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ К группам", callback_data=f"profile_pick_groups_{category}")],
            ]),
        )


@router.callback_query(F.data.startswith("profile_set_"))
async def apply_template(callback: CallbackQuery):
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    account_id, template_id = map(int, callback.data.rsplit("_", 2)[-2:])
    await callback.answer()
    async with session_scope() as session:
        template = await get_template(session, template_id)
    if template is None:
        await callback.message.answer("Шаблон не найден.")
        return
    name = " ".join(filter(None, (template.first_name, template.last_name)))
    ok, detail = await _apply_profile(account_id, name=name, bio=template.bio, photo_file=template.photo_file)
    await safe_edit_message(
        callback.message, f"{'✅' if ok else '❌'} {escape(detail)}",
        reply_markup=get_edit_profile_keyboard(account_id),
    )
