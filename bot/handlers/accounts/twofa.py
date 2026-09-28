"""Owner-controlled 2FA setup and password rotation through leased Telethon sessions."""

from __future__ import annotations

import html
from dataclasses import dataclass

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from telethon import functions

from bot.config import SESSIONS_DIR, is_authorized_user
from bot.handlers.accounts.common import safe_edit_message
from database.models import ProxyType
from database.repositories import AccountRepository
from database.session import session_scope
from utils.logger import log

router = Router()
PAGE_SIZE = 10
_CANCEL = {"/start", "отмена", "cancel"}
_SKIP_CURRENT = {"-", "пропустить", "нет"}


class Fleet2FAFSM(StatesGroup):
    new_password = State()
    confirm_password = State()
    current_password = State()
    processing = State()


@dataclass(frozen=True)
class TwoFAResult:
    account_id: int
    label: str
    outcome: str  # updated | skipped | error
    detail: str


def _keyboard(*rows: tuple[str, str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=callback)]
            for label, callback in rows
        ]
    )


async def _accounts():
    async with session_scope() as session:
        return await AccountRepository.get_all(session)


def _account_label(account) -> str:
    return f"#{account.id} {account.display_title}"


async def _remove_password_message(message: Message) -> bool:
    try:
        await message.delete()
        return True
    except Exception:
        log.warning("Could not remove a 2FA password message from the bot chat")
        return False


async def _require_deleted_password_message(message: Message, state: FSMContext) -> bool:
    if await _remove_password_message(message):
        return True
    await state.clear()
    await message.answer(
        "❌ Не удалось удалить сообщение с паролем. Операция отменена; "
        "удалите сообщение вручную и начните заново."
    )
    return False


async def _with_client(account, action):
    """Verify proxy/session and borrow the running client or obtain its session lease."""
    from workers.manager import _BorrowedWorker, account_worker_for_action

    proxy = account.proxy
    if proxy is None or proxy.proxy_type != ProxyType.SOCKS5:
        raise RuntimeError("нет SOCKS5-прокси")
    if not proxy.is_active or not proxy.is_working:
        raise RuntimeError("прокси отключён или не прошёл проверку")
    path = SESSIONS_DIR / f"{account.session_name}.session"
    if not path.is_file():
        raise RuntimeError("файл сессии отсутствует")
    worker = account_worker_for_action(account, path, proxy)

    async def use_client():
        if not await worker.connect(quiet=True) or not worker.client:
            raise RuntimeError("сессия занята, не авторизована или недоступна")
        if not await worker.client.is_user_authorized():
            raise RuntimeError("сессия не авторизована")
        return await action(worker.client)

    try:
        if isinstance(worker, _BorrowedWorker):
            # The borrowed wrapper's connect/disconnect are lock-free no-ops.
            # Hold the owner's locks while checking auth and changing 2FA.
            async with worker._send_lock:
                async with worker._connection_lock:
                    return await use_client()
        return await use_client()
    finally:
        try:
            if not await worker.disconnect():
                log.warning("2FA client cleanup failed account_id={}", account.id)
        except Exception as exc:
            log.warning(
                "2FA client cleanup failed account_id={}: {}",
                account.id,
                type(exc).__name__,
            )


async def _has_2fa(client) -> bool:
    status = await client(functions.account.GetPasswordRequest())
    return bool(status.has_password)


async def _probe_account(account) -> tuple[str, str]:
    try:
        enabled = await _with_client(account, _has_2fa)
        return ("✅", "включена") if enabled else ("❌", "не установлена")
    except Exception as exc:
        log.warning("2FA status check account_id={} failed: {}", account.id, type(exc).__name__)
        return "⚠️", "не удалось проверить"


async def _apply_account(account, new_password: str, current_password: str | None) -> TwoFAResult:
    label = _account_label(account)

    async def change(client):
        if await _has_2fa(client):
            if not current_password:
                return TwoFAResult(account.id, label, "skipped", "нужен текущий пароль")
            ok = await client.edit_2fa(
                current_password=current_password,
                new_password=new_password,
                hint="Сохраните пароль в менеджере паролей",
            )
        else:
            ok = await client.edit_2fa(
                new_password=new_password,
                hint="Сохраните пароль в менеджере паролей",
            )
        if not ok or not await _has_2fa(client):
            raise RuntimeError("Telegram не подтвердил установку 2FA")
        return TwoFAResult(account.id, label, "updated", "2FA подтверждена Telegram")

    try:
        return await _with_client(account, change)
    except Exception as exc:
        # Never format exception text: external libraries may include supplied secrets.
        log.warning("2FA change account_id={} failed: {}", account.id, type(exc).__name__)
        detail = (
            "текущий пароль не подошёл"
            if type(exc).__name__ == "PasswordHashInvalidError"
            else "проверка сессии, прокси или Telegram завершилась ошибкой"
        )
        return TwoFAResult(account.id, label, "error", detail)


async def render_twofa_menu(message: Message) -> None:
    accounts = await _accounts()
    await safe_edit_message(
        message,
        "🔐 <b>2FA аккаунтов</b>\n\n"
        f"Аккаунтов: <b>{len(accounts)}</b>. Статус в списке запрашивается у Telegram.\n"
        "Для замены 2FA потребуется действующий пароль. Новый и текущий пароли "
        "не сохраняются в базе и удаляются из чата после ввода.",
        reply_markup=_keyboard(
            ("🔐 Установить всем", "twofa_all"),
            ("📋 Установить из списка", "twofa_list_p_0"),
            ("🔁 Установить единый 2FA", "twofa_unified"),
            ("⬅️ Управление", "accounts_manage"),
        ),
    )


@router.callback_query(F.data == "twofa_all")
@router.callback_query(F.data == "twofa_unified")
async def cb_twofa_scope(callback: CallbackQuery, state: FSMContext):
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    accounts = await _accounts()
    if not accounts:
        await callback.answer("Нет аккаунтов", show_alert=True)
        return
    mode = "unified" if callback.data == "twofa_unified" else "all"
    await callback.answer()
    await safe_edit_message(
        callback.message,
        f"🔐 <b>{'Единый 2FA' if mode == 'unified' else 'Установить всем'}</b>\n\n"
        f"Будут проверены <b>{len(accounts)}</b> аккаунтов. "
        "На аккаунтах без 2FA новый пароль будет установлен. "
        + (
            "Для аккаунтов с 2FA потребуется старый пароль; где он не подойдёт, "
            "изменения не будет."
            if mode == "unified"
            else "Аккаунты с уже включённой 2FA будут пропущены."
        )
        + "\n\nПродолжить?",
        reply_markup=_keyboard(
            ("✅ Подтвердить", f"twofa_confirm_{mode}"),
            ("⬅️ Назад", "accounts_twofa"),
        ),
    )


@router.callback_query(F.data.startswith("twofa_list_p_"))
async def cb_twofa_list(callback: CallbackQuery):
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await callback.answer()
    accounts = await _accounts()
    pages = max(1, (len(accounts) + PAGE_SIZE - 1) // PAGE_SIZE)
    try:
        page = max(0, min(int(callback.data.rsplit("_", 1)[-1]), pages - 1))
    except (TypeError, ValueError):
        page = 0
    rows = []
    for account in accounts[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]:
        icon, _ = await _probe_account(account)
        rows.append(
            [InlineKeyboardButton(
                text=f"{icon} {_account_label(account)[:45]}",
                callback_data=f"twofa_pick_{account.id}",
            )]
        )
    nav = []
    if page:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"twofa_list_p_{page - 1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"twofa_list_p_{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="accounts_twofa")])
    await safe_edit_message(
        callback.message,
        f"🔐 <b>2FA по аккаунтам</b> · {page + 1}/{pages}\n\n"
        "✅ включена · ❌ нет · ⚠️ статус не проверен. "
        "Нажатие откроет ввод пароля; изменение начнётся после подтверждения пароля.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("twofa_pick_"))
@router.callback_query(F.data.startswith("account_set_2fa_"))
async def cb_twofa_pick(callback: CallbackQuery, state: FSMContext):
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    try:
        account_id = int(callback.data.rsplit("_", 1)[-1])
    except (TypeError, ValueError):
        await callback.answer("Неверный аккаунт", show_alert=True)
        return
    accounts = await _accounts()
    account = next((item for item in accounts if item.id == account_id), None)
    if account is None:
        await callback.answer("Аккаунт не найден", show_alert=True)
        return
    await state.clear()
    await state.update_data(twofa_mode="single", twofa_account_id=account_id)
    await state.set_state(Fleet2FAFSM.new_password)
    await callback.answer()
    await safe_edit_message(
        callback.message,
        f"🔐 <b>{html.escape(_account_label(account))}</b>\n\n"
        "Введите новый пароль 2FA (не менее 8 символов). Сообщение будет удалено. "
        "Если 2FA уже включена, далее потребуется текущий пароль.\n\n"
        "Отмена: /start",
        reply_markup=_keyboard(("⬅️ Отмена", "twofa_cancel")),
    )


@router.callback_query(F.data.startswith("twofa_confirm_"))
async def cb_twofa_confirm(callback: CallbackQuery, state: FSMContext):
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    mode = callback.data.removeprefix("twofa_confirm_")
    if mode not in {"all", "unified"}:
        await callback.answer("Неверное действие", show_alert=True)
        return
    await state.clear()
    await state.update_data(twofa_mode=mode)
    await state.set_state(Fleet2FAFSM.new_password)
    await callback.answer()
    await safe_edit_message(
        callback.message,
        "🔐 Введите <b>новый</b> пароль 2FA (не менее 8 символов). "
        "Сообщение будет удалено. Отмена: /start",
        reply_markup=_keyboard(("⬅️ Отмена", "twofa_cancel")),
    )


@router.callback_query(F.data == "twofa_cancel")
async def cb_twofa_cancel(callback: CallbackQuery, state: FSMContext):
    if not is_authorized_user(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    await callback.answer()
    await render_twofa_menu(callback.message)


@router.message(Fleet2FAFSM.new_password)
async def process_new_password(message: Message, state: FSMContext):
    if not await _require_deleted_password_message(message, state):
        return
    if not is_authorized_user(message.from_user.id):
        return
    password = message.text or ""
    if password.strip().lower() in _CANCEL:
        await state.clear()
        await message.answer("❌ Изменение 2FA отменено.")
        return
    if len(password) < 8 or len(password) > 128:
        await message.answer("Пароль должен содержать 8–128 символов. Введите новый пароль:")
        return
    await state.update_data(twofa_new_password=password)
    await state.set_state(Fleet2FAFSM.confirm_password)
    await message.answer("Введите новый пароль ещё раз. Сообщение будет удалено.")


@router.message(Fleet2FAFSM.confirm_password)
async def process_confirm_password(message: Message, state: FSMContext):
    if not await _require_deleted_password_message(message, state):
        return
    if not is_authorized_user(message.from_user.id):
        return
    confirmation = message.text or ""
    if confirmation.strip().lower() in _CANCEL:
        await state.clear()
        await message.answer("❌ Изменение 2FA отменено.")
        return
    data = await state.get_data()
    if confirmation != data.get("twofa_new_password"):
        await state.clear()
        await message.answer("❌ Пароли не совпали. Начните настройку 2FA заново.")
        return
    if data.get("twofa_mode") == "all":
        await _run_change(message, state, current_password=None)
        return
    await state.set_state(Fleet2FAFSM.current_password)
    await message.answer(
        "Введите <b>действующий</b> пароль 2FA для замены на аккаунтах, "
        "где он уже включён. Если такого пароля нет, отправьте «пропустить»: "
        "эти аккаунты останутся без изменений. Сообщение будет удалено.",
        parse_mode=ParseMode.HTML,
    )


@router.message(Fleet2FAFSM.current_password)
async def process_current_password(message: Message, state: FSMContext):
    if not await _require_deleted_password_message(message, state):
        return
    if not is_authorized_user(message.from_user.id):
        return
    current = message.text or ""
    if current.strip().lower() in _CANCEL:
        await state.clear()
        await message.answer("❌ Изменение 2FA отменено.")
        return
    if current.strip().lower() in _SKIP_CURRENT:
        current = None
    elif len(current) > 128:
        await message.answer("Текущий пароль слишком длинный. Введите до 128 символов:")
        return
    await _run_change(message, state, current_password=current)


async def _run_change(message: Message, state: FSMContext, *, current_password: str | None):
    progress = None
    try:
        data = await state.get_data()
        if data.get("twofa_mode") not in {"all", "unified", "single"}:
            raise ValueError("invalid 2FA mode")
        if not data.get("twofa_new_password"):
            raise ValueError("missing 2FA password")
        await state.set_state(Fleet2FAFSM.processing)
        progress = await message.answer("⏳ Проверяю аккаунты и меняю 2FA…")
        accounts = await _accounts()
        if data.get("twofa_mode") == "single":
            accounts = [
                account for account in accounts
                if account.id == data.get("twofa_account_id")
            ]
        if not accounts:
            await safe_edit_message(progress, "Аккаунты не найдены; 2FA не менялась.")
            return
        results = []
        for account in accounts:
            results.append(
                await _apply_account(account, data["twofa_new_password"], current_password)
            )
        counts = {key: sum(r.outcome == key for r in results) for key in ("updated", "skipped", "error")}
        lines = [
            "🔐 <b>Результат 2FA</b>",
            f"✅ Обновлено: {counts['updated']} · ⏭️ Пропущено: {counts['skipped']} · ❌ Ошибки: {counts['error']}",
            "",
        ]
        for result in results[:25]:
            icon = {"updated": "✅", "skipped": "⏭️", "error": "❌"}[result.outcome]
            lines.append(f"{icon} {html.escape(result.label)} — {html.escape(result.detail)}")
        if len(results) > 25:
            lines.append(f"… ещё {len(results) - 25} строк в TXT-отчёте")
        await safe_edit_message(progress, "\n".join(lines))
        if len(results) > 25:
            report = "\ufeff" + "\n".join(
                f"{result.label} | {result.outcome} | {result.detail}"
                for result in results
            ) + "\n"
            await message.answer_document(
                BufferedInputFile(report.encode("utf-8"), filename="corebot-2fa-report.txt")
            )
    except Exception as exc:
        log.error("2FA batch failed: {}", type(exc).__name__)
        if progress is not None:
            await safe_edit_message(
                progress,
                "❌ Не удалось завершить операцию. Проверьте фактический статус 2FA в списке аккаунтов.",
            )
    finally:
        # Secrets exist only in this short-lived FSM flow; clear even on Telegram failure.
        await state.clear()
