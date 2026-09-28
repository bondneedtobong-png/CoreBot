"""Profile pool categories keep imported and randomly applied identities separate."""

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.handlers.accounts import profile_templates as handler
from database.models import Account, Base, Group, ProfilePoolItem
from database.profile_templates import add_pool_batch, list_pool_items, pool_counts, random_profile
from database.repository import Database


def test_borrowed_profile_serializes_send_and_disconnect(tmp_path, monkeypatch):
    from workers import manager

    async def scenario():
        account = SimpleNamespace(id=7, proxy=object(), session_name="seven")
        calls = []
        photo_started = asyncio.Event()
        finish_photo = asyncio.Event()
        send_lock = asyncio.Lock()
        connection_lock = asyncio.Lock()

        class Client:
            async def __call__(self, request):
                assert send_lock.locked() and connection_lock.locked()
                calls.append("text")

        class Active:
            is_connected = True
            client = Client()
            _send_lock = send_lock
            _connection_lock = connection_lock

            async def set_profile_photo(self, path):
                assert path == str(tmp_path / "avatar.jpg")
                assert send_lock.locked() and connection_lock.locked()
                photo_started.set()
                await finish_photo.wait()
                calls.append("photo")
                return True

        @asynccontextmanager
        async def scope():
            yield object()

        async def get_account(_session, _id):
            return account

        async def execute(_session, _statement, *, op_name):
            calls.append(op_name)

        async def commit(_session, *, op_name):
            pass

        monkeypatch.setattr(handler, "session_scope", scope)
        monkeypatch.setattr(handler.AccountRepository, "get_by_id", get_account)
        monkeypatch.setattr(handler, "execute_with_busy_retry", execute)
        monkeypatch.setattr(handler, "commit_with_busy_retry", commit)
        monkeypatch.setattr(handler, "_asset_path", lambda _file: tmp_path / "avatar.jpg")
        monkeypatch.setattr(manager.worker_manager, "is_mailing_busy", lambda: False)
        monkeypatch.setattr(manager, "account_worker_for_action", lambda *_args: manager._BorrowedWorker(Active()))

        async with send_lock:
            applying = asyncio.create_task(handler._apply_profile(7, name="Имя", bio=None, photo_file="avatar.jpg"))
            await asyncio.sleep(0.01)
            assert calls == []

        await asyncio.wait_for(photo_started.wait(), 1)
        async def send():
            async with send_lock:
                calls.append("send")

        async def disconnect():
            async with connection_lock:
                calls.append("disconnect")

        sending = asyncio.create_task(send())
        disconnecting = asyncio.create_task(disconnect())
        await asyncio.sleep(0.01)
        assert not sending.done() and not disconnecting.done()
        finish_photo.set()
        assert await asyncio.wait_for(applying, 1) == (True, "Профиль применён")
        await asyncio.gather(sending, disconnecting)
        assert calls[:4] == ["text", "profile-random-text", "photo", "profile-random-photo"]
        assert not send_lock.locked() and not connection_lock.locked()

    asyncio.run(scenario())


def test_profile_photo_rejection_keeps_text_result_and_releases_borrowed_locks(tmp_path, monkeypatch):
    from workers import manager

    async def scenario():
        account = SimpleNamespace(id=8, proxy=object(), session_name="eight")
        calls = []
        active = SimpleNamespace(
            is_connected=True,
            _send_lock=asyncio.Lock(),
            _connection_lock=asyncio.Lock(),
        )

        async def update_text(_request):
            calls.append("text")

        async def reject_photo(_path):
            calls.append("photo-rejected")
            return False

        @asynccontextmanager
        async def scope():
            yield object()

        async def get_account(_session, _id):
            return account

        async def execute(_session, _statement, *, op_name):
            calls.append(op_name)

        async def commit(_session, *, op_name):
            pass

        active.client = update_text
        active.set_profile_photo = reject_photo
        monkeypatch.setattr(handler, "session_scope", scope)
        monkeypatch.setattr(handler.AccountRepository, "get_by_id", get_account)
        monkeypatch.setattr(handler, "execute_with_busy_retry", execute)
        monkeypatch.setattr(handler, "commit_with_busy_retry", commit)
        monkeypatch.setattr(handler, "_asset_path", lambda _file: tmp_path / "avatar.jpg")
        monkeypatch.setattr(manager.worker_manager, "is_mailing_busy", lambda: False)
        monkeypatch.setattr(manager, "account_worker_for_action", lambda *_args: manager._BorrowedWorker(active))

        result = await handler._apply_profile(8, name="Имя", bio=None, photo_file="avatar.jpg")
        assert result == (False, "Текст профиля сохранён, фото Telegram отклонил")
        assert calls == ["text", "profile-random-text", "photo-rejected"]
        assert not active._send_lock.locked() and not active._connection_lock.locked()

        async def fail_text(_request):
            raise RuntimeError("telegram failed")

        active.client = fail_text
        result = await handler._apply_profile(8, name="Имя", bio=None, photo_file="avatar.jpg")
        assert result == (False, "Ошибка Telegram: RuntimeError")
        assert not active._send_lock.locked() and not active._connection_lock.locked()

    asyncio.run(scenario())


def test_profile_fresh_worker_connects_without_outer_connection_lock(monkeypatch):
    from workers import manager

    async def scenario():
        account = SimpleNamespace(id=9, proxy=object(), session_name="nine")

        class FreshWorker:
            def __init__(self):
                self._connection_lock = asyncio.Lock()
                self.client = None
                self.disconnected = False

            async def connect(self, *, quiet):
                async with self._connection_lock:
                    self.client = self.update_text
                    return True

            async def update_text(self, _request):
                pass

            async def disconnect(self):
                async with self._connection_lock:
                    self.disconnected = True

        worker = FreshWorker()

        @asynccontextmanager
        async def scope():
            yield object()

        async def get_account(_session, _id):
            return account

        async def persist(_session, _statement=None, *, op_name):
            pass

        monkeypatch.setattr(handler, "session_scope", scope)
        monkeypatch.setattr(handler.AccountRepository, "get_by_id", get_account)
        monkeypatch.setattr(handler, "execute_with_busy_retry", persist)
        monkeypatch.setattr(handler, "commit_with_busy_retry", persist)
        monkeypatch.setattr(manager.worker_manager, "is_mailing_busy", lambda: False)
        monkeypatch.setattr(manager, "account_worker_for_action", lambda *_args: worker)

        assert await asyncio.wait_for(
            handler._apply_profile(9, name="Имя", bio=None, photo_file=None), 1
        ) == (True, "Профиль применён")
        assert worker.disconnected

    asyncio.run(scenario())


class DummyState:
    def __init__(self):
        self.data = {}
        self.state = None

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **values):
        self.data.update(values)

    async def set_state(self, state):
        self.state = state

    async def clear(self):
        self.data.clear()
        self.state = None


class DummyMessage:
    def __init__(self, *, text=None, photo=None, document=None, bot=None):
        self.text = text
        self.photo = photo or []
        self.document = document
        self.bot = bot
        self.from_user = SimpleNamespace(id=1)
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))

    async def answer_photo(self, photo, **kwargs):
        self.answers.append(("photo", kwargs))


class DummyCallback:
    def __init__(self, data, bot=None):
        self.data = data
        self.bot = bot
        self.message = DummyMessage()
        self.from_user = SimpleNamespace(id=1)
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


def test_old_zip_pool_rows_migrate_as_unisex(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE profile_pool_items ("
            "id INTEGER PRIMARY KEY, kind VARCHAR(10) NOT NULL, value TEXT NOT NULL, created_at DATETIME)"
        )
        conn.execute("INSERT INTO profile_pool_items(id, kind, value) VALUES (1, 'name', 'СтароеИмя')")

    async def scenario():
        db = Database(f"sqlite+aiosqlite:///{path}")
        await db.connect()
        await db.disconnect()
        db = Database(f"sqlite+aiosqlite:///{path}")
        await db.connect()  # repeat is idempotent
        await db.disconnect()

    asyncio.run(scenario())
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT category FROM profile_pool_items WHERE id=1"
        ).fetchone()[0] == "u"
        assert conn.execute(
            "SELECT value FROM profile_pool_items_category_backup WHERE id=1"
        ).fetchone()[0] == "СтароеИмя"


def test_profile_selection_does_not_mix_categories(tmp_path):
    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pool.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with maker() as session:
                await add_pool_batch(session, {
                    "name": ["Макс"], "bio": ["Мужское био"], "photo": ["m.jpg"],
                }, category="m")
                assert await add_pool_batch(session, {
                    "name": ["Макс", "Макс"], "bio": ["Мужское био"], "photo": ["m.jpg"],
                }, category="m") == {"name": 0, "bio": 0, "photo": 0}
                await add_pool_batch(session, {
                    "name": ["Анна"], "bio": ["Женское био"], "photo": ["f.jpg"],
                }, category="f")
                await add_pool_batch(session, {
                    "name": ["Alex"], "bio": ["Универсальное био"], "photo": ["u.jpg"],
                })
                assert await random_profile(session, "m") == {
                    "name": "Макс", "bio": "Мужское био", "photo": "m.jpg",
                }
                assert await random_profile(session, "f") == {
                    "name": "Анна", "bio": "Женское био", "photo": "f.jpg",
                }
                assert await random_profile(session) == {
                    "name": "Alex", "bio": "Универсальное био", "photo": "u.jpg",
                }
                assert await pool_counts(session, "f") == {"name": 1, "bio": 1, "photo": 1}
                rows, total = await list_pool_items(session, "name", "m")
                assert total == 1 and rows[0].value == "Макс"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_text_and_photo_upload_then_category_pick(tmp_path, monkeypatch):
    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'upload.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)

        @asynccontextmanager
        async def scope():
            async with maker() as session:
                yield session

        async def fake_edit(message, text, **kwargs):
            message.answers.append((text, kwargs))

        class Bot:
            async def download(self, file_id, *, destination):
                assert file_id == "telegram-photo"
                destination.write_bytes(b"\xff\xd8\xff" + b"image" * 10)

        monkeypatch.setattr(handler, "session_scope", scope)
        monkeypatch.setattr(handler, "safe_edit_message", fake_edit)
        monkeypatch.setattr(handler, "is_authorized_user", lambda _id: True)
        monkeypatch.setattr(handler, "DATA_DIR", tmp_path)
        monkeypatch.setattr(handler, "ASSETS_DIR", tmp_path / "assets")
        try:
            state = DummyState()
            await state.update_data(pool_kind="name")
            await handler.receive_pool_text(DummyMessage(text="Анна;Мария"), state)
            assert state.data["pool_values"] == ["Анна", "Мария"]
            callback = DummyCallback("profile_pool_category_f")
            await handler.save_pool_category(callback, state)
            assert "Добавлено: 2" in callback.message.answers[-1][0]
            assert state.data == {}

            state = DummyState()
            await state.update_data(pool_kind="photo")
            bot = Bot()
            photo = SimpleNamespace(file_id="telegram-photo", file_size=100)
            await handler.receive_pool_photo(DummyMessage(photo=[photo], bot=bot), state)
            callback = DummyCallback("profile_pool_category_f", bot=bot)
            await handler.save_pool_category(callback, state)
            assert "Добавлено: 1" in callback.message.answers[-1][0]

            async with maker() as session:
                rows = list((await session.execute(select(ProfilePoolItem))).scalars().all())
                assert len(rows) == 3
                assert {row.category for row in rows} == {"f"}
                assert (tmp_path / "assets" / rows[-1].value).is_file()

            picked = []

            async def fake_apply(account_id, *, name, bio, photo_file):
                picked.append((account_id, name, bio, photo_file))
                return True, "Профиль применён"

            monkeypatch.setattr(handler, "_apply_profile", fake_apply)
            callback = DummyCallback("profile_pick_account_f_7")
            await handler.apply_random_category(callback)
            assert picked[0][0:2] == (7, "Анна") or picked[0][0:2] == (7, "Мария")
            assert picked[0][3] == rows[-1].value
            assert any("Имя:" in item[0] for item in callback.message.answers)
            assert any(item[0] == "photo" for item in callback.message.answers)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_semicolon_input_rejects_mixed_or_invalid_values():
    assert handler._parse_semicolon_values("Ира;Мира;Ира", "name") == ["Ира", "Мира"]
    for text in ("Имя Фамилия;Ира", "Ира;;Мира", "Ира\nМира"):
        try:
            handler._parse_semicolon_values(text, "name")
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted invalid names: {text!r}")


def test_group_randomization_requires_confirmation_and_reports_only_counts(tmp_path, monkeypatch):
    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'group.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)

        @asynccontextmanager
        async def scope():
            async with maker() as session:
                yield session

        async def fake_edit(message, text, **kwargs):
            message.answers.append((text, kwargs))

        monkeypatch.setattr(handler, "session_scope", scope)
        monkeypatch.setattr(handler, "safe_edit_message", fake_edit)
        monkeypatch.setattr(handler, "is_authorized_user", lambda _id: True)
        monkeypatch.setattr(handler, "GROUP_RANDOM_DELAY_SECONDS", 0)
        try:
            async with maker() as session:
                group = Group(name="Тестовая группа")
                group.accounts.extend([
                    Account(phone="+10000000001", session_name="one"),
                    Account(phone="+10000000002", session_name="two"),
                ])
                session.add(group)
                await session.commit()
                group_id = group.id
                await add_pool_batch(session, {"name": ["Анна"]}, category="f")

            confirm = DummyCallback(f"profile_group_confirm_f_{group_id}")
            await handler.confirm_random_group(confirm)
            assert "Для каждого из 2 аккаунтов" in confirm.message.answers[-1][0]
            markup = confirm.message.answers[-1][1]["reply_markup"]
            assert markup.inline_keyboard[0][0].callback_data == f"profile_group_run_f_{group_id}"

            calls = []

            async def fake_apply(account_id, *, name, bio, photo_file):
                calls.append((account_id, name))
                return (account_id == 1), "unused detail"

            monkeypatch.setattr(handler, "_apply_profile", fake_apply)
            run = DummyCallback(f"profile_group_run_f_{group_id}")
            await handler.run_random_group(run)
            assert calls == [(1, "Анна"), (2, "Анна")]
            final = run.message.answers[-1][0]
            assert "успешно 1 из 2, ошибок 1" in final
            assert "unused detail" not in final
        finally:
            await engine.dispose()

    asyncio.run(scenario())
