"""Read-only check of a public link to a community administered by an account."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from io import BytesIO
from html import escape
import re

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.config import is_authorized_user
from database.repositories import AccountRepository
from database.session import session_scope
from services.community_link_checker import (
    check_owned_community_link,
    list_owned_community_link_checks,
    parse_community_link_batch,
)
from services.community_link_batch_jobs import (
    cancel_owned_community_batch,
    enqueue_owned_community_batch,
    get_owned_community_batch,
    list_owned_community_batches,
)


router = Router()
PAGE_SIZE = 10
_active_batches: dict[int, asyncio.Event] = {}


class CommunityCheckFlow(StatesGroup):
    waiting_link = State()
    waiting_batch = State()
    batch_preview = State()
    waiting_schedule_at = State()


def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="community_check_cancel")],
    ])


def _batch_preview_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Проверить", callback_data="community_check_batch_confirm")],
        [InlineKeyboardButton(text="⏲ Через 10 минут", callback_data="community_check_batch_schedule_600")],
        [InlineKeyboardButton(text="⏲ Через 1 час", callback_data="community_check_batch_schedule_3600")],
        [InlineKeyboardButton(text="⏲ Через 24 часа", callback_data="community_check_batch_schedule_86400")],
        [InlineKeyboardButton(text="📅 Выбрать время UTC", callback_data="community_check_batch_schedule_custom")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="community_check_cancel")],
    ])


def _batch_running_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏹ Остановить после текущей", callback_data="community_check_batch_stop")],
    ])


def _batch_job_keyboard(job: dict) -> InlineKeyboardMarkup:
    job_id = int(job["id"])
    rows = [[InlineKeyboardButton(
        text="🔄 Обновить статус", callback_data=f"community_check_job_show_{job_id}",
    )]]
    if job.get("status") in {"pending", "processing"}:
        rows.append([InlineKeyboardButton(
            text="⏹ Отменить задание", callback_data=f"community_check_job_cancel_{job_id}",
        )])
    rows.append([InlineKeyboardButton(text="📋 История проверок", callback_data="community_check_history")])
    rows.append([InlineKeyboardButton(text="🗓 Задания проверки", callback_data="community_check_jobs")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _batch_job_text(job: dict) -> str:
    statuses = {
        "pending": "ожидает запуска", "processing": "выполняется",
        "done": "завершено", "cancelled": "отменено", "failed": "ошибка",
    }
    status = statuses.get(str(job.get("status")), "неизвестно")
    checked = int(job.get("checked") or 0)
    total = int(job.get("total") or 0)
    owned_count = int(job.get("owned_count") or 0)
    other_count = int(job.get("other_count") or 0)
    lines = [
        f"🔗 <b>Пакетная проверка #{int(job['id'])}</b>",
        f"Статус: {status}",
        f"Проверено: {checked}/{total}; своих: {owned_count}; остальных: {other_count}.",
    ]
    if job.get("status") == "pending" and job.get("due_at"):
        due = escape(str(job["due_at"])).removesuffix("+00:00")[:32].replace("T", " ")
        lines.append(f"Запуск: {due} UTC.")
    titles = [escape(str(title))[:100] for title in (job.get("owned_titles") or [])[:20]]
    if titles:
        lines.extend(["", "Подтверждённые свои сообщества:", *(f"• {title}" for title in titles)])
    return "\n".join(lines)


async def _show_batch_job(callback: CallbackQuery, job: dict) -> None:
    try:
        await callback.message.edit_text(
            _batch_job_text(job), parse_mode="HTML", reply_markup=_batch_job_keyboard(job),
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


def _batch_preview_text(parsed: dict) -> str:
    lines = [f"🔗 <b>Пакетная проверка: {len(parsed['links'])} уникальных ссылок</b>"]
    lines.extend(f"{index}. <code>{escape(link)}</code>" for index, link in enumerate(parsed["links"], 1))
    if parsed["errors"]:
        numbers = ", ".join(str(item["line"]) for item in parsed["errors"])
        lines.append(f"❌ Недопустимые ссылки в строках: {numbers}.")
    if parsed["duplicates"]:
        numbers = ", ".join(str(item["line"]) for item in parsed["duplicates"])
        lines.append(f"Повторы в строках: {numbers} (пропущены).")
    return "\n".join(lines)


def _accounts_keyboard(accounts: list, page: int) -> InlineKeyboardMarkup:
    page_count = max(1, (len(accounts) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(page, 0), page_count - 1)
    rows = [
        [InlineKeyboardButton(
            text=f"{(account.list_label or account.username or account.phone or 'Аккаунт')[:52]} · #{account.id}",
            callback_data=f"community_check_account_{account.id}",
        )]
        for account in accounts[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    ]
    if not accounts:
        rows.append([InlineKeyboardButton(text="Нет активных аккаунтов", callback_data="community_check_noop")])
    if page_count > 1:
        navigation = []
        if page:
            navigation.append(InlineKeyboardButton(text="◀️", callback_data=f"community_check_page_{page - 1}"))
        navigation.append(InlineKeyboardButton(text=f"{page + 1}/{page_count}", callback_data="community_check_noop"))
        if page < page_count - 1:
            navigation.append(InlineKeyboardButton(text="▶️", callback_data=f"community_check_page_{page + 1}"))
        rows.append(navigation)
    rows.append([InlineKeyboardButton(text="📋 История проверок", callback_data="community_check_history")])
    rows.append([InlineKeyboardButton(text="🗓 Задания проверки", callback_data="community_check_jobs")])
    rows.append([InlineKeyboardButton(text="⬅️ К управлению", callback_data="accounts_manage")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _active_accounts() -> list:
    async with session_scope() as session:
        return await AccountRepository.get_active(session)


@router.callback_query(F.data == "community_check_start")
async def start_community_check(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    try:
        accounts = await _active_accounts()
    except Exception:
        await callback.answer("Не удалось загрузить аккаунты", show_alert=True)
        return
    await callback.message.edit_text(
        "🔗 <b>Проверка своего канала или супергруппы</b>\n\n"
        "Выберите аккаунт с правами администратора. Проверка только читает сведения о публичной ссылке; "
        "она не вступает в чат и ничего не отправляет.",
        parse_mode="HTML", reply_markup=_accounts_keyboard(accounts, 0),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("community_check_page_"))
async def community_check_page(callback: CallbackQuery) -> None:
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    try:
        page = int(callback.data.rsplit("_", 1)[1])
        accounts = await _active_accounts()
        await callback.message.edit_reply_markup(reply_markup=_accounts_keyboard(accounts, page))
    except Exception:
        await callback.answer("Не удалось обновить список", show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("community_check_account_"))
async def community_check_account(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    account_id = None
    try:
        account_id = int(callback.data.rsplit("_", 1)[1])
        accounts = await _active_accounts()
    except Exception:
        accounts = []
    if not any(account.id == account_id for account in accounts):
        await state.clear()
        await callback.answer("Аккаунт не активен. Начните заново.", show_alert=True)
        return
    await state.clear()
    await state.update_data(account_id=account_id)
    await state.set_state(CommunityCheckFlow.waiting_link)
    await callback.message.edit_text(
        "Пришлите публичную ссылку на собственный канал или супергруппу, например "
        "<code>https://t.me/mychannel</code>. Приглашения, ссылки на людей и телефоны не проверяются.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📚 Пакетная проверка", callback_data="community_check_batch_start")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="community_check_cancel")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data == "community_check_batch_start")
async def community_check_batch_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    data = await state.get_data()
    if "account_id" not in data:
        await callback.answer("Сначала выберите аккаунт.", show_alert=True)
        return
    await state.update_data(batch_raw=None)
    await state.set_state(CommunityCheckFlow.waiting_batch)
    await callback.message.edit_text(
        "Пришлите до 20 публичных ссылок на свои сообщества, по одной на строку, "
        "сообщением или файлом UTF-8 .txt (до 4096 символов).",
        reply_markup=_cancel_keyboard(),
    )
    await callback.answer()


@router.message(CommunityCheckFlow.waiting_batch)
async def community_check_batch_input(message: Message, state: FSMContext) -> None:
    if not is_authorized_user(message.from_user.id):
        return
    document = message.document
    if document is not None:
        if not (document.file_name or "").lower().endswith(".txt"):
            await message.answer("Нужен файл .txt в кодировке UTF-8.")
            return
        if document.file_size is not None and document.file_size > 16384:
            await message.answer("Файл слишком большой: максимум 4096 символов.")
            return
        buffer = BytesIO()
        try:
            await message.bot.download(document, destination=buffer)
            if len(buffer.getvalue()) > 16384:
                raise ValueError("file too large")
            raw = buffer.getvalue().decode("utf-8-sig", errors="strict")
        except (UnicodeDecodeError, ValueError):
            await message.answer("Нужен файл UTF-8 .txt объёмом до 4096 символов.")
            return
        except Exception:
            await message.answer("Не удалось прочитать файл. Отправьте ссылки сообщением.")
            return
    else:
        raw = message.text or ""
    try:
        parsed = parse_community_link_batch(raw)
    except ValueError:
        await message.answer("Слишком много данных: максимум 20 непустых строк и 4096 символов.")
        return
    if parsed["errors"] or not parsed["links"]:
        await message.answer(
            _batch_preview_text(parsed) + "\nИсправьте ссылки и отправьте список снова.",
            parse_mode="HTML",
        )
        return
    await state.update_data(batch_raw=raw)
    await state.set_state(CommunityCheckFlow.batch_preview)
    await message.answer(
        _batch_preview_text(parsed) + "\n\nПроверить эти ссылки выбранным аккаунтом?",
        parse_mode="HTML", reply_markup=_batch_preview_keyboard(),
    )


@router.callback_query(F.data == "community_check_batch_confirm")
async def community_check_batch_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    actor_id = callback.from_user.id
    if not is_authorized_user(actor_id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    if actor_id in _active_batches:
        await callback.answer("Проверка уже выполняется.", show_alert=True)
        return
    if await state.get_state() != CommunityCheckFlow.batch_preview.state:
        await callback.answer("Предпросмотр устарел. Отправьте список заново.", show_alert=True)
        return
    data = await state.get_data()
    try:
        account_id = int(data["account_id"])
        parsed = parse_community_link_batch(data["batch_raw"])
        accounts = await _active_accounts()
    except Exception:
        await callback.answer("Данные проверки устарели. Начните заново.", show_alert=True)
        return
    if parsed["errors"] or not parsed["links"] or not any(account.id == account_id for account in accounts):
        await callback.answer("Аккаунт или ссылки больше недоступны. Начните заново.", show_alert=True)
        return
    stop_event = asyncio.Event()
    _active_batches[actor_id] = stop_event
    try:
        await state.clear()
        await callback.answer("Проверка запущена.")
        links = parsed["links"]
        await callback.message.edit_text(
            f"Проверено 0/{len(links)}. Остановить после текущей проверки можно кнопкой ниже.",
            reply_markup=_batch_running_keyboard(),
        )
        owned: list[str] = []
        failed = 0
        checked = 0
        for link in links:
            if stop_event.is_set():
                break
            try:
                result = await check_owned_community_link(account_id, link, actor_id=actor_id)
            except Exception:
                result = {"status": "failed"}
            checked += 1
            if result.get("status") == "ok":
                title = escape(str(result.get("title") or ""))[:100]
                owned.append(f"• <b>{title}</b>")
            else:
                failed += 1
            if checked < len(links) and not stop_event.is_set():
                await callback.message.edit_text(
                    f"Проверено {checked}/{len(links)}. Подтверждённых своих: {len(owned)}; "
                    f"остальных: {failed}.",
                    reply_markup=_batch_running_keyboard(),
                )
        summary = f"Проверено {checked}/{len(links)}. Подтверждённых своих: {len(owned)}; остальных: {failed}."
        if stop_event.is_set():
            summary += "\nПроверка остановлена. Текущая операция могла завершиться."
        if owned:
            summary += "\n\n" + "\n".join(owned)
        await callback.message.edit_text(
            summary, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Проверить ещё", callback_data="community_check_start")],
                [InlineKeyboardButton(text="📋 История проверок", callback_data="community_check_history")],
            ]),
        )
    finally:
        _active_batches.pop(actor_id, None)


@router.callback_query(F.data == "community_check_batch_schedule_custom")
async def community_check_batch_schedule_custom(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    if await state.get_state() != CommunityCheckFlow.batch_preview.state:
        await callback.answer("Предпросмотр устарел. Отправьте список заново.", show_alert=True)
        return
    await state.set_state(CommunityCheckFlow.waiting_schedule_at)
    await callback.message.edit_text(
        "Пришлите дату и время запуска по UTC в формате <code>ГГГГ-ММ-ДД ЧЧ:ММ</code>. "
        "Время должно быть через 5 минут — 30 дней. Например: <code>2026-10-01 12:30</code>.",
        parse_mode="HTML", reply_markup=_cancel_keyboard(),
    )
    await callback.answer()


@router.message(CommunityCheckFlow.waiting_schedule_at)
async def community_check_batch_schedule_at(message: Message, state: FSMContext) -> None:
    if not is_authorized_user(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", raw):
        await message.answer("Укажите время по UTC в формате ГГГГ-ММ-ДД ЧЧ:ММ.")
        return
    try:
        due_at = datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        await message.answer("Некорректная дата. Укажите ГГГГ-ММ-ДД ЧЧ:ММ по UTC.")
        return
    try:
        data = await state.get_data()
        account_id = int(data["account_id"])
        parsed = parse_community_link_batch(data["batch_raw"])
        accounts = await _active_accounts()
        if parsed["errors"] or not parsed["links"] or not any(
            account.id == account_id for account in accounts
        ):
            raise KeyError("stale account or links")
    except (KeyError, TypeError, ValueError):
        await state.clear()
        await message.answer("Аккаунт или ссылки больше недоступны. Начните заново.")
        return
    try:
        job = await enqueue_owned_community_batch(
            account_id, message.from_user.id, parsed["links"], start_at_utc=due_at,
        )
    except ValueError:
        await message.answer("Выберите время через 5 минут — 30 дней по UTC.")
        return
    except Exception:
        await message.answer("Не удалось запланировать проверку. Попробуйте снова.")
        return
    await state.clear()
    await message.answer(
        _batch_job_text(job), parse_mode="HTML", reply_markup=_batch_job_keyboard(job),
    )


@router.callback_query(F.data.regexp(r"^community_check_batch_schedule_(600|3600|86400)$"))
async def community_check_batch_schedule(callback: CallbackQuery, state: FSMContext) -> None:
    actor_id = callback.from_user.id
    if not is_authorized_user(actor_id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    if await state.get_state() != CommunityCheckFlow.batch_preview.state:
        await callback.answer("Предпросмотр устарел. Отправьте список заново.", show_alert=True)
        return
    try:
        delay_seconds = int(callback.data.rsplit("_", 1)[1])
        if delay_seconds not in {600, 3600, 86400}:
            raise ValueError("unsupported delay")
        data = await state.get_data()
        account_id = int(data["account_id"])
        parsed = parse_community_link_batch(data["batch_raw"])
        accounts = await _active_accounts()
        if parsed["errors"] or not parsed["links"] or not any(
            account.id == account_id for account in accounts
        ):
            raise ValueError("stale account or links")
        job = await enqueue_owned_community_batch(
            account_id, actor_id, parsed["links"], delay_seconds,
        )
    except (KeyError, TypeError, ValueError):
        await callback.answer("Данные проверки устарели. Начните заново.", show_alert=True)
        return
    except Exception:
        await callback.answer("Не удалось запланировать проверку.", show_alert=True)
        return
    await state.clear()
    await _show_batch_job(callback, job)
    await callback.answer("Проверка запланирована.")


@router.callback_query(F.data.startswith("community_check_job_show_"))
async def community_check_job_show(callback: CallbackQuery) -> None:
    actor_id = callback.from_user.id
    if not is_authorized_user(actor_id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    try:
        job_id = int(callback.data.rsplit("_", 1)[1])
        job = await get_owned_community_batch(job_id, actor_id)
    except (TypeError, ValueError):
        job = None
    except Exception:
        await callback.answer("Не удалось загрузить задание.", show_alert=True)
        return
    if job is None:
        await callback.answer("Задание не найдено.", show_alert=True)
        return
    await _show_batch_job(callback, job)
    await callback.answer()


@router.callback_query(F.data == "community_check_jobs")
async def community_check_jobs(callback: CallbackQuery) -> None:
    actor_id = callback.from_user.id
    if not is_authorized_user(actor_id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    try:
        jobs = await list_owned_community_batches(actor_id, limit=20)
    except Exception:
        await callback.answer("Не удалось загрузить задания.", show_alert=True)
        return
    status_names = {
        "pending": "ожидает", "processing": "выполняется", "done": "завершено",
        "cancelled": "отменено", "failed": "ошибка",
    }
    lines = ["🗓 <b>Последние задания проверки</b>"]
    buttons = []
    for job in jobs:
        created = escape(str(job.get("created_at") or ""))[:16].replace("T", " ")
        status = status_names.get(str(job.get("status")), "неизвестно")
        job_id = int(job["id"])
        lines.append(
            f"#{job_id} · {created} UTC · {status} · "
            f"{int(job.get('checked') or 0)}/{int(job.get('total') or 0)}"
        )
        buttons.append([InlineKeyboardButton(
            text=f"#{job_id} · {status}", callback_data=f"community_check_job_show_{job_id}",
        )])
    if not jobs:
        lines.append("Заданий пока нет.")
    buttons.append([InlineKeyboardButton(text="⬅️ К проверке", callback_data="community_check_start")])
    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("community_check_job_cancel_"))
async def community_check_job_cancel(callback: CallbackQuery) -> None:
    actor_id = callback.from_user.id
    if not is_authorized_user(actor_id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    try:
        job_id = int(callback.data.rsplit("_", 1)[1])
        job = await cancel_owned_community_batch(job_id, actor_id)
    except (TypeError, ValueError):
        job = None
    except Exception:
        await callback.answer("Не удалось отменить задание.", show_alert=True)
        return
    if job is None:
        await callback.answer("Задание не найдено.", show_alert=True)
        return
    await _show_batch_job(callback, job)
    await callback.answer("Отмена сохранена. Текущая проверка может завершиться.")


@router.callback_query(F.data == "community_check_batch_stop")
async def community_check_batch_stop(callback: CallbackQuery) -> None:
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    stop_event = _active_batches.get(callback.from_user.id)
    if stop_event is None:
        await callback.answer("Активной проверки нет.")
        return
    stop_event.set()
    await callback.answer("Остановка запрошена. Текущая проверка может завершиться.", show_alert=True)


@router.message(CommunityCheckFlow.waiting_link)
async def community_check_link(message: Message, state: FSMContext) -> None:
    if not is_authorized_user(message.from_user.id):
        return
    link = (message.text or "").strip()
    if len(link) > 255:
        await message.answer("Ссылка слишком длинная. Отправьте публичную ссылку t.me на свой канал.")
        return
    data = await state.get_data()
    await state.clear()
    try:
        result = await check_owned_community_link(
            int(data["account_id"]), link, actor_id=message.from_user.id,
        )
    except Exception:
        result = {"status": "failed", "reason": "check_unavailable"}
    if result.get("status") == "ok":
        kind = "Канал" if result.get("kind") == "channel" else "Супергруппа"
        title = escape(str(result.get("title") or ""))[:160]
        username = escape(str(result.get("username") or ""))[:40]
        account = escape(str(result.get("account_name") or ""))[:80]
        body = (f"✅ {kind} доступен выбранному администратору.\n"
                f"Название: <b>{title}</b>\nАдрес: @{username}\nАккаунт: {account}")
    else:
        reasons = {
            "invalid_link": "Нужна публичная ссылка https://t.me/username на своё сообщество.",
            "account_not_found": "Аккаунт не найден.",
            "session_unavailable": "Сессия аккаунта недоступна.",
            "account_unavailable": "Аккаунт сейчас недоступен.",
            "unavailable_or_not_owned": "Сообщество недоступно или аккаунт не является его администратором.",
            "check_unavailable": "Проверка временно недоступна.",
        }
        body = "❌ " + reasons.get(str(result.get("reason")), "Проверка не выполнена.")
    await message.answer(
        body, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Проверить ещё", callback_data="community_check_start")],
            [InlineKeyboardButton(text="📋 История проверок", callback_data="community_check_history")],
            [InlineKeyboardButton(text="⬅️ К управлению", callback_data="accounts_manage")],
        ]),
    )


@router.callback_query(F.data == "community_check_history")
async def community_check_history(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    try:
        entries = await list_owned_community_link_checks(limit=20, actor_id=callback.from_user.id)
    except Exception:
        await callback.answer("Не удалось загрузить историю", show_alert=True)
        return
    lines = ["📋 <b>Последние проверки своих сообществ</b>"]
    for entry in entries:
        when = escape(str(entry.get("checked_at") or ""))[:19].replace("T", " ")
        link = escape(str(entry.get("canonical_link") or ""))[:255]
        title = escape(str(entry.get("title") or ""))[:100]
        is_ok = entry.get("status") == "ok"
        marker = "✅" if is_ok else "❌"
        lines.append(f"\n{marker} {when} UTC · аккаунт #{int(entry['account_id'])}")
        lines.append(f"<code>{link}</code>")
        if is_ok and title:
            lines.append(f"{title}")
    if not entries:
        lines.append("\nПроверок пока нет.")
    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="community_check_history")],
            [InlineKeyboardButton(text="⬅️ К проверке", callback_data="community_check_start")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data == "community_check_cancel")
async def community_check_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(
        "Проверка отменена.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ К управлению", callback_data="accounts_manage")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data == "community_check_noop")
async def community_check_noop(callback: CallbackQuery) -> None:
    await callback.answer()
