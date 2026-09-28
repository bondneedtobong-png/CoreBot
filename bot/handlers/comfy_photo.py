"""Generate account photo previews locally and add approved images to the pool."""
from __future__ import annotations

from html import escape
from pathlib import Path

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.config import DATA_DIR, is_authorized_user
from bot.handlers.accounts.profile_templates import _save_photo
from database.profile_templates import add_pool_batch
from database.session import session_scope
from services.comfyui import ComfyPreviewError, generate_profile_preview

router = Router()
_PREVIEW_DIR = DATA_DIR / "comfy_previews"
_NEGATIVE = "blurry, low quality, watermark, text, distorted face, extra limbs"


class ComfyPhotoFlow(StatesGroup):
    waiting_prompt = State()
    waiting_approval = State()


def _keyboard(*rows: tuple[str, str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data)] for label, data in rows
    ])


def _safe_preview_path(raw: str, token: str) -> Path | None:
    if len(token) != 12 or any(ch not in "0123456789abcdef" for ch in token):
        return None
    candidate = Path(raw)
    if candidate.suffix.lower() != ".png" or not candidate.is_file():
        return None
    parent = _PREVIEW_DIR.resolve()
    resolved = candidate.resolve()
    if resolved.parent != parent or resolved.stem[:12] != token:
        return None
    return resolved


@router.callback_query(F.data == "ai_comfy_photo")
async def start_comfy_photo(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    await state.set_state(ComfyPhotoFlow.waiting_prompt)
    await callback.message.edit_text(
        "📸 <b>Генерация фото в ComfyUI</b>\n\n"
        "Опишите фото для аккаунта одним сообщением (до 700 символов). "
        "Бот покажет результат до добавления в набор фото. "
        "Telegram-профиль сам не изменится.",
        reply_markup=_keyboard(("⬅️ К ИИ", "menu_ai")),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(ComfyPhotoFlow.waiting_prompt)
async def receive_comfy_prompt(message: Message, state: FSMContext) -> None:
    if not is_authorized_user(message.from_user.id):
        return
    prompt = (message.text or "").strip()
    if not 1 <= len(prompt) <= 700:
        await message.answer("Опишите фото текстом длиной от 1 до 700 символов.")
        return
    status = await message.answer("⏳ Генерирую фото в локальном ComfyUI. Это может занять около минуты.")
    try:
        result = await generate_profile_preview(prompt, negative_prompt=_NEGATIVE)
    except (ComfyPreviewError, ValueError):
        await status.edit_text(
            "❌ Не удалось получить фото: локальная ComfyUI не запустилась или генерация завершилась ошибкой. Попробуйте снова."
        )
        return
    token = result.path.stem[:12]
    await state.update_data(preview_path=str(result.path), preview_token=token)
    await state.set_state(ComfyPhotoFlow.waiting_approval)
    await status.edit_text("✅ Фото готово. Просмотрите его перед добавлением в набор.")
    await message.answer_photo(
        FSInputFile(result.path),
        caption=(
            "Предпросмотр фото для аккаунтов.\n"
            f"Модель: {escape(result.model)} · seed: {result.seed}\n"
            "Добавление в набор не меняет профиль Telegram."
        ),
        reply_markup=_keyboard(
            ("✅ Добавить в набор фото", f"ai_comfy_save_{token}"),
            ("🔄 Новое фото", "ai_comfy_photo"),
            ("⬅️ К ИИ", "menu_ai"),
        ),
    )


@router.callback_query(F.data.startswith("ai_comfy_save_"))
async def save_comfy_photo(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    data = await state.get_data()
    token = callback.data.removeprefix("ai_comfy_save_")
    if token != data.get("preview_token"):
        await callback.answer("Предпросмотр устарел. Создайте новое фото.", show_alert=True)
        return
    source = _safe_preview_path(data.get("preview_path", ""), token)
    if source is None:
        await callback.answer("Файл предпросмотра не найден.", show_alert=True)
        return
    try:
        copied_name = _save_photo(source)
    except (OSError, ValueError):
        await callback.answer("Фото не удалось сохранить в набор (размер или формат).", show_alert=True)
        return
    try:
        async with session_scope() as session:
            await add_pool_batch(session, {"photo": [copied_name]})
    except Exception:
        (DATA_DIR / "profile_assets" / copied_name).unlink(missing_ok=True)
        await callback.answer("Не удалось добавить фото в набор.", show_alert=True)
        return
    await state.clear()
    await callback.message.answer(
        "✅ Фото добавлено в набор для рандомизации. Профили аккаунтов не изменены.",
        reply_markup=_keyboard(("📸 Создать ещё", "ai_comfy_photo"), ("⬅️ К ИИ", "menu_ai")),
    )
    await callback.answer("Сохранено")
