"""XLSX parsing exports keep CSV evidence semantics and literal cell data."""
from datetime import datetime, timedelta, timezone
import io
from types import SimpleNamespace
import xml.etree.ElementTree as ET
import zipfile

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from control_plane.business import parsing
from control_plane.business.db import get_bot_db
from control_plane.deps import get_current_user
from database.models import (
    Base, ParsedChannel, ParsedGroup, ParsedUser, ParsedUserSource, ParsingTask,
)


NS = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _cells(response):
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    cells = {}
    for cell in root.findall(".//x:c", NS):
        inline = cell.find("x:is/x:t", NS)
        value = cell.find("x:v", NS)
        cells[cell.attrib["r"]] = (
            cell.attrib.get("t"),
            inline.text if inline is not None else value.text if value is not None else None,
            cell.find("x:f", NS) is not None,
        )
    return cells


def test_aware_datetime_is_written_as_utc_naive():
    class Worksheet:
        def __init__(self):
            self.written = None

        def write_datetime(self, row, col, value, cell_format):
            self.written = (row, col, value, cell_format)

    worksheet = Worksheet()
    aware = datetime(2026, 9, 25, 13, 30, tzinfo=timezone(timedelta(hours=3)))
    parsing._write_xlsx_value(worksheet, 2, 3, "message_at", aware, "date-format")
    assert worksheet.written == (2, 3, datetime(2026, 9, 25, 10, 30), "date-format")


def test_xlsx_export_preserves_filters_evidence_and_literal_text(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'xlsx-export.db'}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(engine, expire_on_commit=False)
    when = datetime(2026, 9, 25, 10, 30)
    with maker.begin() as db:
        db.add_all([
            ParsingTask(kind="users", status="completed"),
            ParsingTask(kind="users", status="completed"),
        ])
        db.flush()
        person = ParsedUser(
            telegram_id=123456789012345678, username="person", display_name="=HYPERLINK(1)",
            source_task_id=2,
        )
        db.add(person)
        db.flush()
        db.add_all([
            ParsedUserSource(
                parsed_user_id=person.id, source_entity_id=-100701,
                source_entity_kind="group", source_kind="active",
                source_task_id=1, message_id=99, message_at=when,
            ),
            ParsedUserSource(
                parsed_user_id=person.id, source_entity_id=-100702,
                source_entity_kind="group", source_kind="member", source_task_id=2,
            ),
            ParsedUser(telegram_id=502, username="legacy", source_task_id=1),
            ParsedChannel(
                telegram_id=701, username="channel_one", title="=SUM(1,1)",
                subscribers=25, source_task_id=1, last_post_at=when,
            ),
            ParsedGroup(
                telegram_id=702, username="group_one", title="+Injected",
                members_count=7, source_task_id=1,
            ),
        ])

    app = FastAPI()
    app.include_router(parsing.router)

    def test_db():
        with maker() as db:
            yield db

    app.dependency_overrides[get_bot_db] = test_db
    try:
        with TestClient(app) as client:
            assert client.get("/business/parsing/export/users.xlsx?task_id=1").status_code == 401
            app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(role="tenant_viewer")

            users = client.get("/business/parsing/export/users.xlsx?task_id=1")
            assert users.status_code == 200
            assert users.headers["content-type"].startswith(parsing._XLSX_MEDIA_TYPE)
            assert 'filename="parsed-users.xlsx"' in users.headers["content-disposition"]
            user_cells = _cells(users)
            assert user_cells["A1"][1] == "telegram_id"
            assert user_cells["A2"] == ("inlineStr", "123456789012345678", False)
            assert user_cells["C2"] == ("inlineStr", "=HYPERLINK(1)", False)
            assert user_cells["I2"] == ("inlineStr", "-100701", False)
            assert user_cells["L2"] == (None, "99", False)
            assert user_cells["N2"][0] is None  # Excel date serial, not a string.
            assert user_cells["P2"] == (None, "1", False)
            assert user_cells["A3"] == ("inlineStr", "502", False)
            assert "A4" not in user_cells  # source from task 2 stays excluded.
            assert "I3" not in user_cells  # legacy user has no evidence row.

            limited = _cells(client.get("/business/parsing/export/users.xlsx?task_id=1&limit=1"))
            assert "A2" in limited and "A3" not in limited

            channels = _cells(client.get("/business/parsing/export/channels.xlsx?task_id=1"))
            assert channels["C2"] == ("inlineStr", "=SUM(1,1)", False)
            assert channels["E2"] == (None, "25", False)
            assert channels["I2"][0] is None
            groups = _cells(client.get("/business/parsing/export/groups.xlsx?task_id=1"))
            assert groups["C2"] == ("inlineStr", "+Injected", False)
            assert groups["E2"] == (None, "7", False)

            assert client.get("/business/parsing/export/unknown.xlsx").status_code == 404
            assert client.get("/business/parsing/export/users.xlsx?limit=0").status_code == 422
            assert client.get("/business/parsing/export/users.xlsx?limit=50001").status_code == 422

            monkeypatch.setattr(parsing, "_XLSX_MAX_BYTES", 100)
            oversized = client.get("/business/parsing/export/users.xlsx?task_id=1")
            assert oversized.status_code == 413
    finally:
        engine.dispose()
