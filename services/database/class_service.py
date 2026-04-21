"""
Инкремент классов CRM по client_id (используется из воркеров и нейрочата).
"""
from __future__ import annotations

from database.crm_repositories import ClientClassCounterRepository
from database.session import session_scope


async def increment_client_class(client_id: int, class_key: str, delta: int = 1) -> int:
    async with session_scope() as session:
        return await ClientClassCounterRepository.increment(
            session, client_id, class_key, delta=delta
        )
