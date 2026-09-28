"""Prompt saves, resets and restores keep an auditable active version."""

import asyncio
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session, sessionmaker

from control_plane.business.db import get_bot_db
from control_plane.business.mailings import router
from control_plane.deps import get_current_user
from database.models import Base, Mailing, NeuroPromptVersion
from database.repository import Database
from services.neurochat import prompt_history


def test_prompt_history_restore_conflicts_and_id_reuse(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'versions.db'}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(engine)
    with maker.begin() as db:
        db.add_all([Mailing(name="first", message_text="hi"),
                    Mailing(name="second", message_text="hi")])

    app = FastAPI()
    app.include_router(router)

    def test_db():
        with maker() as db:
            yield db

    app.dependency_overrides[get_bot_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        username="owner", id=1, role="tenant_admin",
    )
    base = "/business/mailings/1/prompt"
    try:
        with TestClient(app) as client:
            initial = client.get(base)
            assert initial.status_code == 200
            assert initial.headers["cache-control"] == "no-store"
            assert initial.json()["version_id"] is None
            assert initial.json()["has_custom_file"] is False

            saved = client.put(base, json={
                "text": "First prompt", "expected_version_id": None,
            })
            assert saved.status_code == 200
            assert saved.headers["cache-control"] == "no-store"
            first_id = saved.json()["version_id"]
            assert isinstance(first_id, int)
            assert saved.json()["has_custom_file"] is True
            assert "First prompt" in saved.json()["text"]

            stale = client.put(base, json={
                "text": "Lost update", "expected_version_id": None,
            })
            assert stale.status_code == 409
            second = client.put(base, json={
                "text": "Second prompt", "expected_version_id": first_id,
            })
            assert second.status_code == 200
            second_id = second.json()["version_id"]

            versions = client.get(f"{base}/versions")
            assert versions.status_code == 200
            assert versions.headers["cache-control"] == "no-store"
            assert versions.json()["current_version_id"] == second_id
            assert [item["action"] for item in versions.json()["versions"]] == ["save", "save"]
            assert client.get(f"{base}/versions/{first_id}").json()["text"] == "First prompt"
            assert client.get(f"{base}/versions/{first_id}").headers["cache-control"] == "no-store"
            assert client.post(f"{base}/versions/{first_id}/restore", json={
                "expected_version_id": first_id,
            }).status_code == 409
            assert client.post(f"{base}/versions/{first_id}/restore", json={
                "expected_version_id": second_id,
            }).status_code == 200
            restored = client.get(base).json()
            restore_id = restored["version_id"]
            assert restore_id not in (first_id, second_id)
            assert "First prompt" in restored["text"]
            assert client.post(f"{base}/versions/{first_id}/restore", json={
                "expected_version_id": restore_id,
            }).json()["version_id"] != first_id

            assert client.get("/business/mailings/2/prompt/versions").json()["versions"] == []
            assert client.post(f"/business/mailings/2/prompt/versions/{first_id}/restore").status_code == 404

            before_reset = client.get(base).json()["version_id"]
            reset = client.request("DELETE", base, json={"expected_version_id": before_reset})
            assert reset.status_code == 200
            assert reset.headers["cache-control"] == "no-store"
            assert reset.json()["has_custom_file"] is False
            assert reset.json()["version_id"] != before_reset
            assert client.get(f"{base}/versions/{reset.json()['version_id']}").json()["is_default"] is True
            assert client.request("DELETE", base, json={
                "expected_version_id": before_reset,
            }).status_code == 409

            with maker.begin() as db:
                db.delete(db.get(Mailing, 1))
            with maker.begin() as db:
                db.add(Mailing(id=1, name="reused", message_text="hi"))
            new_prompt = client.get(base).json()
            assert new_prompt["version_id"] is None
            assert new_prompt["has_custom_file"] is False
            assert client.get(f"{base}/versions/{first_id}").status_code == 404
    finally:
        engine.dispose()


def test_prompt_history_read_is_allowed_to_viewer_but_writes_are_not(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'rbac.db'}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(engine)
    with maker.begin() as db:
        db.add(Mailing(name="viewer", message_text="hi"))
    app = FastAPI()
    app.include_router(router)

    def test_db():
        with maker() as db:
            yield db

    app.dependency_overrides[get_bot_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        username="viewer", id=2, role="tenant_viewer",
    )
    try:
        with TestClient(app) as client:
            assert client.get("/business/mailings/1/prompt/versions").status_code == 200
            assert client.put("/business/mailings/1/prompt", json={"text": "new"}).status_code == 403
            assert client.request("DELETE", "/business/mailings/1/prompt").status_code == 403
            assert client.post("/business/mailings/1/prompt/versions/1/restore").status_code == 403
            with maker() as db:
                assert len(db.execute(select(Mailing)).scalars().all()) == 1
    finally:
        engine.dispose()


def test_legacy_prompt_import_is_idempotent_and_keeps_source_file(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as conn:
        conn.execute(sql_text("CREATE TABLE mailings (id INTEGER PRIMARY KEY)"))
        conn.execute(sql_text("INSERT INTO mailings (id) VALUES (7)"))
        NeuroPromptVersion.__table__.create(conn)
    prompt_dir = tmp_path / "prompts"
    legacy = prompt_dir / "7" / "system.txt"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("Imported prompt", encoding="utf-8")
    monkeypatch.setattr(prompt_history, "DATABASE_URL", str(engine.url))
    monkeypatch.setattr(prompt_history, "NEURO_MAILING_PROMPTS_DIR", prompt_dir)
    try:
        with engine.begin() as conn:
            prompt_history.migrate_prompt_history(conn, str(engine.url))
        with engine.begin() as conn:
            prompt_history.migrate_prompt_history(conn, str(engine.url))
            row = conn.execute(sql_text(
                "SELECT prompt_scope_uuid, prompt_revision FROM mailings WHERE id=7"
            )).one()
            versions = conn.execute(sql_text(
                "SELECT raw_text, action, actor FROM neuro_prompt_versions WHERE mailing_id=7"
            )).all()
        assert len(row.prompt_scope_uuid) == 32
        assert row.prompt_revision == 1
        assert versions == [("Imported prompt", "import", "migration")]
        assert legacy.read_text(encoding="utf-8") == "Imported prompt"
    finally:
        engine.dispose()


def test_bot_migration_and_control_plane_share_active_prompt(tmp_path):
    path = tmp_path / "shared.db"

    async def initialize():
        database = Database(f"sqlite+aiosqlite:///{path}")
        try:
            await database.connect()
            async with database.async_session_maker() as db:
                mailing = Mailing(name="shared", message_text="hi")
                db.add(mailing)
                await db.commit()
                assert mailing.prompt_scope_uuid
                version = await prompt_history.save_version_async(
                    db, mailing, raw_text="Live prompt", actor="telegram:1",
                )
                assert version.action == "upload"
                active = await prompt_history.current_version_async(db, mailing)
                assert active.id == version.id
        finally:
            await database.disconnect()

    asyncio.run(initialize())
    engine = create_engine(f"sqlite:///{path}")
    try:
        with Session(engine) as db:
            mailing = db.get(Mailing, 1)
            state = prompt_history.prompt_state(db, mailing)
            assert state["has_custom_file"] is True
            assert "Live prompt" in state["text"]
    finally:
        engine.dispose()
