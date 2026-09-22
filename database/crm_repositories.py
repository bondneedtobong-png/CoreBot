"""
Репозитории CRM: счётчики классов, взаимодействия, сессии рассылки, accept-транскрипты.
"""
from __future__ import annotations

from datetime import datetime
from utils.time import utcnow_naive
from typing import Any, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

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
        result = await session.execute(
            select(ClientClassCounter).where(
                ClientClassCounter.client_id == client_id,
                ClientClassCounter.class_key == key,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            row = ClientClassCounter(client_id=client_id, class_key=key, count=max(0, delta))
            session.add(row)
        else:
            row.count = int(row.count or 0) + delta
            if row.count < 0:
                row.count = 0
        await session.commit()
        await session.refresh(row)
        return int(row.count)

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
        result = await session.execute(
            select(MailingLocalClassCounter).where(
                MailingLocalClassCounter.mailing_id == mailing_id,
                MailingLocalClassCounter.client_id == client_id,
                MailingLocalClassCounter.class_key == key,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            row = MailingLocalClassCounter(
                mailing_id=mailing_id,
                client_id=client_id,
                class_key=key,
                count=max(0, delta),
            )
            session.add(row)
        else:
            row.count = int(row.count or 0) + delta
            if row.count < 0:
                row.count = 0
        await session.commit()
        await session.refresh(row)
        return int(row.count)


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
        result = await session.execute(
            select(ClientAliveWindow).where(
                ClientAliveWindow.mailing_id == mailing_id,
                ClientAliveWindow.account_id == account_id,
                ClientAliveWindow.client_id == client_id,
                ClientAliveWindow.window_key == int(window_key),
            )
        )
        row = result.scalar_one_or_none()
        if row is not None:
            return False
        row = ClientAliveWindow(
            mailing_id=mailing_id,
            account_id=account_id,
            client_id=client_id,
            window_key=int(window_key),
        )
        session.add(row)
        await session.commit()
        return True


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
        result = await session.execute(
            select(ClientMailSession).where(
                ClientMailSession.client_id == client_id,
                ClientMailSession.account_id == account_id,
                ClientMailSession.mailing_id == mailing_id,
            )
        )
        row = result.scalar_one_or_none()
        if row:
            return row
        row = ClientMailSession(
            client_id=client_id,
            account_id=account_id,
            mailing_id=mailing_id,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
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
        result = await session.execute(
            select(ClientAcceptTranscript).where(
                ClientAcceptTranscript.mail_session_id == mail_session_id
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            row = ClientAcceptTranscript(
                mail_session_id=mail_session_id,
                messages_json=messages_json,
            )
            session.add(row)
        else:
            row.messages_json = messages_json
        await session.commit()
        await session.refresh(row)
        return row
