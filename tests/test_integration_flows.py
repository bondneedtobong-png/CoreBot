"""Task 10: end-to-end integration flows (hermetic, no network).

Covers ONLY the gaps left by the existing integration files (no duplication):
- mailing bot-command queue -> BotCommandConsumer -> background mailing task
  (Telegram boundary replaced with exact fakes; the full chain is asserted:
  pending row -> consumer processes -> worker_manager.start_mailing is
  scheduled as a supervised background task and RUNS -> row marked done);
- mailing.pause signal path (-> worker stop + status flip, no task spawn);
- unknown command -> failed, nothing spawned;
- duplicate TData import is idempotent WITHOUT multipart/HTTP: a direct
  call of _create_account_from_tdata twice against a real tmp SQLite DB
  returns the same account id and leaves a single row.

Already covered elsewhere (referenced here, NOT duplicated):
- health/lifespan: tests/test_lifespan_bootstrap.py, test_health_readiness.py;
- backup/restore test-DB roundtrip: tests/test_backup_restore.py;
- parser claim/concurrency: tests/test_parser_concurrency.py.

Zero real network: messenger/LLM clients are never constructed in
this file (worker_manager is monkeypatched with a fake); the static guard
below fails the suite if a real network client import sneaks in.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

# NOTE: markers are built by concatenation so the literal forbidden strings
# never appear contiguously in THIS file — otherwise the guard below would
# fail on its own definition (self-trigger, fixed in task 10).
FORBIDDEN_NETWORK_MARKERS = (
    "Telegram" + "Client",
    "tele" + "thon",
    "aio" + "gram",
    "open" + "router",
    "Aiohttp" + "Session",
    "Client" + "Session(",
    "url" + "open",
)


def test_integration_file_uses_no_real_network_clients():
    src = Path(__file__).read_text(encoding="utf-8")
    lowered = src.lower()
    for marker in FORBIDDEN_NETWORK_MARKERS:
        assert marker.lower() not in lowered, f"network marker leaked: {marker}"


class _FakeSupervisor:
    """Exact-boundary fake for utils.background_tasks.BackgroundTaskSupervisor."""

    def __init__(self):
        self.spawned: list[dict] = []

    def create(self, coro, *, name=None, **kwargs):
        task = asyncio.ensure_future(coro)
        self.spawned.append({"name": name, "task": task})
        return task


class _Scope:
    """Fake session_scope() yielding a canned session object."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


def _patch_db_boundary(monkeypatch, consumer, session):
    """Redirect the consumer's DB boundary to fakes; return (executed, committed)."""
    executed: list = []
    committed: list = []

    async def _fake_execute(sess, stmt, *, op_name=None, **kwargs):
        executed.append(stmt)
        return SimpleNamespace()

    async def _fake_commit(sess, *, op_name=None, **kwargs):
        committed.append(op_name)
        return None

    monkeypatch.setattr(consumer, "session_scope", lambda: _Scope(session))
    monkeypatch.setattr(consumer, "execute_with_busy_retry", _fake_execute)
    monkeypatch.setattr(consumer, "commit_with_busy_retry", _fake_commit)
    return executed, committed


def _done_params(executed):
    """Extract UPDATE params of the first bot_commands status write."""
    assert executed, "consumer performed no DB writes"
    params = executed[0].compile().params
    assert params.get("status") in ("done", "failed")
    return params


def test_mailing_start_command_flows_to_background_task(monkeypatch):
    """Queue row mailing.start -> consumer -> supervised task RUNS -> done."""
    import workers.bot_command_consumer as consumer
    import workers.manager as manager

    started: list[int] = []

    async def _fake_mailing_coro(mailing_id: int) -> str:
        started.append(mailing_id)
        return f"mailing-{mailing_id}-done"

    class _FakeWorkerManager:
        def start_mailing(self, mailing_id: int):
            return _fake_mailing_coro(int(mailing_id))

    supervisor = _FakeSupervisor()
    monkeypatch.setattr(manager, "worker_manager", _FakeWorkerManager())
    monkeypatch.setattr(consumer, "background_tasks", supervisor)
    executed, _committed = _patch_db_boundary(monkeypatch, consumer, object())

    from database.models import BotCommand

    row = BotCommand(command="mailing.start", args_json='{"mailing_id": 7}')
    row.id = 42

    async def _run():
        await consumer.BotCommandConsumer()._process_one(row)
        assert len(supervisor.spawned) == 1
        spawned = supervisor.spawned[0]
        assert spawned["name"] == "mailing-7"
        # The task must not only be scheduled — it must RUN to completion.
        result = await asyncio.wait_for(spawned["task"], timeout=5)
        assert result == "mailing-7-done"

    asyncio.run(_run())

    assert started == [7]
    assert _done_params(executed)["status"] == "done"


def test_mailing_pause_signals_stop_without_spawning_task(monkeypatch):
    """Queue row mailing.pause -> worker stop + PAUSED flip -> done, no spawn."""
    import workers.bot_command_consumer as consumer
    import workers.manager as manager

    from database.models import BotCommand, MailingStatus

    stops: list[bool] = []

    class _FakeWorkerManager:
        def stop_mailing(self):
            stops.append(True)

    class _PauseSession:
        def __init__(self):
            self.mailing = SimpleNamespace(id=9, status=MailingStatus.RUNNING)

        async def get(self, model, pk):
            assert int(pk) == 9
            return self.mailing

    supervisor = _FakeSupervisor()
    monkeypatch.setattr(manager, "worker_manager", _FakeWorkerManager())
    monkeypatch.setattr(consumer, "background_tasks", supervisor)
    session = _PauseSession()
    executed, _committed = _patch_db_boundary(monkeypatch, consumer, session)

    row = BotCommand(command="mailing.pause", args_json='{"mailing_id": 9}')
    row.id = 43

    asyncio.run(consumer.BotCommandConsumer()._process_one(row))

    assert stops == [True]
    assert supervisor.spawned == []
    assert session.mailing.status is MailingStatus.PAUSED
    assert _done_params(executed)["status"] == "done"


def test_unknown_command_fails_without_spawning_task(monkeypatch):
    """Unknown queue command -> failed with reason, nothing spawned."""
    import workers.bot_command_consumer as consumer
    import workers.manager as manager

    from database.models import BotCommand

    supervisor = _FakeSupervisor()
    monkeypatch.setattr(manager, "worker_manager", SimpleNamespace())
    monkeypatch.setattr(consumer, "background_tasks", supervisor)
    executed, _committed = _patch_db_boundary(monkeypatch, consumer, object())

    row = BotCommand(command="nope.bogus", args_json="{}")
    row.id = 44

    asyncio.run(consumer.BotCommandConsumer()._process_one(row))

    assert supervisor.spawned == []
    params = _done_params(executed)
    assert params["status"] == "failed"
    assert "unknown command" in str(params.get("error", ""))


def test_duplicate_tdata_import_is_idempotent_without_http(tmp_path):
    """Same session_name twice -> same id, single row (no multipart/HTTP path).

    Direct repository-level call against a real tmp SQLite DB: the HTTP
    form-parsing layer (UploadFile/multipart) is deliberately NOT exercised
    here — it is already covered by tests/test_tdata_web_import.py.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from control_plane.business.tdata_routes import _create_account_from_tdata
    from database.models import Account, Base

    engine = create_engine(f"sqlite:///{tmp_path / 'tdata-dup.db'}", future=True)
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            first = _create_account_from_tdata(
                db,
                {
                    "session_name": "sess_dup",
                    "phone": "+1000000001",
                    "username": "user1",
                    "first_name": "Test",
                    "last_name": "User",
                },
            )
            # Re-import of the same TData (converter may report different
            # phone/username casing) must NOT create a second row.
            second = _create_account_from_tdata(
                db,
                {
                    "session_name": "sess_dup",
                    "phone": "+1999999999",
                    "username": "user1_changed",
                    "first_name": "Test",
                    "last_name": "User",
                },
            )
            assert int(second) == int(first)
            count = db.query(Account).filter(Account.session_name == "sess_dup").count()
            assert count == 1
    finally:
        engine.dispose()
