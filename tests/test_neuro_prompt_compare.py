"""Prompt comparison uses the live prompt preparation path without a Telegram send."""

import sqlite3
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from control_plane.business import mailings
from control_plane.business.db import get_bot_db
from control_plane.deps import get_current_user
from database.models import Account, Base, Mailing
from services.neurochat.provider_registry import ProviderRuntime
from utils import neuro_prompts


def test_compare_saved_and_candidate_prompt_without_persisting(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'prompt-compare.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(engine)
    with maker.begin() as db:
        db.add(Mailing(name="test campaign", message_text="hello", neuro_model="test/model"))
        db.add(Account(phone="+10000000661", session_name="prompt-account", first_name="Ana"))
    monkeypatch.setattr(neuro_prompts, "NEURO_MAILING_PROMPTS_DIR", tmp_path / "prompts")
    prompt_path = neuro_prompts.neuro_prompt_file_path(1)
    prompt_path.parent.mkdir(parents=True)
    prompt_path.write_text("Saved instructions {first_name}", encoding="utf-8")
    monkeypatch.setattr(mailings, "resolve_provider_sync", lambda _db, _mailing: ProviderRuntime(
        "OpenRouter", "https://openrouter.ai/api/v1", "test/model", "test-key",
        is_openrouter=True,
    ))
    calls = []

    async def fake_generate(messages, model, *, api_key, generation, provider):
        calls.append(([message.copy() for message in messages], model, api_key, generation))
        prompt = "saved" if "Saved instructions" in messages[0]["content"] else "candidate"
        return f"{prompt} reply {sum(message['role'] == 'user' for message in messages)}", None

    monkeypatch.setattr(mailings, "generate_reply_with_retries_and_fallback", fake_generate)
    app = FastAPI()
    app.include_router(mailings.router)

    def test_db():
        with maker() as db:
            yield db

    app.dependency_overrides[get_bot_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=1, username="owner", role="tenant_admin",
    )
    try:
        with TestClient(app) as client:
            url = "/business/mailings/1/prompt/compare"
            assert client.put("/business/mailings/1/prompt", json={
                "text": "Saved instructions {first_name}",
            }).status_code == 200
            payload = {"sample_message": "Ignored first", "sample_messages": ["Hello", "Follow up", "Final ask"],
                       "candidate_text": "Candidate {first_name}",
                       "account_id": 1}
            with sqlite3.connect(engine.url.database) as audit_db:
                before = list(audit_db.iterdump())
            response = client.post(url, json=payload)
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["saved_reply"] == "saved reply 3"
            assert result["candidate_reply"] == "candidate reply 3"
            assert result["turns"] == [
                {"user_message": message, "saved_reply": f"saved reply {turn}",
                 "candidate_reply": f"candidate reply {turn}"}
                for turn, message in enumerate(payload["sample_messages"], 1)
            ]
            assert result["same_prompt"] is False
            assert result["telegram_sent"] is False
            assert len(calls) == 6
            assert all(call[1] == "test/model" and call[2] == "test-key" for call in calls)
            assert all(call[3]["max_tokens"] <= 300 for call in calls)
            assert all(call[0][1] == {"role": "user", "content": "Hello"} for call in calls)
            assert calls[2][0][2:] == [
                {"role": "assistant", "content": "saved reply 1"},
                {"role": "user", "content": "Follow up"},
            ]
            assert calls[3][0][2:] == [
                {"role": "assistant", "content": "candidate reply 1"},
                {"role": "user", "content": "Follow up"},
            ]
            assert calls[4][0][4] == {"role": "assistant", "content": "saved reply 2"}
            assert calls[5][0][4] == {"role": "assistant", "content": "candidate reply 2"}
            assert "Ana" in calls[0][0][0]["content"]
            assert "Ana" in calls[1][0][0]["content"]
            assert prompt_path.read_text(encoding="utf-8") == "Saved instructions {first_name}"
            with sqlite3.connect(engine.url.database) as audit_db:
                assert list(audit_db.iterdump()) == before

            same = client.post(url, json={**payload, "candidate_text": "Saved instructions {first_name}"})
            assert same.status_code == 200
            assert same.json()["same_prompt"] is True
            assert len(same.json()["turns"]) == 3
            assert all(turn["saved_reply"] == turn["candidate_reply"] for turn in same.json()["turns"])
            assert len(calls) == 9  # identical prompts generate only once per turn
            assert calls[7][0][2] == {"role": "assistant", "content": "saved reply 1"}
            legacy = client.post(url, json={
                "sample_message": "Hello", "candidate_text": "Candidate {first_name}",
            })
            assert legacy.status_code == 200
            assert legacy.json()["turns"] == [
                {"user_message": "Hello", "saved_reply": "saved reply 1",
                 "candidate_reply": "candidate reply 1"},
            ]
            assert client.post(url, json={**payload, "sample_messages": []}).status_code == 422
            assert client.post(url, json={**payload, "sample_messages": ["ok"]}).status_code == 422
            assert client.post(url, json={**payload, "sample_messages": ["   "]}).status_code == 422
            assert client.post(url, json={**payload, "sample_messages": ["a" * 1001]}).status_code == 422
            assert client.post(url, json={**payload, "sample_messages": ["one", "two", "three", "four"]}).status_code == 422
            assert client.post(url, json={**payload, "candidate_text": "   "}).status_code == 400
            assert client.post("/business/mailings/999/prompt/compare", json=payload).status_code == 404
            assert client.post(url, json={**payload, "account_id": 999}).status_code == 404

            async def failed_generate(*_args, **_kwargs):
                raise RuntimeError("secret provider response")

            monkeypatch.setattr(mailings, "generate_reply_with_retries_and_fallback", failed_generate)
            failed = client.post(url, json=payload)
            assert failed.status_code == 502
            assert failed.json()["detail"] == "prompt test failed"

            app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
                id=2, username="viewer", role="tenant_viewer",
            )
            assert client.post(url, json=payload).status_code == 403
    finally:
        engine.dispose()
