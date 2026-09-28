"""Bot flow for fictional ComfyUI characters and reviewed scene photos."""
from __future__ import annotations

import asyncio
from html import escape
from pathlib import Path
from time import time
from uuid import uuid4

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message
from PIL import Image, ImageFilter, ImageOps, UnidentifiedImageError

from bot.config import DATA_DIR, is_authorized_user
from bot.handlers.accounts.profile_templates import _save_photo
from database.profile_templates import add_pool_batch
from database.session import session_scope
from services.comfyui.identity import (
    ComfyIdentityError, ComfyIdentityModelMissingError, ComfyIdentityQualityError,
    ComfyIdentityTranslationError, create_identity,
    generate_identity_photo, get_identity, list_identities,
)
from services.comfyui.person_mask import PersonMaskError, generate_person_mask


router = Router()
_TEMP_DIR = DATA_DIR / "comfy_templates"
_IDENTITY_DIR = DATA_DIR / "comfy_identities"
_MAX_UPLOAD = 10_000_000
Image.MAX_IMAGE_PIXELS = 16_000_000


class PersonFlow(StatesGroup):
    waiting_appearance = State()
    waiting_scene = State()
    waiting_template = State()
    waiting_mask = State()
    waiting_photo_approval = State()


def _buttons(*rows: tuple[str, str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data)] for label, data in rows
    ])


def _old_temp_files() -> None:
    if not _TEMP_DIR.exists():
        return
    cutoff = time() - 24 * 3600
    for path in _TEMP_DIR.iterdir():
        if path.is_file() and path.suffix.lower() in {".jpg", ".png"} and path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)


def _review_path(raw: str, identity_id: str, token: str) -> Path | None:
    try:
        path = Path(raw).resolve()
        parent = (_IDENTITY_DIR / identity_id).resolve()
        if path.is_file() and path.suffix.lower() == ".png" and path.is_relative_to(parent):
            if len(token) == 12 and path.stem.startswith(token):
                return path
    except (OSError, ValueError):
        pass
    return None


def _prepare_template(image_path: Path, mask_path: Path) -> tuple[Path, Path]:
    """Apply the same center crop to a photo and its binary person mask."""
    with Image.open(image_path) as photo_file, Image.open(mask_path) as mask_file:
        photo = ImageOps.exif_transpose(photo_file).convert("RGB")
        mask = ImageOps.exif_transpose(mask_file).convert("L")
        if photo.size != mask.size or min(photo.size) < 512 or max(photo.size) > 4096:
            raise ValueError("template and mask must match and be 512–4096 pixels")
        photo = ImageOps.fit(photo, (1024, 1024), method=Image.Resampling.LANCZOS)
        mask = ImageOps.fit(mask, (1024, 1024), method=Image.Resampling.NEAREST)
        mask = mask.point(lambda value: 255 if value >= 128 else 0)
        marked_pixels = mask.histogram()[255]
        if not 0.02 * 1024 * 1024 <= marked_pixels <= 0.95 * 1024 * 1024:
            raise ValueError("person mask must cover a visible part of the photo")
        mask = mask.filter(ImageFilter.GaussianBlur(radius=8))
        prepared_image = _TEMP_DIR / f"{uuid4().hex}.png"
        prepared_mask = _TEMP_DIR / f"{uuid4().hex}.png"
        try:
            photo.save(prepared_image, format="PNG")
            mask.save(prepared_mask, format="PNG")
        except OSError:
            prepared_image.unlink(missing_ok=True)
            prepared_mask.unlink(missing_ok=True)
            raise
    if prepared_image.stat().st_size > _MAX_UPLOAD or prepared_mask.stat().st_size > _MAX_UPLOAD:
        prepared_image.unlink(missing_ok=True)
        prepared_mask.unlink(missing_ok=True)
        raise ValueError("prepared files are too large")
    return prepared_image, prepared_mask


def _mask_preview(image_path: Path, mask_path: Path, output_path: Path) -> None:
    """Show the replacement area in red over the original photo."""
    with Image.open(image_path) as photo_file, Image.open(mask_path) as mask_file:
        photo = ImageOps.exif_transpose(photo_file).convert("RGB")
        mask = ImageOps.exif_transpose(mask_file).convert("L")
        if photo.size != mask.size:
            raise ValueError("person mask dimensions do not match the template")
        marked = Image.composite(Image.new("RGB", photo.size, (255, 55, 55)), photo, mask)
        preview = Image.blend(photo, marked, 0.48)
        preview.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
        preview.save(output_path, format="PNG")


def _temp_file(raw: str) -> Path | None:
    try:
        path = Path(raw).resolve(strict=True)
        if path.is_file() and path.is_relative_to(_TEMP_DIR.resolve()):
            return path
    except (OSError, ValueError, TypeError):
        pass
    return None


def _clear_template_files(data: dict) -> None:
    for key in ("template_path", "auto_mask_path", "mask_preview_path"):
        path = _temp_file(data.get(key, ""))
        if path is not None:
            path.unlink(missing_ok=True)


@router.callback_query(F.data == "ai_comfy_identities")
async def show_identities(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    identities = list_identities(limit=20)
    rows = [[InlineKeyboardButton(
        text=f"👤 {item.identity_id[:8]} · {item.prompt[:35]}",
        callback_data=f"ai_person_pick_{item.identity_id}",
    )] for item in identities]
    rows.extend([
        [InlineKeyboardButton(text="➕ Создать вымышленную внешность", callback_data="ai_person_create")],
        [InlineKeyboardButton(text="⬅️ К ИИ", callback_data="menu_ai")],
    ])
    await callback.message.edit_text(
        "🧑 <b>Вымышленные персонажи</b>\n\n"
        "Сначала создайте внешность, затем снимайте того же персонажа в новых сценах. "
        "Проверяйте лицо на каждом результате: генеративная модель может немного менять черты. "
        "Для фото-шаблона бот предложит автоматическую маску человека с предварительным просмотром.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "ai_person_create")
async def start_identity(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    await state.set_state(PersonFlow.waiting_appearance)
    await callback.message.edit_text(
        "Опишите внешность <b>вымышленного взрослого человека</b>: возраст, волосы, лицо, стиль. "
        "Не указывайте настоящего человека или знаменитость. До 600 символов.\n\n"
        "ComfyUI запустится сама, если она выключена.",
        reply_markup=_buttons(("⬅️ Персонажи", "ai_comfy_identities")), parse_mode="HTML",
    )
    await callback.answer()


@router.message(PersonFlow.waiting_appearance)
async def receive_appearance(message: Message, state: FSMContext) -> None:
    if not is_authorized_user(message.from_user.id):
        return
    description = (message.text or "").strip()
    if not 3 <= len(description) <= 600:
        await message.answer("Нужно описание внешности от 3 до 600 символов.")
        return
    status = await message.answer("⏳ Запускаю ComfyUI при необходимости и создаю вымышленную внешность. Это может занять несколько минут.")
    try:
        record = await create_identity(description)
    except ComfyIdentityTranslationError:
        await status.edit_text(
            "❌ Локальный перевод описания недоступен. Попробуйте написать внешность по-английски."
        )
        return
    except ComfyIdentityError:
        await status.edit_text("❌ Не удалось создать внешность. Проверьте локальную установку ComfyUI и попробуйте ещё раз.")
        return
    await state.clear()
    await status.edit_text("✅ Внешность создана. Проверьте портрет, прежде чем создавать другие сцены.")
    await message.answer_photo(
        FSInputFile(record.reference_path),
        caption=f"Вымышленный персонаж #{record.identity_id[:8]} · seed {record.seed}",
        reply_markup=_buttons(
            ("📷 Новая сцена", f"ai_person_pick_{record.identity_id}"),
            ("👥 Все персонажи", "ai_comfy_identities"),
        ),
    )


@router.callback_query(F.data.startswith("ai_person_pick_"))
async def choose_identity(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    identity_id = callback.data.removeprefix("ai_person_pick_")
    try:
        record = get_identity(identity_id)
    except ValueError:
        record = None
    if record is None:
        await callback.answer("Персонаж не найден", show_alert=True)
        return
    await state.clear()
    await state.update_data(identity_id=identity_id)
    await state.set_state(PersonFlow.waiting_scene)
    await callback.message.answer(
        f"📷 Персонаж <b>#{identity_id[:8]}</b>. Опишите новую ситуацию, одежду и место (до 700 символов). "
        "После этого можно использовать своё фото как шаблон сцены.",
        reply_markup=_buttons(("⬅️ Персонажи", "ai_comfy_identities")), parse_mode="HTML",
    )
    await callback.answer()


@router.message(PersonFlow.waiting_scene)
async def receive_scene(message: Message, state: FSMContext) -> None:
    if not is_authorized_user(message.from_user.id):
        return
    scene = (message.text or "").strip()
    if not 3 <= len(scene) <= 700:
        await message.answer("Опишите сцену текстом от 3 до 700 символов.")
        return
    await state.update_data(scene_prompt=scene)
    await message.answer(
        "Для новой ситуации выберите «Сцена по описанию». «По фото-шаблону» сохраняет "
        "композицию загруженного фото; используйте только снимок, на который у вас есть право. "
        "Бот покажет область замены перед генерацией.",
        reply_markup=_buttons(
            ("🎨 Сцена по описанию", "ai_person_generate"),
            ("🖼 По фото-шаблону", "ai_person_template"),
            ("⬅️ Персонажи", "ai_comfy_identities"),
        ),
    )


async def _generate(message: Message, state: FSMContext, *, template: Path | None = None, mask: Path | None = None) -> None:
    data = await state.get_data()
    identity_id = data.get("identity_id", "")
    scene = data.get("scene_prompt", "")
    if not get_identity(identity_id) or not scene:
        await message.answer("Сцена устарела. Выберите персонажа заново.")
        return
    status = await message.answer("⏳ Генерирую новую фотографию персонажа…")
    try:
        result = await generate_identity_photo(
            identity_id, scene, scene_template_path=template, person_mask_path=mask,
        )
    except ComfyIdentityModelMissingError:
        await status.edit_text("❌ Модель сохранения внешности PhotoMaker не найдена в локальном ComfyUI.")
        return
    except ComfyIdentityQualityError:
        await status.edit_text(
            "❌ Кадр не прошёл проверку пропорций лица. Попробуйте другой фото-шаблон "
            "или повторите генерацию. Испорченный результат не сохранён."
        )
        return
    except ComfyIdentityTranslationError:
        await status.edit_text(
            "❌ Локальный перевод описания сцены недоступен. Попробуйте написать сцену по-английски."
        )
        return
    except ComfyIdentityError:
        await status.edit_text("❌ Не удалось создать сцену. Проверьте шаблон и маску или повторите без них.")
        return
    token = result.path.stem[:12]
    await state.update_data(result_path=str(result.path), result_token=token)
    await state.set_state(PersonFlow.waiting_photo_approval)
    await status.edit_text("✅ Снимок готов. Проверьте лицо и сцену перед использованием в аккаунте.")
    actions = [
        ("✅ В набор Ж", f"ai_person_save_f_{token}"),
        ("✅ В набор М", f"ai_person_save_m_{token}"),
        ("✅ В набор −", f"ai_person_save_u_{token}"),
    ]
    if template is None:
        actions.append(("🎲 Ещё вариант", "ai_person_reroll"))
    actions.append(("🔄 Другая сцена", f"ai_person_pick_{identity_id}"))
    await message.answer_photo(
        FSInputFile(result.path),
        caption=f"Персонаж #{identity_id[:8]} · seed {result.seed}. Изображение создано ИИ.",
        reply_markup=_buttons(*actions),
    )


@router.callback_query(F.data == "ai_person_generate")
async def generate_scene(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await callback.answer()
    await _generate(callback.message, state)


@router.callback_query(F.data == "ai_person_reroll", PersonFlow.waiting_photo_approval)
async def reroll_scene(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await callback.answer()
    await _generate(callback.message, state)


@router.callback_query(F.data == "ai_person_template")
async def start_template(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    data = await state.get_data()
    if not data.get("identity_id") or not data.get("scene_prompt"):
        await callback.answer("Сначала выберите персонажа и сцену", show_alert=True)
        return
    _old_temp_files()
    await state.set_state(PersonFlow.waiting_template)
    await callback.message.answer(
        "Пришлите фото-шаблон как изображение или JPEG/PNG-документ до 10 МБ. "
        "Я автоматически выделю человека и покажу маску для проверки. "
        "Если граница окажется неточной, можно прислать свою маску PNG.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(PersonFlow.waiting_template)
async def receive_template(message: Message, state: FSMContext) -> None:
    if not is_authorized_user(message.from_user.id):
        return
    source = message.photo[-1] if message.photo else message.document
    if not source or (source.file_size or 0) > _MAX_UPLOAD:
        await message.answer("Пришлите JPEG/PNG-фото до 10 МБ.")
        return
    suffix = ".jpg" if message.photo else Path(message.document.file_name or "").suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png"}:
        await message.answer("Нужен JPEG или PNG.")
        return
    _TEMP_DIR.mkdir(parents=True, exist_ok=True)
    target = _TEMP_DIR / f"{uuid4().hex}{suffix}"
    try:
        await message.bot.download(source, destination=target)
        if target.stat().st_size > _MAX_UPLOAD:
            raise ValueError("file too large")
    except (OSError, ValueError):
        target.unlink(missing_ok=True)
        await message.answer("Не удалось загрузить фото-шаблон.")
        return
    await state.update_data(template_path=str(target), auto_mask_path="", mask_preview_path="")
    await state.set_state(PersonFlow.waiting_mask)
    status = await message.answer("⏳ Выделяю человека на фото. Первый запуск может занять немного больше времени.")
    mask = _TEMP_DIR / f"{uuid4().hex}.png"
    preview_path = _TEMP_DIR / f"{uuid4().hex}.png"
    try:
        await generate_person_mask(target, mask)
        await asyncio.to_thread(_mask_preview, target, mask, preview_path)
        await state.update_data(auto_mask_path=str(mask), mask_preview_path=str(preview_path))
        await status.edit_text("Проверьте красную область: она будет заменена, остальное фото сохранится.")
        await message.answer_photo(
            FSInputFile(preview_path),
            caption="Если волосы, руки или одежда выделены неверно, пришлите свою маску PNG документом.",
            reply_markup=_buttons(
                ("✅ Использовать маску", "ai_person_mask_accept"),
                ("✏️ Загрузить свою", "ai_person_mask_manual"),
            ),
        )
    except (PersonMaskError, OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        mask.unlink(missing_ok=True)
        preview_path.unlink(missing_ok=True)
        await status.edit_text(
            "Автоматически выделить одного человека не удалось. Пришлите маску PNG документом: "
            "белым — весь человек, чёрным — фон; размер должен совпадать с фото."
        )


@router.callback_query(F.data == "ai_person_mask_manual", PersonFlow.waiting_mask)
async def choose_manual_mask(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await callback.message.answer(
        "Пришлите маску PNG документом: белым закрасьте всего человека, чёрным оставьте фон. "
        "Размер должен совпадать с фото."
    )
    await callback.answer()


@router.callback_query(F.data == "ai_person_mask_accept", PersonFlow.waiting_mask)
async def accept_auto_mask(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    data = await state.get_data()
    template = _temp_file(data.get("template_path", ""))
    mask = _temp_file(data.get("auto_mask_path", ""))
    if template is None or mask is None:
        await callback.answer("Фото или маска уже недоступны. Загрузите шаблон заново.", show_alert=True)
        return
    await callback.answer()
    prepared: tuple[Path, Path] | None = None
    try:
        prepared = await asyncio.to_thread(_prepare_template, template, mask)
        await _generate(callback.message, state, template=prepared[0], mask=prepared[1])
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        await callback.message.answer("Автоматическая маска некорректна. Пришлите свою маску PNG документом.")
    finally:
        _clear_template_files(data)
        if prepared:
            prepared[0].unlink(missing_ok=True)
            prepared[1].unlink(missing_ok=True)


@router.message(PersonFlow.waiting_mask)
async def receive_mask(message: Message, state: FSMContext) -> None:
    if not is_authorized_user(message.from_user.id):
        return
    source = message.document
    if not source or not (source.file_name or "").lower().endswith(".png") or (source.file_size or 0) > _MAX_UPLOAD:
        await message.answer("Пришлите маску именно PNG-документом до 10 МБ.")
        return
    target = _TEMP_DIR / f"{uuid4().hex}.png"
    data = await state.get_data()
    template = _temp_file(data.get("template_path", ""))
    prepared: tuple[Path, Path] | None = None
    try:
        await message.bot.download(source, destination=target)
        if target.stat().st_size > _MAX_UPLOAD or template is None:
            raise ValueError("file missing or too large")
        prepared = await asyncio.to_thread(_prepare_template, template, target)
        await _generate(message, state, template=prepared[0], mask=prepared[1])
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        await message.answer("Фото и маска должны быть корректными изображениями одинакового размера (не меньше 512 пикселей).")
    finally:
        target.unlink(missing_ok=True)
        _clear_template_files(data)
        if prepared:
            prepared[0].unlink(missing_ok=True)
            prepared[1].unlink(missing_ok=True)


@router.callback_query(F.data.startswith("ai_person_save_"), PersonFlow.waiting_photo_approval)
async def save_person_photo(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    parts = callback.data.split("_")
    if len(parts) != 5 or parts[3] not in {"m", "f", "u"}:
        await callback.answer("Неверная категория", show_alert=True)
        return
    category, token = parts[3], parts[4]
    data = await state.get_data()
    if token != data.get("result_token"):
        await callback.answer("Фото устарело", show_alert=True)
        return
    source = _review_path(data.get("result_path", ""), data.get("identity_id", ""), token)
    if source is None:
        await callback.answer("Файл фото не найден", show_alert=True)
        return
    try:
        copied = _save_photo(source)
        async with session_scope() as session:
            await add_pool_batch(session, {"photo": [copied]}, category=category)
    except Exception:
        if "copied" in locals():
            (DATA_DIR / "profile_assets" / copied).unlink(missing_ok=True)
        await callback.answer("Не удалось добавить фото в набор", show_alert=True)
        return
    await state.clear()
    await callback.message.answer(
        f"✅ Фото добавлено в набор {escape({'m': 'М', 'f': 'Ж', 'u': '−'}[category])}. "
        "Профиль Telegram пока не менялся.",
        reply_markup=_buttons(("👥 Персонажи", "ai_comfy_identities"), ("⬅️ К ИИ", "menu_ai")),
    )
    await callback.answer("Сохранено")
