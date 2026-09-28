"""User parser sources are checked before a task enters the worker queue."""

import csv
import io
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from control_plane.business.db import get_bot_db
from control_plane.business.parsing import router
from control_plane.deps import get_current_user
from database.models import (
    Account, Base, ParsedChannel, ParsedGroup, ParsedUser, ParsedUserSource,
    ParsingTask,
)
from workers.parser.querygen import preview_manual_sources


def test_preview_reports_duplicates_and_line_errors():
    result = preview_manual_sources(
        "@Group_One\nhttps://t.me/group_one\n-1003993284688\n"
        "https://t.me/+invitehash\nnot a source\n"
    )
    assert result["sources"] == ["group_one", "-1003993284688"]
    assert result["duplicates"] == 1
    assert result["errors"] == [
        {"line": 4, "reason": "unsupported_link"},
        {"line": 5, "reason": "invalid_source"},
    ]


def test_invalid_sources_never_queue_a_users_task(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'sources.db'}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(engine)
    with maker.begin() as session:
        session.add(Account(phone="+10000000001", session_name="parser-test"))

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
            source_text = "@Group_One\nhttps://t.me/group_one\nhttps://t.me/+invitehash"
            preview = client.post("/business/parsing/sources-preview", json={"text": source_text})
            assert preview.status_code == 200
            assert preview.json()["count"] == 1
            assert preview.json()["duplicates"] == 1
            assert preview.json()["error_count"] == 1

            payload = {
                "kind": "users", "account_ids": [1],
                "params": {"user_inputs_text": source_text},
            }
            invalid = client.post("/business/parsing/tasks", json=payload)
            assert invalid.status_code == 422
            with maker() as session:
                assert session.execute(select(ParsingTask)).scalars().all() == []

            payload["params"]["user_inputs_text"] = "@Group_One\nhttps://t.me/group_one"
            created = client.post("/business/parsing/tasks", json=payload)
            assert created.status_code == 201
            assert created.json()["params"]["user_inputs_text"] == "group_one"
            with maker() as session:
                tasks = session.execute(select(ParsingTask)).scalars().all()
                assert len(tasks) == 1
                assert tasks[0].status == "pending"

            with maker.begin() as session:
                other_task = ParsingTask(kind="users", status="done")
                session.add(other_task)
                session.flush()
                person = ParsedUser(
                    telegram_id=501, username="source_user", display_name="=formula",
                    source_task_id=other_task.id,
                )
                session.add(person)
                session.flush()
                session.add_all([
                    ParsedUserSource(
                        parsed_user_id=person.id, source_entity_id=-100701,
                        source_entity_kind="group", source_kind="active",
                        source_task_id=1, message_id=99,
                    ),
                    ParsedUserSource(
                        parsed_user_id=person.id, source_entity_id=-100702,
                        source_entity_kind="group", source_kind="member",
                        source_task_id=other_task.id,
                    ),
                    ParsedChannel(
                        telegram_id=701, username="channel_one", title="=formula",
                        subscribers=25, source_task_id=1,
                    ),
                    ParsedGroup(
                        telegram_id=702, username="group_one", title="Test group",
                        members_count=7, source_task_id=1,
                    ),
                ])

            def csv_rows(kind):
                response = client.get(f"/business/parsing/export/{kind}.csv?task_id=1")
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("text/csv")
                return list(csv.DictReader(io.StringIO(response.text.lstrip("\ufeff"))))

            users = csv_rows("users")
            assert len(users) == 1
            assert users[0]["message_id"] == "99"
            assert users[0]["source_entity_id"] == "'-100701"
            assert users[0]["display_name"] == "'=formula"
            assert len(client.get("/business/parsing/results/users?task_id=1").json()) == 1
            assert len(client.get("/business/parsing/results/users?task_id=2").json()) == 1
            sources = client.get("/business/parsing/results/users/1/sources?task_id=1").json()
            assert [source["message_id"] for source in sources] == [99]
            assert client.get("/business/parsing/export/users.txt?task_id=1").text == "@source_user\n"
            channels = csv_rows("channels")
            assert channels[0]["title"] == "'=formula"
            assert channels[0]["audience_count"] == "25"
            assert csv_rows("groups")[0]["audience_count"] == "7"
            assert client.get("/business/parsing/export/unknown.csv").status_code == 404
    finally:
        engine.dispose()
