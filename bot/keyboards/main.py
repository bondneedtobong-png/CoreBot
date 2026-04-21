"""
Клавиатуры для главного меню и навигации.
"""
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from utils.neuro_sampling import NEURO_PARAM_BUTTONS

# Пагинация списков в Telegram
ACCOUNTS_LIST_PAGE_SIZE = 5
PROXY_LIST_PAGE_SIZE = 10
MAILING_LIST_PAGE_SIZE = 10
CLIENTS_LIST_PAGE_SIZE = 10


# ==================== Главное меню ====================

def get_main_keyboard() -> InlineKeyboardMarkup:
    """Главное меню бота."""
    keyboard = [
        [
            InlineKeyboardButton(text="👥 Аккаунты", callback_data="menu_accounts"),
            InlineKeyboardButton(text="🗄 База данных", callback_data="menu_database"),
        ],
        [
            InlineKeyboardButton(text="📬 Рассылка", callback_data="menu_mailing"),
            InlineKeyboardButton(text="🧠 Нейрочаттинг", callback_data="menu_neurochat"),
        ],
        [
            InlineKeyboardButton(text="🌐 Прокси", callback_data="menu_proxy"),
            InlineKeyboardButton(text="🔥 Прогрев", callback_data="menu_warmup"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_warmup_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton(text="👤 Выбрать аккаунты", callback_data="warmup_pick_accounts"),
            InlineKeyboardButton(text="📂 Выбрать группу", callback_data="warmup_pick_group"),
        ],
        [
            InlineKeyboardButton(text="⚙️ Настройки прогрева", callback_data="warmup_settings"),
        ],
        [
            InlineKeyboardButton(text="📊 Статус прогрева", callback_data="warmup_status_summary"),
        ],
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_back"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_warmup_status_keyboard() -> InlineKeyboardMarkup:
    """Сводка по прогреву: обновить, назад в меню прогрева."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Обновить", callback_data="warmup_status_summary"),
            ],
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_warmup"),
            ],
        ]
    )


def get_warmup_accounts_pick_keyboard(accounts: list) -> InlineKeyboardMarkup:
    rows = []
    for a in accounts:
        label = f"🔥 {'ON' if getattr(a, 'warmup_enabled', False) else 'OFF'} · {(a.username or a.phone)}"
        rows.append([InlineKeyboardButton(text=label[:64], callback_data=f"warmup_toggle_acc_{a.id}")])
        profile = str(getattr(a, "warmup_profile", "safe") or "safe")
        rows.append([InlineKeyboardButton(text=f"🧠 Профиль: {profile}", callback_data=f"warmup_acc_profile_{a.id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_warmup")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_warmup_account_profile_keyboard(account_id: int, profiles: list, selected: str) -> InlineKeyboardMarkup:
    rows = []
    selected_norm = (selected or "safe").strip().lower()
    for p in profiles:
        name = str(getattr(p, "name", "") or "").strip()
        if not name:
            continue
        marker = "✅ " if name.lower() == selected_norm else ""
        rows.append([
            InlineKeyboardButton(
                text=f"{marker}{name}",
                callback_data=f"warmup_set_acc_profile_{account_id}_{name}",
            )
        ])
    rows.append([InlineKeyboardButton(text="⬅️ К аккаунтам", callback_data="warmup_pick_accounts")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_warmup_groups_pick_keyboard(groups: list) -> InlineKeyboardMarkup:
    rows = []
    for g in groups:
        rows.append([InlineKeyboardButton(text=f"📂 {g.name}", callback_data=f"warmup_group_menu_{g.id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_warmup")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_warmup_group_action_keyboard(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Включить прогрев группе", callback_data=f"warmup_group_on_{group_id}")],
            [InlineKeyboardButton(text="⏸ Выключить прогрев группе", callback_data=f"warmup_group_off_{group_id}")],
            [InlineKeyboardButton(text="🧠 Профиль для группы", callback_data=f"warmup_group_profile_{group_id}")],
            [InlineKeyboardButton(text="⬅️ К группам", callback_data="warmup_pick_group")],
        ]
    )


def get_warmup_group_profile_keyboard(group_id: int, profiles: list, selected: str) -> InlineKeyboardMarkup:
    rows = []
    selected_norm = (selected or "safe").strip().lower()
    for p in profiles:
        name = str(getattr(p, "name", "") or "").strip()
        if not name:
            continue
        marker = "✅ " if name.lower() == selected_norm else ""
        rows.append([
            InlineKeyboardButton(
                text=f"{marker}{name}",
                callback_data=f"warmup_group_set_profile_{group_id}_{name}",
            )
        ])
    rows.append([InlineKeyboardButton(text="⬅️ К группе", callback_data=f"warmup_group_menu_{group_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_warmup_settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏱ Задержка и jitter", callback_data="warmup_settings_delay")],
            [InlineKeyboardButton(text="📈 Дневной лимит", callback_data="warmup_settings_limit")],
            [InlineKeyboardButton(text="💬 Сообщества для активности", callback_data="warmup_settings_chats")],
            [InlineKeyboardButton(text="🧩 Копировать профиль по шаблону", callback_data="warmup_copy_profile_start")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_warmup")],
        ]
    )


def get_warmup_copy_source_keyboard(profiles: list) -> InlineKeyboardMarkup:
    rows = []
    visible_idx = 0
    for p in profiles:
        name = str(getattr(p, "name", "") or "").strip()
        if not name:
            continue
        rows.append([InlineKeyboardButton(text=name, callback_data=f"warmup_copy_source_idx_{visible_idx}")])
        visible_idx += 1
    rows.append([InlineKeyboardButton(text="⬅️ К настройкам", callback_data="warmup_settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_warmup_copy_mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Создать новый профиль", callback_data="warmup_copy_mode_new")],
            [InlineKeyboardButton(text="♻️ Обновить существующий", callback_data="warmup_copy_mode_overwrite")],
            [InlineKeyboardButton(text="⬅️ К шаблонам", callback_data="warmup_copy_profile_start")],
        ]
    )


def get_warmup_copy_target_keyboard(profiles: list) -> InlineKeyboardMarkup:
    rows = []
    visible_idx = 0
    for p in profiles:
        name = str(getattr(p, "name", "") or "").strip()
        if not name or name.lower() == "safe":
            continue
        rows.append([InlineKeyboardButton(text=name, callback_data=f"warmup_copy_target_idx_{visible_idx}")])
        visible_idx += 1
    rows.append([InlineKeyboardButton(text="⬅️ К режиму", callback_data="warmup_copy_mode_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_warmup_copy_confirm_keyboard(mode: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"warmup_copy_confirm_{mode}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="warmup_copy_confirm_back")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="warmup_settings")],
        ]
    )


# ==================== Навигация «Назад» ====================

def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура с кнопкой «Назад в главное меню»."""
    keyboard = [
        [InlineKeyboardButton(text="⬅️ В главное меню", callback_data="menu_back")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_context_back_keyboard(callback: str, text: str = "⬅️ Назад") -> InlineKeyboardMarkup:
    """
    Клавиатура с кнопкой «Назад» на произвольный callback.

    Args:
        callback: Callback-data для кнопки назад
        text: Текст кнопки (по умолчанию «⬅️ Назад»)
    """
    keyboard = [
        [InlineKeyboardButton(text=text, callback_data=callback)],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_cancel_keyboard(cancel_callback: str = "cancel_accounts") -> InlineKeyboardMarkup:
    """
    Кнопка «Отмена». callback_data должен совпадать с хендлером раздела,
    иначе сработает чужой роутер (раньше все использовали «cancel»).
    """
    keyboard = [
        [InlineKeyboardButton(text="❌ Отмена", callback_data=cancel_callback)],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_cancel_with_back_keyboard(
    cancel_callback: str = "cancel_accounts",
    back_callback: str = "menu_back",
    *,
    back_text: str = "⬅️ Назад",
) -> InlineKeyboardMarkup:
    """Отмена + возврат на экран выше без сообщения «отменено» (хендлер back должен сделать state.clear)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=back_text, callback_data=back_callback)],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=cancel_callback)],
        ]
    )


# ==================== Аккаунты ====================

def get_accounts_keyboard() -> InlineKeyboardMarkup:
    """Меню управления аккаунтами."""
    keyboard = [
        [
            InlineKeyboardButton(text="📥 Загрузить Tdata (ZIP)", callback_data="accounts_upload"),
        ],
        [
            InlineKeyboardButton(
                text="📦 Массовый залив Tdata (ГЕО)",
                callback_data="accounts_upload_bulk_geo",
            ),
        ],
        [
            InlineKeyboardButton(text="📋 Список аккаунтов", callback_data="accounts_list"),
        ],
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_back"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_accounts_list_keyboard(accounts: list, *, page: int = 0) -> InlineKeyboardMarkup:
    """
    Клавиатура со списком аккаунтов (пагинация).

    Args:
        accounts: Полный список объектов Account
        page: Номер страницы (с нуля)
    """
    keyboard = []
    page_size = ACCOUNTS_LIST_PAGE_SIZE
    total = len(accounts)
    total_pages = max(1, (total + page_size - 1) // page_size) if total else 1
    page = max(0, min(int(page), total_pages - 1))
    chunk = accounts[page * page_size : (page + 1) * page_size]

    if not accounts:
        keyboard.append([
            InlineKeyboardButton(text="📭 Нет аккаунтов", callback_data="accounts_empty")
        ])
    else:
        for account in chunk:
            status_emoji = {
                "active": "🟢",
                "inactive": "🟡",
                "banned": "🔴",
                "flood_wait": "🟠",
                "error": "⚫",
                "spam_blocked": "🚫",
            }.get(account.status.value, "⚪")

            title = account.list_row_caption
            if len(title) > 36:
                title = title[:33] + "..."
            keyboard.append([
                InlineKeyboardButton(
                    text=f"{status_emoji} {title}",
                    callback_data=f"account_view_{account.id}"
                )
            ])

    if total_pages > 1:
        nav_row = []
        if page > 0:
            nav_row.append(
                InlineKeyboardButton(
                    text="◀️ Пред.",
                    callback_data=f"accounts_list_p_{page - 1}",
                )
            )
        nav_row.append(
            InlineKeyboardButton(
                text=f"{page + 1}/{total_pages}",
                callback_data="accounts_list_page_info",
            )
        )
        if page < total_pages - 1:
            nav_row.append(
                InlineKeyboardButton(
                    text="След. ▶️",
                    callback_data=f"accounts_list_p_{page + 1}",
                )
            )
        keyboard.append(nav_row)

    keyboard.append([
        InlineKeyboardButton(text="📁 Группы", callback_data="accounts_groups"),
    ])
    keyboard.append([
        InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_accounts"),
    ])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_account_groups_menu_keyboard(groups: list) -> InlineKeyboardMarkup:
    """Список групп аккаунтов + создать + назад к списку аккаунтов."""
    rows = []
    for g in groups:
        rows.append([
            InlineKeyboardButton(text=f"📂 {g.name}", callback_data=f"group_view_{g.id}"),
        ])
    rows.append([
        InlineKeyboardButton(text="➕ Создать группу", callback_data="groups_create_start"),
    ])
    rows.append([
        InlineKeyboardButton(text="⬅️ Назад", callback_data="accounts_list"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_group_detail_keyboard(group_id: int) -> InlineKeyboardMarkup:
    """Карточка группы: проверки, состав, удаление, назад."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Проверить прокси",
                    callback_data=f"group_check_proxy_{group_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔍 Проверка на спам-блок",
                    callback_data=f"group_check_spam_{group_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="👥 Состав группы",
                    callback_data=f"group_members_{group_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔐 Установить 2FA",
                    callback_data=f"group_bulk_2fa_start_{group_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🧩 Массовое редактирование профиля",
                    callback_data=f"group_bulk_profile_start_{group_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🗑 Удалить группу",
                    callback_data=f"group_delete_confirm_{group_id}",
                ),
            ],
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data="accounts_groups"),
            ],
        ]
    )


def get_group_members_keyboard(group_id: int, accounts_in_group: list) -> InlineKeyboardMarkup:
    """Состав: убрать аккаунт, добавить, назад к группе."""
    rows = []
    for acc in accounts_in_group:
        label = acc.list_row_caption
        if len(label) > 35:
            label = label[:32] + "..."
        rows.append([
            InlineKeyboardButton(
                text=f"➖ {label}",
                callback_data=f"group_rm_{group_id}_{acc.id}",
            ),
        ])
    rows.append([
        InlineKeyboardButton(
            text="➕ Добавить аккаунт",
            callback_data=f"group_add_menu_{group_id}",
        ),
    ])
    rows.append([
        InlineKeyboardButton(text="⬅️ К группе", callback_data=f"group_view_{group_id}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_group_add_pick_keyboard(group_id: int, accounts: list) -> InlineKeyboardMarkup:
    """Выбор аккаунта для добавления в группу (как в списке: название[Имя Фамилия])."""
    rows = []
    for acc in accounts:
        label = acc.list_row_caption
        if len(label) > 36:
            label = label[:33] + "..."
        rows.append([
            InlineKeyboardButton(
                text=f"➕ {label}",
                callback_data=f"group_pick_{group_id}_{acc.id}",
            ),
        ])
    rows.append([
        InlineKeyboardButton(text="⬅️ Назад", callback_data=f"group_members_{group_id}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_account_card_keyboard(account: object, is_authorized: bool = None) -> InlineKeyboardMarkup:
    """
    Клавиатура карточки аккаунта.

    Args:
        account: Объект Account со всеми полями
        is_authorized: Статус авторизации (None = неизвестно)
    """
    # Прокси для кнопки
    if account.proxy:
        proxy_btn_text = f"🌐 Прокси: {account.proxy.name} → Сменить"
    else:
        proxy_btn_text = "🌐 Прокси: не назначен → Назначить"
    warmup_on = bool(getattr(account, "warmup_enabled", False))
    warmup_btn_text = f"🔥 Прогрев: {'ВКЛ' if warmup_on else 'ВЫКЛ'}"

    keyboard = [
        [
            InlineKeyboardButton(text="🔄 Перепроверить авторизацию", callback_data=f"account_recheck_auth_{account.id}"),
        ],
        [
            InlineKeyboardButton(text=proxy_btn_text, callback_data=f"account_change_proxy_{account.id}"),
        ],
        [
            InlineKeyboardButton(text=warmup_btn_text, callback_data=f"account_warmup_toggle_{account.id}"),
        ],
        [
            InlineKeyboardButton(
                text="✏️ Имя / bio / username",
                callback_data=f"account_edit_profile_{account.id}",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🏷 Название в списке",
                callback_data=f"account_list_label_{account.id}",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🖼 Управление аватарками",
                callback_data=f"account_manage_photos_{account.id}",
            ),
        ],
        [
            InlineKeyboardButton(text="🔐 Установить 2FA", callback_data=f"account_set_2fa_{account.id}"),
        ],
        [
            InlineKeyboardButton(text="🗑 Удалить аккаунт", callback_data=f"account_delete_confirm_{account.id}"),
        ],
        [
            InlineKeyboardButton(text="⬅️ Назад к списку", callback_data="accounts_list"),
        ],
    ]

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_edit_profile_keyboard(account_id: int) -> InlineKeyboardMarkup:
    """Клавиатура: имя, username, bio (аватарки — отдельный раздел «Управление аватарками»)."""
    keyboard = [
        [
            InlineKeyboardButton(text="✏️ Имя и фамилия", callback_data=f"account_edit_name_{account_id}"),
        ],
        [
            InlineKeyboardButton(text="📛 Username", callback_data=f"account_edit_username_{account_id}"),
        ],
        [
            InlineKeyboardButton(text="📝 Bio / Описание", callback_data=f"account_edit_bio_{account_id}"),
        ],
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data=f"account_view_{account_id}"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_photo_management_keyboard(account_id: int) -> InlineKeyboardMarkup:
    """
    Экран управления фото профиля аккаунта (список + действия).

    Callback-и:
      account_photo_add_{id} — режим ожидания нового фото;
      account_photo_del_prompt_{id} — режим ввода номера для удаления;
      account_view_{id} — возврат в карточку аккаунта.
    """
    keyboard = [
        [
            InlineKeyboardButton(
                text="➕ Добавить новую аватарку",
                callback_data=f"account_photo_add_{account_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🗑 Удалить фото по номеру",
                callback_data=f"account_photo_del_prompt_{account_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                text="⬅️ Назад в карточку",
                callback_data=f"account_view_{account_id}",
            ),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_confirm_photo_delete_keyboard(account_id: int) -> InlineKeyboardMarkup:
    """
    Клавиатура на этапе ввода номера фото или ожидания файла:
    отмена возвращает к экрану списка фото без смены карточки аккаунта.
    """
    keyboard = [
        [
            InlineKeyboardButton(
                text="❌ Отмена",
                callback_data=f"account_manage_photos_{account_id}",
            ),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_confirm_delete_keyboard(account_id: int) -> InlineKeyboardMarkup:
    """Клавиатура подтверждения удаления аккаунта."""
    keyboard = [
        [
            InlineKeyboardButton(text="⚠️ Да, удалить", callback_data=f"account_delete_{account_id}"),
        ],
        [
            InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"account_view_{account_id}"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


# ==================== Клиенты ====================

def get_clients_keyboard() -> InlineKeyboardMarkup:
    """Меню управления клиентами."""
    keyboard = [
        [
            InlineKeyboardButton(text="📥 Загрузить базу (TXT)", callback_data="clients_upload"),
        ],
        [
            InlineKeyboardButton(
                text="🧾 Обработать список t.me → @",
                callback_data="clients_username_tool",
            ),
        ],
        [
            InlineKeyboardButton(text="📋 Список клиентов", callback_data="clients_list"),
            InlineKeyboardButton(text="🗑 Очистить базу", callback_data="clients_clear"),
        ],
        [
            InlineKeyboardButton(text="♻️ Повторный прогон (CONTACTED→NEW)", callback_data="clients_reset_contacted"),
        ],
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_database"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_clients_list_keyboard(clients: list, *, page: int = 0) -> InlineKeyboardMarkup:
    """Клавиатура списка клиентов с пагинацией."""
    keyboard = []
    total = len(clients)
    page_size = CLIENTS_LIST_PAGE_SIZE
    total_pages = max(1, (total + page_size - 1) // page_size) if total else 1
    page = max(0, min(int(page), total_pages - 1))
    chunk = clients[page * page_size : (page + 1) * page_size]

    if not chunk:
        keyboard.append([InlineKeyboardButton(text="📭 Нет клиентов", callback_data="clients_empty")])
    else:
        for c in chunk:
            status_emoji = {
                "new": "🟢",
                "contacted": "✅",
                "invalid": "❌",
                "blocked": "🚫",
            }.get(c.status.value, "⚪")
            uname = f"@{c.username}" if c.username else f"id:{c.id}"
            keyboard.append([InlineKeyboardButton(text=f"{status_emoji} {uname}"[:64], callback_data="clients_page_info")])

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀️ Пред.", callback_data=f"clients_list_p_{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="clients_page_info"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(text="След. ▶️", callback_data=f"clients_list_p_{page + 1}"))
        keyboard.append(nav)

    keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_database")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


# ==================== Рассылки ====================

def get_mailing_keyboard() -> InlineKeyboardMarkup:
    """Меню рассылок."""
    keyboard = [
        [
            InlineKeyboardButton(text="➕ Создать рассылку", callback_data="mailing_create"),
        ],
        [
            InlineKeyboardButton(text="📋 Мои рассылки", callback_data="mailing_list"),
        ],
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_back"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_mailing_list_keyboard(mailings: list, *, page: int = 0) -> InlineKeyboardMarkup:
    """Клавиатура со списком рассылок."""
    keyboard = []
    total = len(mailings)
    page_size = MAILING_LIST_PAGE_SIZE
    total_pages = max(1, (total + page_size - 1) // page_size) if total else 1
    page = max(0, min(int(page), total_pages - 1))
    chunk = mailings[page * page_size : (page + 1) * page_size]

    if not chunk:
        keyboard.append([
            InlineKeyboardButton(text="📭 Нет рассылок", callback_data="mailing_empty")
        ])
    else:
        for mailing in chunk:
            status_emoji = {
                "draft": "📝",
                "pending": "⏳",
                "running": "🚀",
                "paused": "⏸",
                "completed": "✅",
                "cancelled": "❌",
                "error": "⚠️",
            }.get(mailing.status.value, "⚪")

            keyboard.append([
                InlineKeyboardButton(
                    text=f"{status_emoji} {mailing.name or f'Рассылка #{mailing.id}'}",
                    callback_data=f"mailing_view_{mailing.id}"
                )
            ])

    if total_pages > 1:
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton(text="◀️ Пред.", callback_data=f"mailing_list_p_{page - 1}"))
        nav_row.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="mailing_list_page_info"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton(text="След. ▶️", callback_data=f"mailing_list_p_{page + 1}"))
        keyboard.append(nav_row)

    keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_mailing")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def mailing_target_group_button_label(mailing) -> str:
    """Подпись кнопки «группа аккаунтов» (название из БД, не только id)."""
    tg = getattr(mailing, "target_group_id", None)
    if not tg:
        return "👥 Группа: все аккаунты"
    rel = getattr(mailing, "target_group", None)
    name = (getattr(rel, "name", None) or "").strip()
    if name:
        return f"👥 Группа: {name}"
    return f"👥 Группа: id {tg}"


def get_mailing_view_keyboard(
    mailing,
    *,
    show_stop: bool = False,
    show_neuro_stop: bool = False,
) -> InlineKeyboardMarkup:
    """Клавиатура просмотра рассылки: Запуск/стоп рассылки, при необходимости — выкл. нейрочат."""
    mid = mailing.id
    launch_row = []
    if show_stop:
        launch_row.append(
            InlineKeyboardButton(text="⏹ Остановить", callback_data=f"mailing_stop_{mid}")
        )
    else:
        launch_row.append(
            InlineKeyboardButton(text="🚀 Запустить", callback_data=f"mailing_start_{mid}")
        )
    launch_row.append(
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"mailing_refresh_{mid}")
    )
    keyboard = [
        launch_row,
    ]
    if show_neuro_stop:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text="⏹ Выключить нейрочат",
                    callback_data=f"mailing_neuro_stop_view_{mid}",
                ),
            ]
        )
    keyboard.extend(
        [
            [
                InlineKeyboardButton(
                    text=mailing_target_group_button_label(mailing),
                    callback_data=f"mailing_pick_group_{mid}",
                ),
            ],
            [
                InlineKeyboardButton(text="⚙️ Настройки", callback_data=f"mailing_settings_{mid}"),
            ],
            [
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"mailing_delete_confirm_{mid}"),
            ],
            [
                InlineKeyboardButton(text="🧠 К нейрочаттингу", callback_data=f"neurochat_open_{mid}"),
            ],
            [
                InlineKeyboardButton(text="⬅️ Назад к списку", callback_data="mailing_list"),
            ],
        ],
    )
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_mailing_modules_keyboard(mailing: object) -> InlineKeyboardMarkup:
    """Хаб настроек рассылки: модули."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛡 Настройка безопасности",
                    callback_data=f"mailing_mod_security_{mailing.id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🎯 Аудитория рассылки",
                    callback_data=f"mailing_campaign_aud_{mailing.id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="✉️ Первое сообщение",
                    callback_data=f"mailing_mod_first_{mailing.id}",
                ),
            ],
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data=f"mailing_view_{mailing.id}"),
            ],
        ]
    )


def get_mailing_audience_keyboard(mailing_id: int, status_lbl: str) -> InlineKeyboardMarkup:
    """Настройка аудитории рассылки по классам и статусу."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Статус очереди: {status_lbl}",
                    callback_data=f"mailing_aud_toggle_{mailing_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Include классы",
                    callback_data=f"mailing_aud_inc_{mailing_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Exclude классы",
                    callback_data=f"mailing_aud_exc_{mailing_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="♻️ Сброс к умолчанию",
                    callback_data=f"mailing_aud_reset_{mailing_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=f"mailing_campaign_aud_{mailing_id}",
                ),
            ],
        ]
    )


def get_mailing_campaign_keyboard(mailing: object) -> InlineKeyboardMarkup:
    """Режим аудитории, тестовый список, лимит и пауза аккаунта после пакета."""
    mid = mailing.id
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🧪 Тест (txt)",
                    callback_data=f"mailing_aud_mode_test_{mid}",
                ),
                InlineKeyboardButton(
                    text="📗 Из базы NEW",
                    callback_data=f"mailing_aud_mode_new_{mid}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🎛 По классам",
                    callback_data=f"mailing_aud_mode_classes_{mid}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🎛 Фильтр классов (include/exclude)",
                    callback_data=f"mailing_aud_menu_{mid}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📎 Загрузить тестовый txt",
                    callback_data=f"mailing_test_txt_{mid}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔢 Лимит успешных",
                    callback_data=f"mailing_cap_edit_{mid}",
                ),
                InlineKeyboardButton(
                    text="⏳ Пауза аккаунта (ч)",
                    callback_data=f"mailing_cd_edit_{mid}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=f"mailing_settings_{mid}",
                ),
            ],
        ]
    )


def get_mailing_settings_keyboard(mailing: object) -> InlineKeyboardMarkup:
    """Совместимость: то же, что хаб модулей."""
    return get_mailing_modules_keyboard(mailing)


def get_mailing_security_keyboard(mailing: object) -> InlineKeyboardMarkup:
    """Задержки, typing, умная задержка, пакеты (аудитория — в карточке рассылки)."""
    typing_status = "✅" if mailing.use_typing else "❌"
    smart_status = "✅" if mailing.smart_delay else "❌"
    ah = getattr(mailing, "auto_stop_hours", None)
    if ah and float(ah) > 0:
        runtime_lbl = f"🕒 Автостоп: {float(ah):g} ч"
    else:
        runtime_lbl = "🕒 Автостоп: выкл (до ручной остановки)"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{typing_status} Имитация набора текста",
                    callback_data=f"mailing_toggle_typing_{mailing.id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"{smart_status} Умная задержка",
                    callback_data=f"mailing_toggle_smart_{mailing.id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"⏱ Задержка: {mailing.delay_between_messages} сек",
                    callback_data=f"mailing_edit_delay_{mailing.id}",
                ),
                InlineKeyboardButton(
                    text=f"📨 На аккаунт: {mailing.messages_per_batch}",
                    callback_data=f"mailing_edit_messages_per_batch_{mailing.id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"🔄 Задержка между аккаунтами+: {mailing.batch_delay} сек",
                    callback_data=f"mailing_edit_batch_delay_{mailing.id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=runtime_lbl,
                    callback_data=f"mailing_edit_runtime_{mailing.id}",
                ),
            ],
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data=f"mailing_settings_{mailing.id}"),
            ],
        ]
    )


def get_openrouter_key_keyboard(mailing_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✏️ Задать ключ",
                    callback_data=f"openrouter_key_set_neuro_{mailing_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🗑 Удалить ключ из бота",
                    callback_data=f"openrouter_key_clear_confirm_neuro_{mailing_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=f"neurochat_open_{mailing_id}",
                ),
            ],
        ]
    )


def get_openrouter_key_clear_keyboard(mailing_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Да, удалить",
                    callback_data=f"openrouter_key_clear_do_neuro_{mailing_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Отмена",
                    callback_data=f"openrouter_key_menu_neuro_{mailing_id}",
                ),
            ],
        ]
    )


def get_mailing_neuro_sampling_keyboard(mailing_id: int) -> InlineKeyboardMarkup:
    """Кнопка на каждый параметр сэмплирования + сброс и назад."""
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(NEURO_PARAM_BUTTONS), 2):
        chunk = NEURO_PARAM_BUTTONS[i : i + 2]
        row = [
            InlineKeyboardButton(
                text=label[:64],
                callback_data=f"mailing_nsp_{mailing_id}_{code}",
            )
            for code, _key, label in chunk
        ]
        rows.append(row)
    rows.append(
        [
            InlineKeyboardButton(
                text="♻️ Сбросить к дефолтам",
                callback_data=f"mailing_neuro_sampling_reset_{mailing_id}",
            ),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=f"neurochat_open_{mailing_id}",
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_mailing_neuro_keyboard(mailing) -> InlineKeyboardMarkup:
    """mailing — объект с полями id, neurochat_enabled."""
    mid = mailing.id
    on = getattr(mailing, "neurochat_enabled", False)
    toggle_lbl = "✅ Нейрочат: ВКЛ" if on else "⬜ Нейрочат: ВЫКЛ"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=toggle_lbl,
                    callback_data=f"mailing_neuro_toggle_{mid}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔑 Ключ OpenRouter",
                    callback_data=f"openrouter_key_menu_neuro_{mid}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🧠 Модель OpenRouter",
                    callback_data=f"mailing_neuro_model_{mid}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🎛 Параметры сэмплирования",
                    callback_data=f"mailing_neuro_sampling_{mid}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📄 Загрузить system.txt",
                    callback_data=f"mailing_neuro_prompt_{mid}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔗 Ссылка",
                    callback_data=f"mailing_neuro_link_{mid}",
                ),
            ],
            [
                InlineKeyboardButton(text="⬅️ К рассылке", callback_data=f"mailing_view_{mid}"),
            ],
            [
                InlineKeyboardButton(text="⬅️ К списку нейрочата", callback_data="menu_neurochat"),
            ],
        ]
    )


def get_mailing_first_message_keyboard(
    mailing_id: int,
    extra_variant_count: int,
    *,
    variant_mode: str = "random",
    tz_short: str = "",
) -> InlineKeyboardMarkup:
    """Первое сообщение: основной текст, доп. варианты, удаление по индексу."""
    mode = "🎲 Случайно" if (variant_mode or "random") == "random" else "🔁 По очереди"
    tz_btn = f"🕐 {tz_short}" if tz_short else "🕐 Часовой пояс плейсхолдеров"
    rows = [
        [
            InlineKeyboardButton(
                text=tz_btn[:64],
                callback_data=f"mailing_tz_menu_{mailing_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                text="✏️ Основной текст",
                callback_data=f"mailing_edit_text_{mailing_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                text="➕ Добавить вариант",
                callback_data=f"mailing_variant_add_{mailing_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                text=f"🧩 Перебор вариантов: {mode}",
                callback_data=f"mailing_variant_mode_toggle_{mailing_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                text=f"📚 Список вариантов ({extra_variant_count})",
                callback_data=f"mailing_variants_{mailing_id}",
            ),
        ],
    ]
    rows.append(
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data=f"mailing_settings_{mailing_id}"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_mailing_tz_keyboard(mailing_id: int) -> InlineKeyboardMarkup:
    """Сдвиг UTC для плейсхолдеров первого сообщения (−12…+14 ч)."""
    mid = mailing_id
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="−3 ч", callback_data=f"mailing_tz_adj_{mid}_-3"),
                InlineKeyboardButton(text="−1 ч", callback_data=f"mailing_tz_adj_{mid}_-1"),
                InlineKeyboardButton(text="+1 ч", callback_data=f"mailing_tz_adj_{mid}_1"),
                InlineKeyboardButton(text="+3 ч", callback_data=f"mailing_tz_adj_{mid}_3"),
            ],
            [
                InlineKeyboardButton(
                    text="♻️ Как в .env (сбросить бот)",
                    callback_data=f"mailing_tz_reset_{mid}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ К первому сообщению",
                    callback_data=f"mailing_mod_first_{mid}",
                ),
            ],
        ]
    )


def get_mailing_target_group_keyboard(
    mailing_id: int,
    groups: list,
    *,
    back_callback: str | None = None,
) -> InlineKeyboardMarkup:
    """Выбор группы аккаунтов для рассылки; 0 = все. Назад — в карточку рассылки."""
    back = back_callback if back_callback is not None else f"mailing_view_{mailing_id}"
    rows = [
        [
            InlineKeyboardButton(
                text="🌐 Все аккаунты",
                callback_data=f"mailing_target_set_{mailing_id}_0",
            ),
        ],
    ]
    for g in groups:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"📂 {g.name}",
                    callback_data=f"mailing_target_set_{mailing_id}_{g.id}",
                ),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=back,
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ==================== Прокси ====================

def get_proxy_keyboard() -> InlineKeyboardMarkup:
    """Меню управления прокси."""
    keyboard = [
        [
            InlineKeyboardButton(text="➕ Добавить прокси", callback_data="proxy_add"),
            InlineKeyboardButton(text="📥 Массово в группу", callback_data="proxy_bulk_add"),
        ],
        [
            InlineKeyboardButton(text="📋 Список прокси", callback_data="proxy_list"),
            InlineKeyboardButton(text="📂 Группы прокси", callback_data="proxy_groups"),
        ],
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_back"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_proxy_groups_keyboard(groups_usage: list[tuple[object, int, int]]) -> InlineKeyboardMarkup:
    """Список групп прокси с метрикой used/total/free."""
    rows = []
    if not groups_usage:
        rows.append([InlineKeyboardButton(text="📭 Нет групп", callback_data="proxy_group_empty")])
    else:
        for group, used, total in groups_usage:
            free = max(0, int(total) - int(used))
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"📂 {group.name} ({used}/{total}, free {free})",
                        callback_data=f"proxy_group_view_{group.id}",
                    )
                ]
            )
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_proxy")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_proxy_group_card_keyboard(group_id: int) -> InlineKeyboardMarkup:
    """Карточка группы прокси."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"proxy_group_view_{group_id}")],
            [
                InlineKeyboardButton(
                    text="🗑 Удалить группу",
                    callback_data=f"proxy_group_delete_{group_id}",
                )
            ],
            [InlineKeyboardButton(text="⬅️ К группам", callback_data="proxy_groups")],
        ]
    )


def get_proxy_group_delete_confirm_keyboard(group_id: int) -> InlineKeyboardMarkup:
    """Подтверждение удаления пустой группы (без занятых прокси)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⚠️ Да, удалить группу",
                    callback_data=f"proxy_group_delete_do_{group_id}",
                )
            ],
            [InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"proxy_group_view_{group_id}")],
        ]
    )


def get_proxy_group_select_keyboard(
    groups_usage: list[tuple[object, int, int]],
    *,
    none_callback: str = "proxy_group_select_none",
    cancel_callback: str = "accounts_upload",
    back_callback: str | None = "menu_accounts",
) -> InlineKeyboardMarkup:
    """Выбор группы прокси с меткой used/total."""
    rows = []
    if not groups_usage:
        rows.append([InlineKeyboardButton(text="📭 Нет групп прокси", callback_data="proxy_group_empty")])
    else:
        for group, used, total in groups_usage:
            free = max(0, int(total) - int(used))
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"🌐 {group.name} ({used}/{total}, free {free})",
                        callback_data=f"proxy_group_select_{group.id}",
                    )
                ]
            )
    rows.append([InlineKeyboardButton(text="❌ Без прокси", callback_data=none_callback)])
    if back_callback:
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=back_callback)])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data=cancel_callback)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_proxy_list_keyboard(proxies: list, *, page: int = 0) -> InlineKeyboardMarkup:
    """
    Клавиатура со списком прокси (пагинация).

    Args:
        proxies: Полный список объектов Proxy
        page: Номер страницы (с нуля)
    """
    keyboard = []
    page_size = PROXY_LIST_PAGE_SIZE
    total = len(proxies)
    total_pages = max(1, (total + page_size - 1) // page_size) if total else 1
    page = max(0, min(int(page), total_pages - 1))
    chunk = proxies[page * page_size : (page + 1) * page_size]

    if not proxies:
        keyboard.append([
            InlineKeyboardButton(text="📭 Нет прокси", callback_data="proxy_empty")
        ])
    else:
        for proxy in chunk:
            status_emoji = "🟢" if proxy.is_working else "🔴"
            keyboard.append([
                InlineKeyboardButton(
                    text=f"{status_emoji} {proxy.name}",
                    callback_data=f"proxy_view_{proxy.id}"
                )
            ])

    if total_pages > 1:
        nav_row = []
        if page > 0:
            nav_row.append(
                InlineKeyboardButton(
                    text="◀️ Пред.",
                    callback_data=f"proxy_list_p_{page - 1}",
                )
            )
        nav_row.append(
            InlineKeyboardButton(
                text=f"{page + 1}/{total_pages}",
                callback_data="proxy_list_page_info",
            )
        )
        if page < total_pages - 1:
            nav_row.append(
                InlineKeyboardButton(
                    text="След. ▶️",
                    callback_data=f"proxy_list_p_{page + 1}",
                )
            )
        keyboard.append(nav_row)

    keyboard.append([
        InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_proxy"),
    ])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_proxy_card_keyboard(proxy: object) -> InlineKeyboardMarkup:
    """
    Клавиатура карточки прокси.

    Args:
        proxy: Объект Proxy со всеми полями
    """
    keyboard = [
        [
            InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"proxy_edit_{proxy.id}"),
        ],
        [
            InlineKeyboardButton(text="🔄 Проверить", callback_data=f"proxy_check_{proxy.id}"),
        ],
        [
            InlineKeyboardButton(text="🗑 Удалить прокси", callback_data=f"proxy_delete_confirm_{proxy.id}"),
        ],
        [
            InlineKeyboardButton(text="⬅️ Назад к списку", callback_data="proxy_list"),
        ],
    ]

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_proxy_select_keyboard(
    proxies: list,
    cancel_callback: str = "cancel_accounts",
    *,
    back_callback: str | None = "menu_accounts",
) -> InlineKeyboardMarkup:
    """
    Клавиатура для выбора прокси (при загрузке Tdata).

    Args:
        proxies: Список объектов Proxy
        cancel_callback: Callback для кнопки отмены
        back_callback: Callback для «Назад» (None — без кнопки)
    """
    keyboard = []

    if not proxies:
        keyboard.append([
            InlineKeyboardButton(text="📭 Нет прокси", callback_data="proxy_empty")
        ])
    else:
        for proxy in proxies:
            status_emoji = "🟢" if proxy.is_working else "🔴"
            keyboard.append([
                InlineKeyboardButton(
                    text=f"{status_emoji} {proxy.name}",
                    callback_data=f"proxy_select_{proxy.id}"
                )
            ])

    keyboard.append([
        InlineKeyboardButton(text="❌ Без прокси", callback_data="proxy_select_none"),
    ])
    if back_callback:
        keyboard.append([
            InlineKeyboardButton(text="⬅️ Назад", callback_data=back_callback),
        ])
    keyboard.append([
        InlineKeyboardButton(text="❌ Отмена", callback_data=cancel_callback),
    ])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_edit_proxy_keyboard(proxy_id: int) -> InlineKeyboardMarkup:
    """Клавиатура редактирования прокси."""
    keyboard = [
        [
            InlineKeyboardButton(text="✏️ Изменить название", callback_data=f"proxy_edit_name_{proxy_id}"),
        ],
        [
            InlineKeyboardButton(text="🌐 Изменить данные", callback_data=f"proxy_edit_data_{proxy_id}"),
        ],
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data=f"proxy_view_{proxy_id}"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_confirm_delete_proxy_keyboard(proxy_id: int) -> InlineKeyboardMarkup:
    """Клавиатура подтверждения удаления прокси."""
    keyboard = [
        [
            InlineKeyboardButton(text="⚠️ Да, удалить", callback_data=f"proxy_delete_{proxy_id}"),
        ],
        [
            InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"proxy_view_{proxy_id}"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_proxy_assign_keyboard(account_id: int, proxies: list) -> InlineKeyboardMarkup:
    """
    Клавиатура назначения прокси на аккаунт.

    Args:
        account_id: ID аккаунта
        proxies: Список объектов Proxy
    """
    keyboard = []
    for proxy in proxies:
        status_emoji = "🟢" if proxy.is_working else "🔴"
        keyboard.append([
            InlineKeyboardButton(
                text=f"{status_emoji} {proxy.name} ({proxy.host}:{proxy.port})",
                callback_data=f"proxy_assign_{account_id}_{proxy.id}"
            )
        ])
    keyboard.append([
        InlineKeyboardButton(text="❌ Без прокси", callback_data=f"proxy_assign_{account_id}_none"),
    ])
    keyboard.append([
        InlineKeyboardButton(text="⬅️ Назад", callback_data=f"account_view_{account_id}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_proxy_group_assign_keyboard(
    account_id: int,
    groups_usage: list[tuple[object, int, int]],
) -> InlineKeyboardMarkup:
    """Назначение прокси аккаунту через выбор группы прокси."""
    rows = []
    if not groups_usage:
        rows.append(
            [InlineKeyboardButton(text="📭 Нет групп прокси", callback_data="proxy_group_empty")]
        )
    else:
        for group, used, total in groups_usage:
            free = max(0, int(total) - int(used))
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"🌐 {group.name} ({used}/{total}, free {free})",
                        callback_data=f"proxy_group_assign_{account_id}_{group.id}",
                    )
                ]
            )
    rows.append(
        [InlineKeyboardButton(text="❌ Без прокси", callback_data=f"proxy_group_assign_{account_id}_none")]
    )
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"account_view_{account_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_tags_select_keyboard(tags: list, mailing_id: int) -> InlineKeyboardMarkup:
    """
    Клавиатура выбора тегов для запуска рассылки.

    Args:
        tags: Список уникальных тегов
        mailing_id: ID рассылки
    """
    keyboard = []

    # Располагаем по 2 тега в ряд для компактности
    for i in range(0, len(tags), 2):
        row = []
        row.append(InlineKeyboardButton(
            text=f"🏷 {tags[i]}",
            callback_data=f"mailing_tag_{mailing_id}_{tags[i]}"
        ))
        if i + 1 < len(tags):
            row.append(InlineKeyboardButton(
                text=f"🏷 {tags[i+1]}",
                callback_data=f"mailing_tag_{mailing_id}_{tags[i+1]}"
            ))
        keyboard.append(row)

    keyboard.append([
        InlineKeyboardButton(
            text="✅ Все аккаунты",
            callback_data=f"mailing_start_all_{mailing_id}"
        ),
    ])
    keyboard.append([
        InlineKeyboardButton(text="⬅️ Назад", callback_data=f"mailing_view_{mailing_id}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)
