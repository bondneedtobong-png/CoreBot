"""Локальная проверка двух выгрузок клиентской БД и доступа владельца."""
from __future__ import annotations

import asyncio
import io
import zipfile
from contextlib import asynccontextmanager
from types import SimpleNamespace
from xml.etree import ElementTree

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from bot.handlers.database import client_exports, menu
from database.models import Base, Client, ClientClassCounter, ClientStatus


class FakeMessage:
    def __init__(self):
        self.documents = []
        self.edits = []

    async def answer_document(self, document, **kwargs):
        self.documents.append((document, kwargs))

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


class FakeCallback:
    def __init__(self, data, *, user_id=42):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = FakeMessage()
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


class FakeState:
    def __init__(self):
        self.data = {}

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def get_data(self):
        return self.data.copy()

    async def clear(self):
        self.data.clear()


def _workbook_strings(payload: bytes) -> tuple[list[str], bytes]:
    ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        sheet = archive.read("xl/worksheets/sheet1.xml")
        strings = archive.read("xl/sharedStrings.xml")
    root = ElementTree.fromstring(strings)
    values = ["".join(node.itertext()) for node in root.findall("x:si", ns)]
    return values, sheet


def test_client_exports_files_filter_and_owner(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Session = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def scoped():
        async with Session() as session:
            yield session

    monkeypatch.setattr(client_exports, "session_scope", scoped)
    monkeypatch.setattr(client_exports, "is_authorized_user", lambda uid: uid == 42)

    async def run():
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with Session() as session:
            session.add_all([
                Client(username="UserOne", status=ClientStatus.NEW),
                Client(username="@USERONE", status=ClientStatus.CONTACTED),
                Client(username="second_user", status=ClientStatus.CONTACTED),
                Client(username="=1+1", status=ClientStatus.NEW),
                Client(username=None, telegram_user_id=12345, status=ClientStatus.NEW),
            ])
            await session.commit()
            clients = (await session.execute(select(Client).order_by(Client.id))).scalars().all()
            session.add_all([
                ClientClassCounter(client_id=clients[0].id, class_key="alive", count=2),
                ClientClassCounter(client_id=clients[1].id, class_key="alive", count=1),
                ClientClassCounter(client_id=clients[2].id, class_key="=SUM(1,1)", count=4),
                ClientClassCounter(client_id=clients[3].id, class_key="bl", count=3),
                ClientClassCounter(client_id=clients[4].id, class_key="alive", count=1),
            ])
            await session.commit()

        denied = FakeCallback("db_exp_txt", user_id=99)
        await client_exports.db_exp_txt(denied)
        assert denied.message.documents == []
        assert denied.answers[-1][1]["show_alert"] is True

        txt = FakeCallback("db_exp_txt")
        await client_exports.db_exp_txt(txt)
        document, options = txt.message.documents[0]
        assert document.filename == "clients_usernames.txt"
        assert document.data.decode() == "@userone\n@second_user\n"
        assert "2 уникальных" in options["caption"]

        state = FakeState()
        denied_menu = FakeCallback("db_exp_xlsx_menu", user_id=99)
        await client_exports.db_exp_xlsx_menu(denied_menu, state)
        assert state.data == {}
        assert denied_menu.message.edits == []

        menu_cb = FakeCallback("db_exp_xlsx_menu")
        await client_exports.db_exp_xlsx_menu(menu_cb, state)
        assert "хотя бы одного" in menu_cb.message.edits[-1][0]
        keyboard = menu_cb.message.edits[-1][1]["reply_markup"]
        callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
        assert f"db_exp_xlsx_toggle_{client_exports._class_token('alive')}" in callbacks

        selected = FakeCallback(f"db_exp_xlsx_toggle_{client_exports._class_token('alive')}")
        await client_exports.db_exp_xlsx_toggle(selected, state)
        assert state.data["db_exp_selected"] == ["alive"]
        await client_exports.db_exp_xlsx_toggle(
            FakeCallback(f"db_exp_xlsx_toggle_{client_exports._class_token('=SUM(1,1)')}"), state
        )

        denied_export = FakeCallback("db_exp_xlsx_do", user_id=99)
        await client_exports.db_exp_xlsx_do(denied_export, state)
        assert denied_export.message.documents == []

        xlsx = FakeCallback("db_exp_xlsx_do")
        await client_exports.db_exp_xlsx_do(xlsx, state)
        document, options = xlsx.message.documents[0]
        assert document.filename == "clients_by_classes.xlsx"
        assert "4 клиентов" in options["caption"]
        strings, sheet_xml = _workbook_strings(document.data)
        assert "Username" in strings and "alive" in strings and "=SUM(1,1)" in strings
        assert "@userone" in strings and "@second_user" in strings
        assert "@=1+1" not in strings
        assert b"<f>" not in sheet_xml and b"<f " not in sheet_xml
        assert sheet_xml.count(b"<row ") == 5  # заголовок + 4 подходящих клиента

    try:
        asyncio.run(run())
    finally:
        asyncio.run(engine.dispose())


def test_client_export_menus_keep_legacy_paths(monkeypatch):
    monkeypatch.setattr(menu, "is_authorized_user", lambda uid: uid == 42)
    root = menu._database_root_keyboard()
    root_ids = [button.callback_data for row in root.inline_keyboard for button in row]
    assert {"db_sheet_211", "db_exp_menu", "db_sec_sheets", "db_sec_logs"} <= set(root_ids)
    export_ids = [
        button.callback_data for row in menu._database_export_keyboard().inline_keyboard for button in row
    ]
    assert {"db_exp_txt", "db_exp_xlsx_menu", "db_exp_legacy"} <= set(export_ids)

    async def run():
        cb = FakeCallback("db_exp_legacy")
        await menu.db_exp_legacy(cb)
        markup = cb.message.edits[0][1]["reply_markup"]
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
        assert {"db_ex_fresh", "db_ex_bl", "db_ex_alive"} <= set(callbacks)

        denied = FakeCallback("db_exp_legacy", user_id=99)
        await menu.db_exp_legacy(denied)
        assert denied.message.edits == []

    asyncio.run(run())


def test_class_keyboard_pagination_and_literal_labels():
    classes = [(f"class_{index:02d}", index + 1) for index in range(19)]
    first = client_exports._class_keyboard(classes, set(), 0)
    last = client_exports._class_keyboard(classes, {"class_18"}, 2)
    first_ids = [button.callback_data for row in first.inline_keyboard for button in row]
    last_ids = [button.callback_data for row in last.inline_keyboard for button in row]
    assert "db_exp_xlsx_page_1" in first_ids
    assert "db_exp_xlsx_page_1" in last_ids
    assert f"db_exp_xlsx_toggle_{client_exports._class_token('class_18')}" in last_ids
