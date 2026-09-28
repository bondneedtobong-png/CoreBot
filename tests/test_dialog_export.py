"""Dialog download includes stored messages and a content-free access log."""

import asyncio
import csv
import io
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, insert, select, text
from sqlalchemy.orm import sessionmaker

from control_plane.business.db import get_bot_db
from control_plane.deps import get_current_user
from control_plane.routes.business import router
from database.models import (
    Account, Base, DialogExportAudit, DialogViewAudit, NeuroChatMessage, OutboundQueue,
)
from database.sqlite_pragmas import register_sqlite_pragmas
from database.repository import Database


def test_dialog_csv_txt_export_and_audit(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'export.db'}", connect_args={"check_same_thread": False},
    )
    register_sqlite_pragmas(engine)
    Base.metadata.create_all(engine)
    maker = sessionmaker(engine, expire_on_commit=False)
    with maker.begin() as db:
        db.add(Account(phone="+10000000451", session_name="export-account"))
        db.flush()
        db.add_all([
            NeuroChatMessage(account_id=1, peer_user_id=123, role="user", content="hello\nworld"),
            NeuroChatMessage(account_id=1, peer_user_id=123, role="assistant", content="=SUM(1,2)"),
            NeuroChatMessage(account_id=1, peer_user_id=456, role="user", content="other peer"),
            OutboundQueue(account_id=1, peer_user_id=123, text="pending secret", status="pending"),
        ])
    app = FastAPI()
    app.include_router(router)

    def test_db():
        with maker() as session:
            yield session

    app.dependency_overrides[get_bot_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=7, username="operator", role="tenant_admin",
    )
    try:
        with TestClient(app) as client:
            endpoint = "/business/accounts/1/dialogs/123/export"
            csv_response = client.get(endpoint)
            assert csv_response.status_code == 200
            assert csv_response.headers["cache-control"] == "no-store"
            assert "attachment" in csv_response.headers["content-disposition"]
            lines = list(csv.reader(io.StringIO(csv_response.text.lstrip("\ufeff"))))
            assert lines[0] == ["message_id", "created_at_utc", "role", "content"]
            assert [line[2] for line in lines[1:]] == ["user", "assistant"]
            assert lines[1][3] == "hello\nworld"
            assert lines[2][3] == "'=SUM(1,2)"
            assert "pending secret" not in csv_response.text
            assert "other peer" not in csv_response.text

            txt_response = client.get(endpoint, params={"format": "txt"})
            assert txt_response.status_code == 200
            assert "hello\\nworld" in txt_response.text
            assert "=SUM(1,2)" in txt_response.text
            history = client.get("/business/accounts/1/dialogs/123/exports")
            assert history.status_code == 200
            assert [(item["format"], item["message_count"]) for item in history.json()] == [
                ("txt", 2), ("csv", 2),
            ]
            assert "content" not in history.text
            assert client.get(endpoint, params={"format": "html"}).status_code == 422
            assert client.get("/business/accounts/999/dialogs/123/export").status_code == 404

            app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
                id=8, username="viewer", role="tenant_viewer",
            )
            assert client.get(endpoint).status_code == 403
            messages = "/business/accounts/1/dialogs/123/messages"
            assert client.get(messages).status_code == 200
            assert client.get(messages).status_code == 200
            assert client.get("/business/accounts/1/dialogs/123/views").status_code == 403
            app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
                id=7, username="operator", role="tenant_admin",
            )
            assert client.get(messages).status_code == 200
            views = client.get("/business/accounts/1/dialogs/123/views")
            assert views.status_code == 200
            assert [item["operator_user_id"] for item in views.json()] == [7, 8]
            assert "content" not in views.text
            with maker.begin() as db:
                db.execute(insert(NeuroChatMessage), [
                    {"account_id": 1, "peer_user_id": 789, "role": "user", "content": f"row-{i}"}
                    for i in range(10001)
                ])
            long_response = client.get("/business/accounts/1/dialogs/789/export")
            assert long_response.status_code == 200
            long_rows = list(csv.reader(io.StringIO(long_response.text.lstrip("\ufeff"))))
            assert len(long_rows) == 10002  # header and every saved message
            assert long_rows[1][3] == "row-0"
            assert long_rows[-1][3] == "row-10000"
        with maker() as db:
            audit = db.execute(select(DialogExportAudit).order_by(DialogExportAudit.id)).scalars().all()
            assert len(audit) == 3
            assert all(item.operator_user_id == 7 for item in audit)
            assert audit[-1].peer_user_id == 789 and audit[-1].message_count == 10001
            assert all(not hasattr(item, "content") for item in audit)
            view_audit = db.execute(select(DialogViewAudit)).scalars().all()
            assert len(view_audit) == 2
    finally:
        engine.dispose()


def test_existing_database_adds_export_page_index(tmp_path):
    db_path = tmp_path / "existing.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX ix_neuro_chat_export_page"))
    engine.dispose()

    async def migrate():
        database = Database(f"sqlite+aiosqlite:///{db_path}")
        try:
            await database.connect()
        finally:
            await database.engine.dispose()

    asyncio.run(migrate())
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            indexes = conn.execute(text("PRAGMA index_list(neuro_chat_messages)"))
            assert "ix_neuro_chat_export_page" in {row[1] for row in indexes}
    finally:
        engine.dispose()
