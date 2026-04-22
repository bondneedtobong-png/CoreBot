from __future__ import annotations

from services.neurochat.admin_service import (
    count_actions_by_mailing as admin_count_actions_by_mailing,
)


async def count_actions_by_mailing(session, mailing_id: int) -> dict[str, int]:
    """Агрегированные счётчики нейро-команд по рассылке."""
    return await admin_count_actions_by_mailing(session, mailing_id)

