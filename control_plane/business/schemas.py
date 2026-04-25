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
    source: str = "neuro"  # 'neuro' | 'manual'


class SendMessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class SendMessageOut(BaseModel):
    queue_id: int
    status: str = "pending"
    enqueued_at: datetime


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


class CleanupResult(BaseModel):
    dialogs_deleted: int
    messages_deleted: int
    interactions_deleted: int
    dry_run: bool
