from types import SimpleNamespace
import re
import asyncio
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from bot.handlers.accounts.groups import _render_template
from bot.handlers.mailing import _mailing_list_page_from_data
from control_plane.business.mailings import patch_mailing
from control_plane.business.schemas import MailingPatch
from control_plane.routes.business import retry_queue_item
from control_plane.business.schemas import SendMessageOut
from services.neurochat.engagement_service import build_alive_window_key
from services.neurochat import manager as neuro_manager
from services.neurochat import post_actions as neuro_post_actions
from services.neurochat import class_bridge as neuro_class_bridge
from services.neurochat import engagement_service as neuro_engagement
from services.neurochat import filters as neuro_filters
from workers.manager import WorkerManager
from utils.neuro_sampling import (
    merge_sampling_for_request,
    parse_sampling_user_input,
)
from utils.neuro_prompts import apply_neuro_prompt_placeholders


def test_mailing_list_page_from_data():
    assert _mailing_list_page_from_data("mailing_list") == 0
    assert _mailing_list_page_from_data("mailing_list_p_2") == 2
    assert _mailing_list_page_from_data("other") == 0


def test_render_template_with_known_fields():
    account = SimpleNamespace(
        id=7,
        phone="+1234567890",
        username="john_doe",
        first_name="John",
        last_name="Doe",
    )
    out = _render_template(
        "acc_{i}_{group}_{username}_{first_name}_{last_name}_{phone}_{id}",
        account,
        4,
        "COL",
    )
    assert out == "acc_4_COL_john_doe_John_Doe_+1234567890_7"


def test_render_template_unknown_placeholder_becomes_empty():
    account = SimpleNamespace(
        id=1,
        phone="100",
        username="u",
        first_name="A",
        last_name="B",
    )
    out = _render_template("x_{unknown}_y", account, 1, "G")
    assert out == "x__y"


def test_parse_sampling_user_input_key_value():
    d = parse_sampling_user_input("temperature=0.5\ntop_p=0.9")
    assert abs(d["temperature"] - 0.5) < 1e-9
    assert abs(d["top_p"] - 0.9) < 1e-9


def test_merge_sampling_defaults_and_override():
    m = merge_sampling_for_request({"top_p": 0.88})
    assert m["max_tokens"] == 0
    assert abs(m["temperature"] - 0.7) < 1e-9
    assert abs(m["top_p"] - 0.88) < 1e-9
    assert m["top_k"] == 40


def test_neuro_prompt_placeholders_account_and_peer():
    acc = SimpleNamespace(
        id=3,
        phone="+100",
        username="mgr",
        first_name="Иван",
        last_name="Петров",
    )
    mailing = SimpleNamespace(id=9, name="Кампания A")
    client = SimpleNamespace(username="lead_user")
    peer = SimpleNamespace(first_name="Анна", last_name="Ли", username="anna")
    raw = "Менеджер {first_name} {last_name} {username} id={account_id} | {mailing} #{mailing_id} | {link} | peer {peer_first_name} {peer_username}"
    out = apply_neuro_prompt_placeholders(
        raw,
        link="https://x.example/join",
        account=acc,
        mailing=mailing,
        client=client,
        peer_sender=peer,
    )
    assert "Иван" in out and "Петров" in out
    assert "@mgr" in out
    assert "id=3" in out
    assert "Кампания A" in out and "#9" in out
    assert "https://x.example/join" in out
    assert "Анна" in out and "@anna" in out


def test_apply_template_timezone_placeholders():
    wm = WorkerManager()
    out = wm.apply_template(
        "{username} {timezone} {time+3} {datetime-2} {date+1} {timezone-1} {fullname}",
        "demo_user",
    )
    assert "@demo_user" in out
    assert re.search(r"UTC[+-]\d{2}:00", out)
    assert "{time+3}" not in out
    assert "{datetime-2}" not in out
    assert "{date+1}" not in out
    assert "{timezone-1}" not in out


def test_check_incoming_allowed_reason_priority(monkeypatch):
    async def _can_true(_session):
        return True

    async def _can_false(_session):
        return False

    async def _filters_ok(_session, _client_id):
        return True, "ok"

    async def _filters_block(_session, _client_id):
        return False, "client_class_bl"

    mailing_on = SimpleNamespace(neurochat_enabled=True)
    mailing_off = SimpleNamespace(neurochat_enabled=False)

    monkeypatch.setattr(neuro_manager, "can_process_incoming", _can_false)
    monkeypatch.setattr(neuro_manager, "check_client_filters", _filters_ok)
    ok, reason = asyncio.run(
        neuro_manager.check_incoming_allowed(
            session=None, mailing=mailing_on, worker_connected=True, client_id=10
        )
    )
    assert not ok and reason == "global_disabled"

    monkeypatch.setattr(neuro_manager, "can_process_incoming", _can_true)
    ok, reason = asyncio.run(
        neuro_manager.check_incoming_allowed(
            session=None, mailing=mailing_off, worker_connected=True, client_id=10
        )
    )
    assert not ok and reason == "mailing_local_disabled"

    monkeypatch.setattr(neuro_manager, "check_client_filters", _filters_block)
    ok, reason = asyncio.run(
        neuro_manager.check_incoming_allowed(
            session=None, mailing=mailing_on, worker_connected=True, client_id=10
        )
    )
    assert not ok and reason == "client_class_bl"


def test_send_text_reply_success_and_failure(monkeypatch):
    async def _emit_event(*_args, **_kwargs):
        return None

    monkeypatch.setattr(neuro_post_actions.telemetry_emitter, "emit_event", _emit_event)
    monkeypatch.setattr(neuro_post_actions.random, "uniform", lambda _a, _b: 0.0)

    class OkWorker:
        async def send_message_with_typing(self, *_args, **_kwargs):
            return True, 1, None, None

    class FailWorker:
        async def send_message_with_typing(self, *_args, **_kwargs):
            return False, None, "send failed", None

    ok = asyncio.run(
        neuro_post_actions.send_text_reply(
            OkWorker(),
            peer_uid=123,
            reply="hello",
            use_typing_neuro=False,
            account_id=1,
            client_id=2,
        )
    )
    assert ok is True

    ok = asyncio.run(
        neuro_post_actions.send_text_reply(
            FailWorker(),
            peer_uid=123,
            reply="hello",
            use_typing_neuro=False,
            account_id=1,
            client_id=2,
        )
    )
    assert ok is False


def test_alive_window_key_hour_bucket():
    dt = datetime(2026, 4, 21, 10, 59, 59, tzinfo=timezone.utc)
    dt_next = datetime(2026, 4, 21, 11, 0, 1, tzinfo=timezone.utc)
    k1 = build_alive_window_key(dt)
    k2 = build_alive_window_key(dt_next)
    assert isinstance(k1, int) and isinstance(k2, int)
    assert k2 == k1 + 1


def test_apply_neuro_class_commands_accept_decline_hater(monkeypatch):
    calls = []

    async def _increment(_session, _mailing, client_id, class_key, delta=1):
        calls.append(("inc", client_id, class_key, delta))

    async def _noop(*_args, **_kwargs):
        return None

    class _Mailing:
        id = 77

    class _Scope:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(neuro_class_bridge, "increment_client_class_for_mailing", _increment)
    monkeypatch.setattr(
        neuro_class_bridge.MailingRepository,
        "get_by_id",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=_Mailing()),
    )
    monkeypatch.setattr(
        neuro_class_bridge.ClientMailSessionRepository,
        "get_or_create",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=SimpleNamespace(id=123)),
    )
    monkeypatch.setattr(neuro_class_bridge.ClientMailSessionRepository, "set_success_end", _noop)
    monkeypatch.setattr(neuro_class_bridge.NeuroActionRepository, "create", _noop)
    monkeypatch.setattr(neuro_class_bridge.ClientInteractionRepository, "add", _noop)
    monkeypatch.setattr(neuro_class_bridge, "session_scope", lambda: _Scope())

    def _fake_schedule(_mail_session_id):
        return None

    import services.database.accept_transcript as accept_transcript

    monkeypatch.setattr(accept_transcript, "schedule_fetch_accept_transcript", _fake_schedule)

    asyncio.run(
        neuro_class_bridge.apply_neuro_class_commands(
            account_id=1,
            client_id=10,
            mailing_id=77,
            cmd_accept=True,
            cmd_decline=True,
            cmd_hater=True,
        )
    )
    assert ("inc", 10, "accept", 1) in calls
    assert ("inc", 10, "decline", 1) in calls
    assert ("inc", 10, "hater", 1) in calls


def test_process_stop_command_increments_stop(monkeypatch):
    calls = []

    async def _increment(_session, _mailing, client_id, class_key, delta=1):
        calls.append((client_id, class_key, delta))

    async def _noop(*_args, **_kwargs):
        return None

    class _Mailing:
        id = 50

    class _Scope:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(neuro_post_actions, "increment_client_class_for_mailing", _increment)
    monkeypatch.setattr(
        neuro_post_actions.MailingRepository,
        "get_by_id",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=_Mailing()),
    )
    monkeypatch.setattr(neuro_post_actions.NeuroActionRepository, "create", _noop)
    monkeypatch.setattr(neuro_post_actions.ClientInteractionRepository, "add", _noop)
    monkeypatch.setattr(neuro_post_actions, "session_scope", lambda: _Scope())

    asyncio.run(
        neuro_post_actions.process_stop_command(
            cmd_stop=True,
            mailing_id=50,
            account_id=2,
            client_id=20,
        )
    )
    assert (20, "stop", 1) in calls


def test_track_incoming_engagement_pulse_and_alive_dedup(monkeypatch):
    class _Mailing:
        id = 88

    increments = []
    interactions = []
    alive_created = {"value": True}

    async def _increment(_session, _mailing, client_id, class_key, delta=1):
        increments.append((client_id, class_key, delta))

    async def _add_interaction(*_args, **kwargs):
        interactions.append(kwargs.get("kind"))
        return None

    async def _create_if_absent(*_args, **_kwargs):
        return alive_created["value"]

    monkeypatch.setattr(neuro_engagement, "increment_client_class_for_mailing", _increment)
    monkeypatch.setattr(neuro_engagement.ClientInteractionRepository, "add", _add_interaction)
    monkeypatch.setattr(
        neuro_engagement.ClientAliveWindowRepository, "create_if_absent", _create_if_absent
    )

    asyncio.run(
        neuro_engagement.track_incoming_engagement(
            session=object(),
            mailing=_Mailing(),
            account_id=3,
            client_id=30,
            body="hello",
            telegram_message_id=123,
        )
    )
    assert (30, "pulse", 1) in increments
    assert (30, "alive", 1) in increments
    assert "pulse" in interactions and "alive" in interactions

    alive_created["value"] = False
    interactions.clear()
    increments.clear()
    asyncio.run(
        neuro_engagement.track_incoming_engagement(
            session=object(),
            mailing=_Mailing(),
            account_id=3,
            client_id=30,
            body="hello again",
            telegram_message_id=124,
        )
    )
    assert (30, "pulse", 1) in increments
    assert (30, "alive", 1) not in increments
    assert "pulse" in interactions and "alive" not in interactions


def test_check_client_filters_blocks_bl(monkeypatch):
    async def _counts(_session, _client_id):
        return {"bl": 1, "stop": 1}

    monkeypatch.setattr(
        neuro_filters.ClientClassCounterRepository, "get_counts", _counts
    )
    ok, reason = asyncio.run(neuro_filters.check_client_filters(session=None, client_id=44))
    assert ok is False
    assert reason == "client_class_bl"


def test_prepare_incoming_context_denies_on_stop_class(monkeypatch):
    async def _allowed(*_args, **_kwargs):
        return True, "ok"

    async def _key(*_args, **_kwargs):
        return "key"

    async def _has_stop(*_args, **_kwargs):
        return True

    monkeypatch.setattr(neuro_manager, "check_incoming_allowed", _allowed)
    monkeypatch.setattr(
        neuro_manager.InstanceSettingsRepository,
        "get_effective_openrouter_key",
        _key,
    )
    monkeypatch.setattr(neuro_manager, "client_has_positive_class", _has_stop)

    ctx, reason = asyncio.run(
        neuro_manager.prepare_incoming_context(
            session=object(),
            worker=SimpleNamespace(is_connected=True, account=SimpleNamespace(id=1)),
            sender=None,
            client=SimpleNamespace(id=2, telegram_user_id=100),
            mailing=SimpleNamespace(
                id=3,
                neurochat_enabled=True,
                neuro_model="",
                community_link="",
                neuro_sampling_json="{}",
                use_typing=True,
            ),
            text="hi",
            peer_uid=100,
        )
    )
    assert ctx is None
    assert reason == "client_class_stop"


def test_patch_mailing_locked_when_running():
    class _Mailing:
        status = "RUNNING"

    class _DB:
        def get(self, _model, _id):
            return _Mailing()

    with pytest.raises(HTTPException) as ex:
        patch_mailing(
            mailing_id=1,
            payload=MailingPatch(name="new name"),
            db=_DB(),
            _user=None,
        )
    assert ex.value.status_code == 400
    assert "RUNNING" in str(ex.value.detail)


def test_retry_queue_item_resets_failed_to_pending():
    class _Row:
        id = 42
        status = "failed"
        error = "x"
        attempts = 3
        next_attempt_at = datetime.now(timezone.utc)
        sent_at = datetime.now(timezone.utc)
        created_at = datetime.now(timezone.utc)

    class _DB:
        def __init__(self):
            self.row = _Row()

        def get(self, _model, queue_id):
            return self.row if queue_id == 42 else None

        def execute(self, _stmt):
            # Имитируем SQL UPDATE из retry_queue_item.
            self.row.status = "pending"
            self.row.error = None
            self.row.attempts = 0
            self.row.next_attempt_at = None
            self.row.sent_at = None
            return None

        def commit(self):
            return None

        def refresh(self, _row):
            return None

    out = retry_queue_item(
        queue_id=42,
        db=_DB(),
        _user=None,
    )
    assert isinstance(out, SendMessageOut)
    assert out.queue_id == 42
    assert out.status == "pending"
