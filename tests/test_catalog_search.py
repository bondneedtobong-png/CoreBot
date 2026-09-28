"""Search the locally collected channel catalog without Telegram sessions."""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from types import SimpleNamespace

from control_plane.business.db import get_bot_db
from control_plane.business.parsing import router
from control_plane.deps import get_current_user, require_operator_write
from database.models import Base, ParsedChannel, ParsedGroup


def test_catalog_search_uses_stored_results_and_exposes_provenance():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    maker = sessionmaker(engine, expire_on_commit=False)
    with maker() as db:
        db.add_all([
            ParsedChannel(
                telegram_id=101, username="owned_news", title="Новости города",
                subscribers=500, lang="ru", has_discussion=True, is_active_7d=True,
                source_task_id=None,
            ),
            ParsedChannel(
                telegram_id=102, username="quiet_news", title="Новости редко",
                subscribers=20, lang="ru", has_discussion=False, is_active_7d=False,
            ),
            ParsedChannel(
                telegram_id=103, username="city_digest", title="Городские новости",
                subscribers=300, lang="ru", has_discussion=True, is_active_7d=True,
            ),
            ParsedChannel(
                telegram_id=104, username="other_lang", title="Новости города",
                subscribers=300, lang="en", has_discussion=True, is_active_7d=True,
            ),
            ParsedChannel(
                telegram_id=105, username="formula_test", title="=1+1",
                subscribers=1, lang="ru", has_discussion=False,
            ),
            ParsedGroup(
                telegram_id=201, username="owned_chat", title="Новости чат",
                members_count=100, lang="ru", group_type="public", is_active_7d=True,
            ),
        ])
        db.commit()

    app = FastAPI()
    app.include_router(router)

    def test_db():
        with maker() as db:
            yield db

    app.dependency_overrides[get_bot_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(username="reader")
    app.dependency_overrides[require_operator_write] = lambda: SimpleNamespace(username="owner")
    try:
        with TestClient(app) as client:
            result = client.get("/business/parsing/catalog/search", params={
                "q": "Новости", "kind": "channels", "lang": "ru",
                "min_count": 100, "has_discussion": True,
            })
            assert result.status_code == 200
            body = result.json()
            assert body["total"] == 1
            assert body["items"][0]["username"] == "owned_news"
            assert body["items"][0]["kind"] == "channel"
            assert "last_seen" in body["items"][0]
            assert "source_task_id" in body["items"][0]
            without_comments = client.get("/business/parsing/catalog/search", params={
                "q": "Новости", "kind": "channels", "lang": "ru",
                "has_discussion": "false",
            }).json()
            assert without_comments["total"] == 1
            assert [item["username"] for item in without_comments["items"]] == ["quiet_news"]
            first_page = client.get("/business/parsing/catalog/search", params={
                "q": "Новости", "kind": "channels", "lang": "ru", "limit": 1, "offset": 0,
            }).json()
            second_page = client.get("/business/parsing/catalog/search", params={
                "q": "Новости", "kind": "channels", "lang": "ru", "limit": 1, "offset": 1,
            }).json()
            assert first_page["total"] == second_page["total"] == 2
            assert first_page["items"][0]["telegram_id"] != second_page["items"][0]["telegram_id"]
            groups = client.get("/business/parsing/catalog/search", params={
                "kind": "groups", "q": "Новости",
            }).json()
            assert groups["total"] == 1
            assert groups["items"][0]["username"] == "owned_chat"
            similar = client.get("/business/parsing/catalog/similar", params={
                "kind": "channel", "telegram_id": 101,
            })
            assert similar.status_code == 200
            assert similar.json()["method"] == "title_tokens"
            assert [item["username"] for item in similar.json()["items"]] == [
                "quiet_news", "city_digest",
            ]
            assert similar.json()["items"][0]["shared_title_words"] == ["новости"]
            assert client.get("/business/parsing/catalog/similar", params={
                "kind": "channel", "telegram_id": 999,
            }).status_code == 404
            assert client.post("/business/parsing/catalog/folders", json={"name": "  "}).status_code == 422
            created = client.post("/business/parsing/catalog/folders", json={"name": "Мои площадки"})
            assert created.status_code == 201
            folder_id = created.json()["id"]
            assert client.post("/business/parsing/catalog/folders", json={"name": "Мои площадки"}).status_code == 409
            path = f"/business/parsing/catalog/folders/{folder_id}/entries"
            assert client.post(path, json={"kind": "channel", "telegram_id": 999}).status_code == 404
            added = client.post(path, json={"kind": "channel", "telegram_id": 101})
            assert added.status_code == 200 and added.json()["added"] is True
            assert client.post(path, json={"kind": "channel", "telegram_id": 101}).json()["added"] is False
            assert client.post(path, json={"kind": "group", "telegram_id": 201}).json()["added"] is True
            folders = client.get("/business/parsing/catalog/folders").json()
            assert folders[0]["entry_count"] == 2
            folder = client.get(f"/business/parsing/catalog/folders/{folder_id}").json()
            assert [item["username"] for item in folder["items"]] == ["owned_news", "owned_chat"]
            assert client.post(path, json={"kind": "channel", "telegram_id": 105}).status_code == 200
            export = client.get(f"/business/parsing/catalog/folders/{folder_id}/export.csv")
            assert export.status_code == 200
            assert export.text.startswith("\ufeffkind,telegram_id")
            assert "'=1+1" in export.text
            assert client.delete(f"{path}/channel/101").json()["removed"] is True
            assert client.delete(f"{path}/channel/101").json()["removed"] is False
            assert client.delete(f"/business/parsing/catalog/folders/{folder_id}").json()["deleted"] is True
            assert client.get(f"/business/parsing/catalog/folders/{folder_id}").status_code == 404
    finally:
        engine.dispose()
