"""Engagement drafts require a verified, one-use Telegram preview."""
import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

import control_plane.business.engagement as api
import workers.bot_command_consumer as consumer
from control_plane.business.db import get_bot_db
from control_plane.deps import get_current_user, require_operator_write
from database.models import Account, AccountStatus, Base, BotCommand, EngagementDraft
from services.neurochat.provider_registry import ProviderRuntime


def test_verified_preview_review_send_and_recovery(tmp_path, monkeypatch):
    path = tmp_path / "engagement.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(engine, expire_on_commit=False)
    with maker() as db:
        db.add(Account(phone="+15551112222", session_name="engagement-account",
                       status=AccountStatus.ACTIVE, daily_limit=10))
        db.commit()
    app = FastAPI()
    app.include_router(api.router)

    def test_db():
        with maker() as db:
            yield db

    user = SimpleNamespace(username="reviewer", role="tenant_admin")
    app.dependency_overrides[get_bot_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[require_operator_write] = lambda: user
    monkeypatch.setattr(api, "resolve_default_provider_sync", lambda _db: ProviderRuntime(
        "OpenRouter", "https://openrouter.ai/api/v1", "test/model", "test-key", is_openrouter=True,
    ))

    async def generate(*_args, **_kwargs):
        return "Useful answer.", None

    monkeypatch.setattr(api, "generate_reply_with_retries_and_fallback", generate)
    with TestClient(app) as client:
        assert client.post("/business/engagement/drafts", json={
            "mode": "comment", "account_id": 1, "message_link": "https://t.me/my_group/123",
            "source_text": "Untrusted text", "managed_target": True,
        }).status_code == 422
        response = client.post("/business/engagement/previews", json={
            "account_id": 1, "message_link": "https://t.me/my_group/123",
        })
        assert response.status_code == 202
        preview_id = response.json()["id"]
        with maker() as db:
            preview = db.get(BotCommand, preview_id)
            data = json.loads(preview.args_json)
            data["result"] = {"peer_id": 3993284688, "peer_ref": "-1003993284688",
                              "message_id": 123, "source_text": "Real message text",
                              "managed_title": "Group", "message_link": "https://t.me/c/3993284688/123"}
            preview.args_json = json.dumps(data)
            preview.status = "done"
            preview.processed_at = api.utcnow_naive()
            db.commit()
        assert client.get(f"/business/engagement/previews/{preview_id}").json()["source_text"] == "Real message text"
        response = client.post("/business/engagement/drafts", json={
            "mode": "comment", "preview_id": preview_id,
        })
        assert response.status_code == 201
        draft_id = response.json()["id"]
        assert response.json()["source_text"] == "Real message text"
        assert client.post("/business/engagement/drafts", json={
            "mode": "comment", "preview_id": preview_id,
        }).status_code == 409
        assert client.patch(f"/business/engagement/drafts/{draft_id}", json={
            "draft_text": "Reviewed answer.",
        }).status_code == 200
        approved = client.post(f"/business/engagement/drafts/{draft_id}/approve")
        assert approved.status_code == 200
        command_id = approved.json()["command_id"]

    async def scenario():
        async_engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        async_maker = async_sessionmaker(async_engine, expire_on_commit=False)

        @asynccontextmanager
        async def scope():
            async with async_maker() as db:
                yield db

        monkeypatch.setattr(consumer, "session_scope", scope)

        async def gate(*_args, **_kwargs):
            return True, "ok"

        async def inspect(_client, _link, **kwargs):
            assert kwargs["expected_peer_id"] == 3993284688
            assert kwargs["expected_text"] == "Real message text"
            return {}

        monkeypatch.setattr(consumer, "check_account_gate", gate)
        monkeypatch.setattr(consumer, "inspect_managed_message", inspect)
        calls = []

        class Worker:
            is_connected = True
            client = object()

            async def send_message_with_typing(self, peer, body, **kwargs):
                assert await kwargs["before_send"]()
                calls.append((peer, body, kwargs["reply_to_message_id"]))
                return True, 987, None, None

        manager = SimpleNamespace(workers={1: Worker()})
        try:
            async with async_maker() as db:
                command = await db.get(BotCommand, command_id)
                await consumer.BotCommandConsumer()._send_engagement(command, draft_id, manager)
            with maker() as db:
                assert db.get(EngagementDraft, draft_id).status == "sent"
                assert db.get(BotCommand, command_id).status == "done"
                draft = db.get(EngagementDraft, draft_id)
                command = db.get(BotCommand, command_id)
                draft.status = "sending"
                command.status = "processing"
                db.commit()
            assert calls == [(-1003993284688, "Reviewed answer.", 123)]
            await consumer.BotCommandConsumer()._recover_engagement_after_restart()
            with maker() as db:
                assert db.get(EngagementDraft, draft_id).status == "uncertain"
                assert db.get(BotCommand, command_id).status == "failed"
        finally:
            await async_engine.dispose()

    asyncio.run(scenario())
    engine.dispose()
