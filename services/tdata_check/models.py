"""Типизированные модели результата проверки TData (задача 12)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

CHECK_STATUSES = (
    "archive_invalid",
    "structure_invalid",
    "conversion_failed",
    "proxy_required",
    "proxy_failed",
    "unauthorized",
    "session_revoked",
    "account_deactivated",
    "flood_wait",
    "spam_restriction",
    "ok",
    "unknown",
)

#: Статусы, при которых Telegram-профиль недоступен (поля профиля = None).
NON_PROFILE_STATUSES = frozenset(
    {
        "archive_invalid",
        "structure_invalid",
        "conversion_failed",
        "proxy_required",
        "proxy_failed",
        "unauthorized",
        "session_revoked",
        "account_deactivated",
        "flood_wait",
        "unknown",
    }
)


@dataclass
class TDataCheckItem:
    """Результат проверки одной найденной TData-папки."""

    item_id: str
    relpath: str
    status: str
    phone: Optional[str] = None
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    country: Optional[str] = None
    user_id: Optional[int] = None
    proxy_id: Optional[int] = None
    proxy_label: Optional[str] = None
    error_code: Optional[str] = None
    error_detail: Optional[str] = None
    retry_after: Optional[int] = None
    spam_restricted: Optional[bool] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "relpath": self.relpath,
            "status": self.status,
            "phone": self.phone,
            "username": self.username,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "country": self.country,
            "user_id": self.user_id,
            "proxy_id": self.proxy_id,
            "proxy_label": self.proxy_label,
            "error_code": self.error_code,
            "error_detail": self.error_detail,
            "retry_after": self.retry_after,
            "spam_restricted": self.spam_restricted,
        }


@dataclass
class TDataCheckRun:
    """Результат проверки целого ZIP (partial success по папкам)."""

    run_id: str
    status: str = "completed"
    created_at: str = ""
    total: int = 0
    ok_count: int = 0
    failed_count: int = 0
    truncated: bool = False
    error_code: Optional[str] = None
    error_detail: Optional[str] = None
    items: list[TDataCheckItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "created_at": self.created_at,
            "total": self.total,
            "ok_count": self.ok_count,
            "failed_count": self.failed_count,
            "truncated": self.truncated,
            "error_code": self.error_code,
            "error_detail": self.error_detail,
            "items": [i.to_dict() for i in self.items],
        }


__all__ = [
    "CHECK_STATUSES",
    "NON_PROFILE_STATUSES",
    "TDataCheckItem",
    "TDataCheckRun",
]
