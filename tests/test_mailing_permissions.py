"""CRM import and mailing audience remain distinct until contact rights are recorded."""

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from types import SimpleNamespace

from control_plane.business.clients import router as clients_router
from control_plane.business.db import get_bot_db
from control_plane.business.mailings import router as mailings_router
from control_plane.deps import get_current_user, require_operator_write
from database.models import (
    Base, Client, ClientContactPermission, ClientStatus, Mailing, MailingStatus,
    MailingTestRecipient,
)
from database.repositories import ClientRepository


def test_regular_mailing_requires_recorded_contact_permission(tmp_path):
    async def scenario():
        from database.models import ClientContactPermission

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'consent.db'}")
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with maker() as session:
                session.add_all([
                    Client(username="requested", status=ClientStatus.NEW),
                    Client(username="public_only", status=ClientStatus.NEW),
                    Client(username="opted_out", status=ClientStatus.NEW),
                ])
                await session.commit()
                session.add_all([
                    ClientContactPermission(
                        client_id=1, state="opt_in", source="owned signup form", actor="operator",
                    ),
                    ClientContactPermission(
                        client_id=3, state="opt_out", source="recipient request", actor="operator",
                    ),
                ])
                await session.commit()
            async with maker() as session:
                rows = await ClientRepository.get_clients_for_mailing(
                    session, {"client_status": "new", "exclude_classes": ["bl", "stop"]},
                )
                assert [row.username for row in rows] == ["requested"]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_permission_api_and_mailing_preview_require_evidence():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(engine, expire_on_commit=False)
    with maker() as db:
        db.add_all([
            Client(username="requested", status=ClientStatus.NEW),
            Client(username="public_only", status=ClientStatus.NEW),
            Mailing(name="with-permission", message_text="hello", status=MailingStatus.DRAFT),
        ])
        db.commit()

    app = FastAPI()
    app.include_router(clients_router)
    app.include_router(mailings_router)

    def test_db():
        with maker() as db:
            yield db

    user = SimpleNamespace(username="owner", role="tenant_admin")
    app.dependency_overrides[get_bot_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[require_operator_write] = lambda: user
    try:
        with TestClient(app) as client:
            preview = client.get("/business/mailings/1/audience-preview")
            assert preview.status_code == 200
            assert preview.json()["eligible_count"] == 0
            assert client.post("/business/mailings/1/start").status_code == 409
            assert client.get("/business/clients/1").json()["contact_permission"] == "unverified"

            invalid = client.post("/business/clients/contact-permissions/bulk", json={
                "client_ids": [1], "state": "opt_in", "source": "x",
            })
            assert invalid.status_code == 422
            blank = client.post("/business/clients/contact-permissions/bulk", json={
                "client_ids": [1], "state": "opt_in", "source": "       ",
            })
            assert blank.status_code == 422
            granted = client.post("/business/clients/contact-permissions/bulk", json={
                "client_ids": [1], "state": "opt_in", "source": "owned signup form",
            })
            assert granted.status_code == 200
            assert client.get("/business/mailings/1/audience-preview").json()["eligible_count"] == 1
            assert client.post("/business/mailings/1/start").json()["status"] == "queued"

            withdrawn = client.post("/business/clients/contact-permissions/bulk", json={
                "client_ids": [1], "state": "opt_out", "source": "recipient requested stop",
            })
            assert withdrawn.status_code == 200
            assert client.get("/business/mailings/1/audience-preview").json()["eligible_count"] == 0
            assert client.get("/business/clients/1").json()["contact_permission"] == "opt_out"
            with maker() as db:
                assert db.get(ClientContactPermission, 1).state == "opt_out"
    finally:
        engine.dispose()


def test_permission_is_checked_again_for_each_mailing_attempt(tmp_path):
    from database.models import Account, AccountStatus
    from services.account_safety import reserve_send

    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'attempt.db'}")
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with maker() as session:
                session.add(Account(
                    phone="+15552223333", session_name="permission-account",
                    status=AccountStatus.ACTIVE, daily_limit=10,
                ))
                session.add(Client(username="permission-client", status=ClientStatus.NEW))
                await session.commit()
                session.add(ClientContactPermission(
                    client_id=1, state="opt_in", source="owned signup form", actor="owner",
                ))
                await session.commit()
            async with maker() as session:
                assert await reserve_send(
                    session, 1, source="mailing", client_id=1,
                ) == (True, "ok")
            async with maker() as session:
                permission = await session.get(ClientContactPermission, 1)
                permission.state = "opt_out"
                await session.commit()
            async with maker() as session:
                assert await reserve_send(
                    session, 1, source="mailing", client_id=1,
                ) == (False, "contact_permission")
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_test_mode_is_not_a_contact_permission_bypass(tmp_path):
    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test-mode.db'}")
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with maker() as session:
                session.add_all([
                    Client(username="owned_test", status=ClientStatus.NEW),
                    Client(username="unverified_test", status=ClientStatus.NEW),
                    Mailing(name="test", message_text="hello", audience_mode="test"),
                ])
                await session.commit()
                session.add_all([
                    MailingTestRecipient(mailing_id=1, client_id=1, username="owned_test"),
                    MailingTestRecipient(mailing_id=1, client_id=2, username="unverified_test"),
                    ClientContactPermission(
                        client_id=1, state="opt_in", source="owned test account", actor="owner",
                    ),
                ])
                await session.commit()
            async with maker() as session:
                rows = await ClientRepository.get_test_recipients_all(session, 1)
                assert [row.username for row in rows] == ["owned_test"]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_withdrawn_permission_skips_recipient_without_pausing_campaign():
    from workers.manager import _is_safety_stop

    assert not _is_safety_stop("SAFETY_STOP: contact_permission")
    assert _is_safety_stop("SAFETY_STOP: flood_wait")
