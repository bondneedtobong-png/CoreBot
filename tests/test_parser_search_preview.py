"""Search previews show the queries that a channel/group task will run."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from control_plane.business.db import get_bot_db
from control_plane.business.parsing import router
from control_plane.deps import get_current_user
from database.models import Account, Base, ParsingTask
from workers.parser import querygen


def test_preview_normalizes_deduplicates_and_preserves_execution_order():
    preview = querygen.preview_channel_or_group_queries({
        "keywords": ["  Crypto   News ", "crypto news"],
        "endings": ["  chat ", "CHAT"],
        "manual_usernames_text": "@CRYPTO_NEWS\nhttps://t.me/group_one\n@Group_One",
    })
    assert preview["queries"] == ["Crypto News", "Crypto News chat", "crypto_news", "group_one"]
    assert preview["count"] == 4
    assert preview["errors"] == []


def test_preview_keeps_legacy_keyword_and_endings():
    preview = querygen.preview_channel_or_group_queries({
        "keyword": "  birds  ", "keyword_endings": ["  news  "]
    })
    assert preview["queries"] == ["birds", "birds news"]


def test_keyword_and_manual_source_share_case_insensitive_deduplication():
    preview = querygen.preview_channel_or_group_queries({
        "keyword": "Group_One", "manual_usernames_text": "@group_one",
        "keyword_endings": ["news"],
    })
    assert preview["queries"] == ["Group_One", "Group_One news"]
    assert preview["count"] == 2


@pytest.mark.parametrize("params,reason", [
    ({}, "empty_queries"),
    ({"keyword": "x" * 201}, "too_long"),
    ({"keywords": ["birds"], "endings": ["x" * 201]}, "too_long"),
    ({"manual_usernames_text": "https://t.me/+invitehash"}, "unsupported_link"),
    ({"endings": "news"}, "invalid_type"),
    ({"keywords": ["birds"] * 201}, "too_many_items"),
    ({"keywords": [f"key_{i}" for i in range(101)], "endings": ["news"]}, "too_many_queries"),
    ({"keyword": "birds", "manual_usernames_text": "@group_one\n" * 2001}, "too_many_lines"),
    ({"keyword": "birds", "endings": ["x" * 199]}, "too_long"),
])
def test_preview_reports_invalid_or_over_limit_input(params, reason):
    preview = querygen.preview_channel_or_group_queries(params)
    assert any(error["reason"] == reason for error in preview["errors"])


def test_query_limit_keeps_full_count_and_marks_preview_truncated():
    preview = querygen.preview_channel_or_group_queries({
        "keywords": [f"key_{i}" for i in range(101)], "endings": ["news"],
    })
    assert preview["count"] == 202
    assert len(preview["queries"]) == 200
    assert preview["truncated"] is True


def test_search_preview_api_and_create_task_share_validation(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'search-preview.db'}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(engine)
    with maker.begin() as session:
        session.add(Account(phone="+10000000001", session_name="parser-search-test"))

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
            for kind in ("channels", "groups"):
                valid = {"kind": kind, "params": {
                    "keyword": " birds ", "keyword_endings": ["news"],
                    "manual_usernames_text": "@group_one\n@GROUP_ONE",
                    "filters": {"members_min": "100", "members_max": 5000} if kind == "groups" else {},
                }}
                response = client.post("/business/parsing/search-preview", json=valid)
                assert response.status_code == 200
                assert response.json()["queries"] == ["birds", "birds news", "group_one"]
                assert response.json()["count"] == 3
                assert response.json()["error_count"] == 0
                assert response.json()["truncated"] is False

                queued = client.post("/business/parsing/tasks", json={
                    **valid, "account_ids": [1],
                })
                assert queued.status_code == 201
                assert queued.json()["params"]["keyword"] == " birds "
                assert queued.json()["params"]["manual_usernames_text"] == "group_one"
                if kind == "groups":
                    assert queued.json()["params"]["filters"]["members_min"] == 100
                    assert queued.json()["params"]["filters"]["members_max"] == 5000
                    for bad_filters in (
                        {"members_min": 50, "members_max": 49},
                        {"members_min": "1.5"},
                        {"members_max": -1},
                    ):
                        invalid_size = {"kind": kind, "account_ids": [1], "params": {
                            "keyword": "birds", "filters": bad_filters,
                        }}
                        assert client.post("/business/parsing/tasks", json=invalid_size).status_code == 422

                invalid = {"kind": kind, "account_ids": [1], "params": {
                    "keyword": "birds", "manual_usernames_text": "https://t.me/+invitehash",
                }}
                assert client.post("/business/parsing/tasks", json=invalid).status_code == 422
                invalid_preview = client.post("/business/parsing/search-preview", json=invalid)
                assert invalid_preview.status_code == 200
                assert invalid_preview.json()["error_count"] == 1
                assert invalid_preview.json()["errors"][0]["reason"] == "unsupported_link"
                assert client.post("/business/parsing/tasks", json={
                    "kind": kind, "account_ids": [1], "params": {},
                }).status_code == 422
                assert client.post("/business/parsing/tasks", json={
                    "kind": kind, "account_ids": [1], "params": {
                        "keywords": [f"key_{i}" for i in range(101)],
                        "endings": ["news"],
                    },
                }).status_code == 422

            assert client.post("/business/parsing/search-preview", json={
                "kind": "users", "params": {"keyword": "birds"},
            }).status_code == 422

            with maker() as session:
                tasks = session.execute(select(ParsingTask)).scalars().all()
                assert len(tasks) == 2
                assert all(task.status == "pending" for task in tasks)
    finally:
        engine.dispose()
