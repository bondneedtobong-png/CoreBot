"""Pydantic-схемы для бизнес-API (аккаунты/диалоги/сообщения/очередь)."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ===== Accounts =====


class AccountListItem(BaseModel):
    id: int
    phone: Optional[str] = None
    username: Optional[str] = None
    list_label: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    status: Optional[str] = None
    membership: Optional[str] = None
    ai_mode: str = "AI_ACTIVE"
    last_activity: Optional[datetime] = None
    dialogs_count: int = 0
    pending_outbound: int = 0
    # Время самого свежего сообщения в любом из диалогов аккаунта
    # (max(neuro_chat_messages.created_at, outbound_queue.created_at)).
    # Нужно UI «Диалоги», чтобы сортировать аккаунты как мессенджер.
    last_dialog_at: Optional[datetime] = None


class AccountModeIn(BaseModel):
    mode: str = Field(pattern="^(AI_ACTIVE|MANUAL)$")


# ===== Dialogs =====


class DialogListItem(BaseModel):
    account_id: int
    peer_user_id: int
    client_id: Optional[int] = None
    client_username: Optional[str] = None
    last_message: Optional[str] = None
    last_message_at: Optional[datetime] = None
    last_role: Optional[str] = None  # 'user' | 'assistant'
    messages_count: int = 0


class MessageOut(BaseModel):
    id: int
    role: str  # 'user' | 'assistant'
    content: str
    created_at: datetime
    # Источник строки в ленте:
    #   'neuro'   — реально доставленное сообщение из neuro_chat_messages
    #   'queue'   — строка outbound_queue (ещё не доставлена / failed / cancelled)
    source: str = "neuro"
    # Для строк из очереди:
    queue_status: Optional[str] = None    # pending | sending | sent | failed | cancelled
    queue_error: Optional[str] = None
    queue_attempts: Optional[int] = None
    queue_id: Optional[int] = None


class SendMessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class SendMessageOut(BaseModel):
    queue_id: int
    status: str = "pending"
    enqueued_at: datetime


class QueueListItem(BaseModel):
    queue_id: int
    account_id: int
    account_title: str
    peer_user_id: int
    client_id: Optional[int] = None
    client_username: Optional[str] = None
    text: str
    status: str
    error: Optional[str] = None
    attempts: int = 0
    requested_by: Optional[str] = None
    created_at: datetime
    next_attempt_at: Optional[datetime] = None
    sent_at: Optional[datetime] = None


class QueueBulkActionIn(BaseModel):
    action: str = Field(pattern="^(retry|cancel)$")
    queue_ids: list[int] = Field(default_factory=list, min_length=1, max_length=500)


class QueueBulkActionOut(BaseModel):
    requested: int
    updated: int
    skipped: int


# ===== Cleanup =====


class CleanupRequest(BaseModel):
    """Параметры безопасной очистки переписок.

    Все поля опциональны; должен быть задан хотя бы один из фильтров,
    иначе будет 400. soft=True — только пометить (зарезервировано
    под будущий soft-delete; сейчас всегда hard delete внутри батчей).
    """

    account_id: Optional[int] = None
    peer_user_id: Optional[int] = None
    older_than_days: Optional[int] = Field(default=None, ge=1, le=3650)
    classes: Optional[list[str]] = None
    dry_run: bool = False
    batch_size: int = Field(default=500, ge=1, le=5000)


class CleanupRequestV2(BaseModel):
    """
    Расширенный запрос очистки.
    mode='hard'    — физически удалить из основных таблиц (как раньше).
    mode='archive' — перенести в архивные таблицы (*_archive)
                     перед удалением, оставив возможность восстановления.
    """

    account_id: Optional[int] = None
    peer_user_id: Optional[int] = None
    older_than_days: Optional[int] = Field(default=None, ge=1, le=3650)
    classes: Optional[list[str]] = None
    dry_run: bool = False
    batch_size: int = Field(default=500, ge=1, le=5000)
    mode: str = Field(default="archive", pattern="^(archive|hard)$")


class CleanupResult(BaseModel):
    dialogs_deleted: int
    messages_deleted: int
    interactions_deleted: int
    messages_archived: int = 0
    interactions_archived: int = 0
    dry_run: bool
    mode: str = "hard"


class ArchiveRestoreRequest(BaseModel):
    """Восстановление из архива. Должен быть задан хотя бы один фильтр."""

    account_id: Optional[int] = None
    peer_user_id: Optional[int] = None
    archived_after: Optional[datetime] = None
    archived_before: Optional[datetime] = None
    batch_size: int = Field(default=500, ge=1, le=5000)


class ArchiveRestoreResult(BaseModel):
    messages_restored: int
    interactions_restored: int


class ArchivedDialogItem(BaseModel):
    account_id: int
    peer_user_id: int
    account_title: Optional[str] = None
    client_username: Optional[str] = None
    messages_count: int
    last_archived_at: Optional[datetime] = None


# ===== Dashboard =====


class DashboardSummary(BaseModel):
    generated_at: datetime
    accounts_total: int
    accounts_authorized: int
    accounts_ai: int
    accounts_manual: int
    mailings_running: int
    mailings_paused: int
    mailings_completed_24h: int
    messages_in_24h: int
    messages_out_24h: int
    manual_sent_24h: int
    manual_pending: int
    manual_failed: int
    mailing_sent_24h: int
    mailing_failed_24h: int
    clients_total: int
    dialogs_total: int
    dialogs_24h: int


class DashboardTimePoint(BaseModel):
    ts: str  # 'YYYY-MM-DD HH:00' (UTC)
    messages_in: int
    messages_out: int
    manual_sent: int


class DashboardTopAccount(BaseModel):
    account_id: int
    title: str
    ai_mode: str
    messages_in: int
    messages_out: int
    total: int


class DashboardRecentMessage(BaseModel):
    id: int
    account_id: int
    account_title: str
    peer_user_id: int
    peer_title: str
    role: str
    content: str
    created_at: datetime


class DashboardClassDistributionItem(BaseModel):
    class_key: str
    clients: int
    events: int


class DashboardMailingItem(BaseModel):
    id: int
    name: str
    status: str
    total: int
    sent: int
    failed: int
    started_at: Optional[datetime] = None


# ===== Mailings =====


class MailingListItem(BaseModel):
    id: int
    name: str
    status: str
    total: int
    sent: int
    failed: int
    audience_mode: str
    neurochat_enabled: bool
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: datetime


class MailingDetail(MailingListItem):
    message_text: str
    message_variants: list[str] = []
    delay_between_messages: float
    delay_between_accounts: float
    daily_limit: int
    messages_per_batch: int
    batch_delay: float
    auto_stop_hours: Optional[float] = None
    target_group_id: Optional[int] = None
    community_link: Optional[str] = None


class MailingActionResult(BaseModel):
    mailing_id: int
    command: str   # start | pause | stop
    command_id: int
    status: str    # queued | rejected
    detail: Optional[str] = None


# ===== Clients =====


class ClientClassCount(BaseModel):
    class_key: str
    count: int


class ClientListItem(BaseModel):
    id: int
    username: str
    telegram_user_id: Optional[int] = None
    status: str
    added_at: datetime
    last_contacted_at: Optional[datetime] = None
    classes: list[ClientClassCount] = []


class ClientInteractionItem(BaseModel):
    id: int
    direction: str
    kind: str
    body: Optional[str] = None
    account_id: Optional[int] = None
    mailing_id: Optional[int] = None
    created_at: datetime


class ClientDetail(ClientListItem):
    interactions_count: int
    tags: list[str] = []


class ClientClassUpdate(BaseModel):
    class_key: str = Field(min_length=1, max_length=64)
    delta: int = Field(default=1)  # может быть отрицательным
    set_value: Optional[int] = None  # если задано — count := set_value


class ClientClassUpdateResult(BaseModel):
    client_id: int
    class_key: str
    new_count: int


# ===== Instance settings =====


class InstanceSettingsOut(BaseModel):
    neurochat_enabled_db: Optional[bool] = None
    neurochat_enabled_effective: bool
    mailing_base_utc_offset_db: Optional[int] = None
    mailing_base_utc_offset_effective: int
    openrouter_key_set: bool
    openrouter_key_masked: Optional[str] = None
    openrouter_key_encrypted: bool


class InstanceSettingsPatch(BaseModel):
    neurochat_enabled: Optional[bool] = None
    # None — оставить как есть; чтобы сбросить до .env, передайте reset_*=True.
    mailing_base_utc_offset: Optional[int] = Field(default=None, ge=-12, le=14)
    reset_neurochat_enabled: bool = False
    reset_mailing_base_utc_offset: bool = False


class OpenRouterKeyIn(BaseModel):
    key: str = Field(min_length=8, max_length=512)


# ===== Accounts (extended) =====


class AccountDetail(AccountListItem):
    bio: Optional[str] = None
    tags: Optional[str] = None
    daily_limit: int = 0
    messages_today: int = 0
    messages_sent: int = 0
    messages_failed: int = 0
    warmup_enabled: bool = False
    warmup_profile: Optional[str] = None
    proxy_id: Optional[int] = None
    proxy_label: Optional[str] = None
    flood_wait_until: Optional[datetime] = None
    is_spam_blocked: bool = False
    group_ids: list[int] = []


class AccountPatch(BaseModel):
    list_label: Optional[str] = Field(default=None, max_length=64)
    first_name: Optional[str] = Field(default=None, max_length=100)
    last_name: Optional[str] = Field(default=None, max_length=100)
    bio: Optional[str] = Field(default=None, max_length=2000)
    tags: Optional[str] = Field(default=None, max_length=500)
    status: Optional[str] = Field(
        default=None,
        pattern="^(active|inactive|banned|flood_wait|error|spam_blocked)$",
    )
    membership: Optional[str] = Field(default=None, pattern="^(READY|WARMUP|TEST)$")
    daily_limit: Optional[int] = Field(default=None, ge=0, le=10000)
    warmup_enabled: Optional[bool] = None
    warmup_profile: Optional[str] = Field(default=None, max_length=50)
    proxy_id: Optional[int] = None  # 0 / -1 — снять прокси
    group_ids: Optional[list[int]] = None  # если задано — заменяет список групп


class AccountCreate(BaseModel):
    phone: str = Field(min_length=3, max_length=32)
    session_name: Optional[str] = Field(default=None, max_length=255)
    username: Optional[str] = Field(default=None, max_length=100)
    list_label: Optional[str] = Field(default=None, max_length=64)
    first_name: Optional[str] = Field(default=None, max_length=100)
    last_name: Optional[str] = Field(default=None, max_length=100)
    membership: str = Field(default="TEST", pattern="^(READY|WARMUP|TEST)$")
    status: str = Field(
        default="inactive",
        pattern="^(active|inactive|banned|flood_wait|error|spam_blocked)$",
    )
    ai_mode: str = Field(default="MANUAL", pattern="^(AI_ACTIVE|MANUAL)$")
    proxy_id: Optional[int] = None
    group_ids: list[int] = []


# ===== Groups =====


class GroupItem(BaseModel):
    id: int
    name: str
    accounts_count: int
    created_at: datetime


class GroupAccountsItem(BaseModel):
    id: int
    title: str
    username: Optional[str] = None
    phone: Optional[str] = None


class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class GroupRename(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class GroupAccountsSet(BaseModel):
    account_ids: list[int]


# ===== Proxies =====


class ProxyGroupItem(BaseModel):
    id: int
    name: str
    proxies_count: int


class ProxyItem(BaseModel):
    id: int
    name: str
    host: str
    port: int
    username: Optional[str] = None
    proxy_type: str
    is_active: bool
    is_working: bool
    group_id: Optional[int] = None
    group_name: Optional[str] = None
    accounts_count: int
    last_checked: Optional[datetime] = None


class ProxyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    username: Optional[str] = Field(default=None, max_length=100)
    password: Optional[str] = Field(default=None, max_length=100)
    proxy_type: str = Field(default="socks5", pattern="^(socks5|http)$")
    group_id: Optional[int] = None
    is_active: bool = True


class ProxyPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    host: Optional[str] = Field(default=None, min_length=1, max_length=255)
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    username: Optional[str] = Field(default=None, max_length=100)
    password: Optional[str] = Field(default=None, max_length=100)
    proxy_type: Optional[str] = Field(default=None, pattern="^(socks5|http)$")
    group_id: Optional[int] = None
    is_active: Optional[bool] = None


class ProxyTestResult(BaseModel):
    proxy_id: int
    ok: bool
    elapsed_ms: int
    detail: Optional[str] = None


# ===== Mailings (edit + prompt) =====


class MailingPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    message_text: Optional[str] = Field(default=None, max_length=8000)
    message_variants: Optional[list[str]] = None
    delay_between_messages: Optional[float] = Field(default=None, ge=0, le=600)
    delay_between_accounts: Optional[float] = Field(default=None, ge=0, le=600)
    daily_limit: Optional[int] = Field(default=None, ge=0, le=10000)
    messages_per_batch: Optional[int] = Field(default=None, ge=0, le=10000)
    batch_delay: Optional[float] = Field(default=None, ge=0, le=86400)
    auto_stop_hours: Optional[float] = Field(default=None, ge=0, le=720)
    target_group_id: Optional[int] = None
    community_link: Optional[str] = Field(default=None, max_length=1024)
    neurochat_enabled: Optional[bool] = None
    neuro_model: Optional[str] = Field(default=None, max_length=255)
    neuro_sampling_json: Optional[str] = Field(default=None, max_length=4000)
    audience_mode: Optional[str] = Field(default=None, pattern="^(classes|test|all)$")


class MailingCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    message_text: str = Field(default="", max_length=8000)
    audience_mode: str = Field(default="classes", pattern="^(classes|test|all)$")
    target_group_id: Optional[int] = None
    neurochat_enabled: bool = False


class MailingPromptOut(BaseModel):
    mailing_id: int
    text: str
    has_custom_file: bool


class MailingPromptIn(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
