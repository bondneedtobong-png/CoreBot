"""New account records retain their creation path without session material."""

import asyncio
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from control_plane.business.db import get_bot_db
from control_plane.business.tdata_routes import _create_account_from_tdata
from control_plane.deps import get_current_user
from control_plane.routes.business import router
from database.models import AccountImportEvent, Base, Proxy
from database.repositories import AccountRepository


def test_account_creation_origin_is_atomic_and_visible(tmp_path):
    db_path = tmp_path / "account-origin.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(engine)

    async def import_from_bot():
        async_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        async_maker = async_sessionmaker(async_engine, expire_on_commit=False)
        try:
            async with async_maker() as session:
                account = await AccountRepository.create(
                    session, phone="+10000000001", session_name="imported-tdata",
                    import_source="tdata_bot_v2",
                )
                return account.id
        finally:
            await async_engine.dispose()

    imported_id = asyncio.run(import_from_bot())
    app = FastAPI()
    app.include_router(router)

    def test_db():
        with maker() as session:
            yield session

    app.dependency_overrides[get_bot_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        username="owner", id=1, role="tenant_admin",
    )
    try:
        with TestClient(app) as client:
            detail = client.get(f"/business/accounts/{imported_id}")
            assert detail.status_code == 200
            assert detail.json()["import_source"] == "tdata_bot_v2"
            assert detail.json()["imported_at"]

            created = client.post("/business/accounts", json={"phone": "+10000000002"})
            assert created.status_code == 201
            assert created.json()["import_source"] == "manual_web"
            assert created.json()["created_at"]
            manual_id = created.json()["id"]
            assert client.get(f"/business/accounts/{manual_id}").json()["import_source"] == "manual_web"

            with maker.begin() as session:
                session.add(Proxy(name="test-proxy", host="127.0.0.1", port=1080))
            with maker() as session:
                result = {"phone": "+10000000003", "session_name": "web-tdata"}
                web_import_id = _create_account_from_tdata(session, result, proxy_id=1)
                assert _create_account_from_tdata(session, result, proxy_id=1) == web_import_id
            assert client.get(f"/business/accounts/{web_import_id}").json()["import_source"] == "tdata_web"

        with maker() as session:
            events = session.execute(select(AccountImportEvent).order_by(AccountImportEvent.id)).scalars().all()
            assert [(event.account_id, event.source_kind) for event in events] == [
                (imported_id, "tdata_bot_v2"), (manual_id, "manual_web"),
                (web_import_id, "tdata_web"),
            ]
            assert all(not hasattr(event, "session_name") for event in events)
    finally:
        engine.dispose()
