"""
Репозитории CRM: счётчики классов, взаимодействия, сессии рассылки, accept-транскрипты.
"""
from __future__ import annotations

from datetime import datetime
from utils.time import utcnow_naive
from typing import Any, Optional

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from database.sqlite_pragmas import commit_with_busy_retry, execute_with_busy_retry

from database.models import (
    ClientAcceptTranscript,
    ClientAliveWindow,
    ClientClassCounter,
    ClientInteraction,
    ClientMailSession,
    MailingLocalClassCounter,
)


class ClientClassCounterRepository:
    @staticmethod
    async def increment(
        session: AsyncSession,
        client_id: int,
        class_key: str,
        delta: int = 1,
    ) -> int:
        key = (class_key or "").strip().lower()
        if not key:
            raise ValueError("class_key пустой")
        # Атомарный UPSERT вместо SELECT→INSERT: конкурентные инкременты
        # не дают UNIQUE-конфликт, счётчик суммируется (floor 0).
        stmt = sqlite_insert(ClientClassCounter).values(
            client_id=client_id, class_key=key, count=max(0, delta)
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[ClientClassCounter.client_id, ClientClassCounter.class_key],
            set_={"count": func.max(0, ClientClassCounter.count + delta)},
        )
        await execute_with_busy_retry(session, stmt, op_name="class-counter-incr")
        await commit_with_busy_retry(session, op_name="class-counter-incr")
        result = await session.execute(
            select(ClientClassCounter).where(
                ClientClassCounter.client_id == client_id,
                ClientClassCounter.class_key == key,
            )
        )
        row = result.scalar_one()
        return int(row.count or 0)

    @staticmethod
    async def get_counts(
        session: AsyncSession,
        client_id: int,
    ) -> dict[str, int]:
        result = await session.execute(
            select(ClientClassCounter.class_key, ClientClassCounter.count).where(
                ClientClassCounter.client_id == client_id
            )
        )
        return {str(r[0]): int(r[1]) for r in result.all()}


class MailingLocalClassCounterRepository:
    """Счётчики классов внутри тестовой рассылки (не глобальная CRM)."""

    @staticmethod
    async def get_count(
        session: AsyncSession,
        mailing_id: int,
        client_id: int,
        class_key: str,
    ) -> int:
        key = (class_key or "").strip().lower()
        if not key:
            return 0
        result = await session.execute(
            select(MailingLocalClassCounter).where(
                MailingLocalClassCounter.mailing_id == mailing_id,
                MailingLocalClassCounter.client_id == client_id,
                MailingLocalClassCounter.class_key == key,
            )
        )
        row = result.scalar_one_or_none()
        return int(row.count or 0) if row else 0

    @staticmethod
    async def increment(
        session: AsyncSession,
        mailing_id: int,
        client_id: int,
        class_key: str,
        delta: int = 1,
    ) -> int:
        key = (class_key or "").strip().lower()
        if not key:
            raise ValueError("class_key пустой")
        # Атомарный UPSERT вместо SELECT→INSERT (см. ClientClassCounterRepository).
        stmt = sqlite_insert(MailingLocalClassCounter).values(
            mailing_id=mailing_id,
            client_id=client_id,
            class_key=key,
            count=max(0, delta),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                MailingLocalClassCounter.mailing_id,
                MailingLocalClassCounter.client_id,
                MailingLocalClassCounter.class_key,
            ],
            set_={"count": func.max(0, MailingLocalClassCounter.count + delta)},
        )
        await execute_with_busy_retry(session, stmt, op_name="local-counter-incr")
        await commit_with_busy_retry(session, op_name="local-counter-incr")
        result = await session.execute(
            select(MailingLocalClassCounter).where(
                MailingLocalClassCounter.mailing_id == mailing_id,
                MailingLocalClassCounter.client_id == client_id,
                MailingLocalClassCounter.class_key == key,
            )
        )
        row = result.scalar_one()
        return int(row.count or 0)


class ClientInteractionRepository:
    @staticmethod
    async def add(
        session: AsyncSession,
        *,
        client_id: int,
        kind: str,
        direction: str = "in",
        account_id: Optional[int] = None,
        mailing_id: Optional[int] = None,
        body: Optional[str] = None,
        payload_json: Optional[str] = None,
        telegram_message_id: Optional[int] = None,
    ) -> ClientInteraction:
        ev = ClientInteraction(
            client_id=client_id,
            account_id=account_id,
            mailing_id=mailing_id,
            direction=direction,
            kind=kind,
            body=body,
            payload_json=payload_json,
            telegram_message_id=telegram_message_id,
        )
        session.add(ev)
        await session.commit()
        await session.refresh(ev)
        return ev


class ClientAliveWindowRepository:
    @staticmethod
    async def create_if_absent(
        session: AsyncSession,
        *,
        mailing_id: int,
        account_id: int,
        client_id: int,
        window_key: int,
    ) -> bool:
        """Идемпотентное окно alive: True — создано сейчас, False — уже было.

        INSERT ... ON CONFLICT DO NOTHING вместо SELECT→INSERT: конкурентные
        воркеры не дают UNIQUE-конфликт, rowcount различает исходы.
        """
        stmt = sqlite_insert(ClientAliveWindow).values(
            mailing_id=mailing_id,
            account_id=account_id,
            client_id=client_id,
            window_key=int(window_key),
        )
        stmt = stmt.on_conflict_do_nothing(
            index_elements=[
                ClientAliveWindow.mailing_id,
                ClientAliveWindow.account_id,
                ClientAliveWindow.client_id,
                ClientAliveWindow.window_key,
            ]
        )
        result = await execute_with_busy_retry(session, stmt, op_name="alive-window")
        await commit_with_busy_retry(session, op_name="alive-window")
        return int(result.rowcount or 0) == 1


class ClientMailSessionRepository:
    @staticmethod
    async def get_by_id(
        session: AsyncSession,
        mail_session_id: int,
    ) -> Optional[ClientMailSession]:
        result = await session.execute(
            select(ClientMailSession).where(ClientMailSession.id == mail_session_id)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_or_create(
        session: AsyncSession,
        client_id: int,
        account_id: int,
        mailing_id: int,
    ) -> ClientMailSession:
        """Идемпотентно: INSERT ... ON CONFLICT DO NOTHING + SELECT."""
        stmt = sqlite_insert(ClientMailSession).values(
            client_id=client_id,
            account_id=account_id,
            mailing_id=mailing_id,
        )
        stmt = stmt.on_conflict_do_nothing(
            index_elements=[
                ClientMailSession.client_id,
                ClientMailSession.account_id,
                ClientMailSession.mailing_id,
            ]
        )
        await execute_with_busy_retry(session, stmt, op_name="mail-session")
        await commit_with_busy_retry(session, op_name="mail-session")
        result = await session.execute(
            select(ClientMailSession).where(
                ClientMailSession.client_id == client_id,
                ClientMailSession.account_id == account_id,
                ClientMailSession.mailing_id == mailing_id,
            )
        )
        row = result.scalar_one()
        return row

    @staticmethod
    async def set_first_outbound(
        session: AsyncSession,
        mail_session_id: int,
        at: Optional[datetime] = None,
    ) -> None:
        await session.execute(
            update(ClientMailSession)
            .where(ClientMailSession.id == mail_session_id)
            .values(
                first_outbound_at=at or utcnow_naive(),
                updated_at=utcnow_naive(),
            )
        )
        await session.commit()

    @staticmethod
    async def set_success_end(
        session: AsyncSession,
        mail_session_id: int,
        at: Optional[datetime] = None,
    ) -> None:
        await session.execute(
            update(ClientMailSession)
            .where(ClientMailSession.id == mail_session_id)
            .values(
                success_end_at=at or utcnow_naive(),
                updated_at=utcnow_naive(),
            )
        )
        await session.commit()


class ClientAcceptTranscriptRepository:
    @staticmethod
    async def upsert(
        session: AsyncSession,
        mail_session_id: int,
        messages_json: str,
    ) -> ClientAcceptTranscript:
        """ON CONFLICT(mail_session_id) DO UPDATE вместо SELECT→INSERT."""
        stmt = sqlite_insert(ClientAcceptTranscript).values(
            mail_session_id=mail_session_id,
            messages_json=messages_json,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[ClientAcceptTranscript.mail_session_id],
            set_={"messages_json": messages_json},
        )
        await execute_with_busy_retry(session, stmt, op_name="accept-transcript")
        await commit_with_busy_retry(session, op_name="accept-transcript")
        result = await session.execute(
            select(ClientAcceptTranscript).where(
                ClientAcceptTranscript.mail_session_id == mail_session_id
            )
        )
        return result.scalar_one()
