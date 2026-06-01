"""Регрессия тестового режима рассылки: каждый аккаунт пишет каждому получателю."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from database.models import (
    Base,
    Client,
    ClientStatus,
    Mailing,
    MailingTestRecipient,
)
from database.repositories import ClientRepository


def _engine():
    return create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def test_get_test_recipients_all_excludes_invalid():
    engine = _engine()
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def run():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with Session() as s:
            m = Mailing(name="t", message_text="hi")
            s.add(m)
            await s.commit()
            await s.refresh(m)
            c1 = Client(username="r1", status=ClientStatus.NEW)
            c2 = Client(username="r2", status=ClientStatus.NEW)
            c3 = Client(username="bad", status=ClientStatus.INVALID)
            s.add_all([c1, c2, c3])
            await s.commit()
            for c in (c1, c2, c3):
                await s.refresh(c)
            s.add_all([
                MailingTestRecipient(mailing_id=m.id, username="r1", client_id=c1.id),
                MailingTestRecipient(mailing_id=m.id, username="r2", client_id=c2.id),
                MailingTestRecipient(mailing_id=m.id, username="bad", client_id=c3.id),
            ])
            await s.commit()

            res = await ClientRepository.get_test_recipients_all(s, m.id)
            usernames = {c.username for c in res}
            # bad (INVALID) исключён, дедупа по «уже отправленным» нет.
            assert usernames == {"r1", "r2"}

    asyncio.run(run())
    asyncio.run(engine.dispose())


def test_run_test_mailing_each_account_writes_each_recipient(monkeypatch):
    import workers.manager as mgr

    class _Scope:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(mgr, "session_scope", lambda: _Scope())

    recipients = [
        SimpleNamespace(id=101, username="lead_a", telegram_user_id=None, status=None),
        SimpleNamespace(id=102, username="lead_b", telegram_user_id=None, status=None),
    ]
    accounts = [
        SimpleNamespace(id=1, username="acc1", phone="+1", first_name="A", last_name=""),
        SimpleNamespace(id=2, username="acc2", phone="+2", first_name="B", last_name=""),
    ]

    async def _recips(session, mailing_id):
        return recipients

    async def _all(session):
        return accounts

    async def _noop(*a, **k):
        return None

    async def _get_or_create(*a, **k):
        return SimpleNamespace(id=1, first_outbound_at=None)

    async def _nosleep(self, seconds):
        return None

    monkeypatch.setattr(mgr.ClientRepository, "get_test_recipients_all", staticmethod(_recips))
    monkeypatch.setattr(mgr.AccountRepository, "get_all", staticmethod(_all))
    monkeypatch.setattr(mgr.MailingLogRepository, "create", staticmethod(_noop))
    monkeypatch.setattr(mgr.ClientRepository, "update_status", staticmethod(_noop))
    monkeypatch.setattr(mgr.ClientRepository, "set_telegram_user_id", staticmethod(_noop))
    monkeypatch.setattr(mgr.ClientMailSessionRepository, "get_or_create", staticmethod(_get_or_create))
    monkeypatch.setattr(mgr.ClientMailSessionRepository, "set_first_outbound", staticmethod(_noop))
    monkeypatch.setattr(mgr.AccountRepository, "increment_stats", staticmethod(_noop))
    monkeypatch.setattr(mgr.MailingRepository, "increment_stats", staticmethod(_noop))
    monkeypatch.setattr(mgr.WorkerManager, "_interruptible_sleep", _nosleep)
    monkeypatch.setattr(mgr.WorkerManager, "apply_template", lambda self, *a, **k: "тест-сообщение")

    class FakeWorker:
        def __init__(self, aid):
            self.account = SimpleNamespace(
                id=aid, username=f"acc{aid}", phone=f"+{aid}", first_name="A", last_name=""
            )
            self.is_connected = True
            self.sends = []

        async def send_message_with_typing(self, *, peer, text, typing_delay=0,
                                           use_typing=False, parse_mode=None,
                                           formatting_entities=None):
            self.sends.append(peer)
            return True, 1, None, 555

    wm = mgr.WorkerManager()
    wm.workers = {1: FakeWorker(1), 2: FakeWorker(2)}

    processed = asyncio.run(
        wm._run_test_mailing(
            1,
            variants=["Привет {username}"],
            variant_mode="random",
            mailing_name="t",
            mailing_link="",
            use_typing=False,
            smart_delay=False,
            delay=0,
            delay_between_accounts=0,
            batch_delay=0,
            group_id=None,
            end_at=None,
        )
    )

    # 2 аккаунта × 2 получателя = 4 отправки, каждый аккаунт написал обоим.
    assert processed == 4
    assert wm.workers[1].sends == ["lead_a", "lead_b"]
    assert wm.workers[2].sends == ["lead_a", "lead_b"]


def test_run_test_mailing_skips_unconnected_accounts(monkeypatch):
    import workers.manager as mgr

    class _Scope:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(mgr, "session_scope", lambda: _Scope())
    recipients = [SimpleNamespace(id=1, username="lead_a", telegram_user_id=None, status=None)]
    accounts = [
        SimpleNamespace(id=1, username="a1", phone="+1", first_name="", last_name=""),
        SimpleNamespace(id=2, username="a2", phone="+2", first_name="", last_name=""),
    ]

    async def _recips(s, m):
        return recipients

    async def _all(s):
        return accounts

    async def _noop(*a, **k):
        return None

    async def _goc(*a, **k):
        return SimpleNamespace(id=1, first_outbound_at=None)

    async def _nosleep(self, seconds):
        return None

    monkeypatch.setattr(mgr.ClientRepository, "get_test_recipients_all", staticmethod(_recips))
    monkeypatch.setattr(mgr.AccountRepository, "get_all", staticmethod(_all))
    monkeypatch.setattr(mgr.MailingLogRepository, "create", staticmethod(_noop))
    monkeypatch.setattr(mgr.ClientRepository, "update_status", staticmethod(_noop))
    monkeypatch.setattr(mgr.ClientRepository, "set_telegram_user_id", staticmethod(_noop))
    monkeypatch.setattr(mgr.ClientMailSessionRepository, "get_or_create", staticmethod(_goc))
    monkeypatch.setattr(mgr.ClientMailSessionRepository, "set_first_outbound", staticmethod(_noop))
    monkeypatch.setattr(mgr.AccountRepository, "increment_stats", staticmethod(_noop))
    monkeypatch.setattr(mgr.MailingRepository, "increment_stats", staticmethod(_noop))
    monkeypatch.setattr(mgr.WorkerManager, "_interruptible_sleep", _nosleep)
    monkeypatch.setattr(mgr.WorkerManager, "apply_template", lambda self, *a, **k: "msg")

    class FakeWorker:
        def __init__(self, aid, connected):
            self.account = SimpleNamespace(id=aid, username=f"a{aid}", phone=f"+{aid}", first_name="", last_name="")
            self.is_connected = connected
            self.sends = []

        async def send_message_with_typing(self, *, peer, **k):
            self.sends.append(peer)
            return True, 1, None, 1

    wm = mgr.WorkerManager()
    # Аккаунт 2 не подключён (например, мёртвый прокси) → пропускается.
    wm.workers = {1: FakeWorker(1, True), 2: FakeWorker(2, False)}

    processed = asyncio.run(
        wm._run_test_mailing(
            1, variants=["x"], variant_mode="random", mailing_name="t", mailing_link="",
            use_typing=False, smart_delay=False, delay=0, delay_between_accounts=0,
            batch_delay=0, group_id=None, end_at=None,
        )
    )
    assert processed == 1
    assert wm.workers[1].sends == ["lead_a"]
    assert wm.workers[2].sends == []


def test_audience_mode_logic_text_distinct():
    from bot.handlers.mailing import _audience_mode_logic_text

    t_test = _audience_mode_logic_text("test", 10)
    t_new = _audience_mode_logic_text("new", 10)
    t_cls = _audience_mode_logic_text("classes", 10)
    assert "каждый" in t_test.lower() and "тест" in t_test.lower()
    assert "new" in t_new.lower()
    assert "класс" in t_cls.lower()
    assert t_test != t_new != t_cls
