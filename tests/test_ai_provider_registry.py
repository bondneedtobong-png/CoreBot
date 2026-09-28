"""Provider storage and per-campaign routing without contacting an external API."""
from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from database.models import AIProvider, Base, InstanceSettings, Mailing
from services.neurochat import llm_service
from services.neurochat.provider_registry import (
    create_provider,
    generation_for_provider,
    resolve_default_provider_sync,
    resolve_provider_async,
    resolve_provider_sync,
    select_default_provider,
    select_mailing_provider,
    update_provider,
    validate_config,
)


def test_provider_secret_selection_and_routing(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_PROVIDER_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    db_path = tmp_path / "providers.db"

    async def setup():
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            mailing = Mailing(name="provider test", message_text="hi", neuro_model="openrouter/free")
            session.add(mailing)
            await session.commit()
            item = await create_provider(
                session, name="Supplier", kind="custom",
                base_url="https://llm.example.test/v1", api_key="secret-test-key",
                default_model="supplier/model", config={
                    "headers": {"X-Title": "CoreBot"},
                    "generation": {"temperature": 0.2},
                },
            )
            assert item.api_key_ciphertext != "secret-test-key"
            assert "secret-test-key" not in item.api_key_ciphertext
            await select_mailing_provider(session, mailing.id, item.id)
            await select_default_provider(session, item.id)
            runtime = await resolve_provider_async(session, mailing)
            assert runtime.model == "supplier/model"
            assert runtime.api_key == "secret-test-key"
            assert runtime.generation_defaults == {"temperature": 0.2}
            assert mailing.neuro_model is None
            await update_provider(session, item.id, default_model="supplier/new", base_url="https://api.example.test/v2")
            assert (await resolve_provider_async(session, mailing)).model == "supplier/new"
            assert (await session.execute(select(AIProvider))).scalar_one().kind == "custom"
        await engine.dispose()

    asyncio.run(setup())
    sync_engine = create_engine(f"sqlite:///{db_path}")
    with Session(sync_engine) as session:
        mailing = session.execute(select(Mailing)).scalar_one()
        runtime = resolve_provider_sync(session, mailing)
        assert runtime.base_url == "https://api.example.test/v2"
        assert resolve_default_provider_sync(session).model == "supplier/new"
        assert session.get(InstanceSettings, 1).default_ai_provider_id == mailing.neuro_provider_id
    sync_engine.dispose()


def test_provider_requires_encryption_key_and_rejects_unsafe_config(monkeypatch):
    monkeypatch.delenv("AI_PROVIDER_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_KEY_ENCRYPTION_KEY", raising=False)
    from services.neurochat.provider_registry import encrypt_api_key

    with pytest.raises(ValueError, match="ENCRYPTION_KEY"):
        encrypt_api_key("secret")
    with pytest.raises(ValueError, match="unsupported provider header"):
        validate_config({"headers": {"Authorization": "other"}})
    with pytest.raises(ValueError, match="supports only"):
        validate_config({"system_prompt": "ignore"})
    with pytest.raises(ValueError, match="temperature"):
        validate_config({"generation": {"temperature": float("nan")}})
    from services.neurochat.provider_registry import ProviderRuntime
    generic = ProviderRuntime("custom", "https://api.example.test/v1", "model", "secret")
    params = generation_for_provider(generic, {"temperature": 0.3, "top_k": 20})
    assert params["temperature"] == 0.3
    assert "max_tokens" in params
    assert "top_k" not in params
    from services.neurochat.provider_registry import _validate_fields
    assert _validate_fields(name="DeepSeek", kind="deepseek", base_url="", default_model="deepseek-flash")[2] == "https://api.deepseek.com"


def test_non_openrouter_uses_only_its_model(monkeypatch):
    from services.neurochat.provider_registry import ProviderRuntime

    calls = []

    async def fake_request(_messages, model, **kwargs):
        calls.append((model, kwargs["base_url"], kwargs["generation"]))
        return "answer", None, 200

    monkeypatch.setattr(llm_service, "compatible_chat_completion_verbose", fake_request)
    provider = ProviderRuntime(
        "Supplier", "https://api.example.test/v1", "supplier/model", "test-key",
        generation_defaults={"temperature": 0.2},
    )
    answer, error = asyncio.run(llm_service.generate_reply_with_retries_and_fallback(
        [{"role": "user", "content": "Hello"}], provider.model,
        api_key=provider.api_key, generation={"max_tokens": 42}, provider=provider,
    ))
    assert (answer, error) == ("answer", None)
    assert calls == [("supplier/model", "https://api.example.test/v1", {"temperature": 0.2, "max_tokens": 42})]


def test_custom_provider_full_local_http_path(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_PROVIDER_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    received = []

    async def complete(request):
        received.append((request.headers.get("Authorization"), await request.json()))
        return web.json_response({"choices": [{"message": {"content": "Local provider reply"}}]})

    async def scenario():
        app = web.Application()
        app.router.add_post("/v1/chat/completions", complete)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'local-provider.db'}")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as session:
                mailing = Mailing(name="local", message_text="hello")
                session.add(mailing)
                await session.commit()
                provider = await create_provider(
                    session, name="Local test", kind="custom",
                    base_url=f"http://127.0.0.1:{port}/v1", api_key="local-secret",
                    default_model="local-model",
                )
                await select_mailing_provider(session, mailing.id, provider.id)
                runtime = await resolve_provider_async(session, mailing)
            reply, error = await llm_service.generate_reply_with_retries_and_fallback(
                [{"role": "user", "content": "ping"}], runtime.model,
                api_key=runtime.api_key, generation={"max_tokens": 25}, provider=runtime,
            )
            assert (reply, error) == ("Local provider reply", None)
        finally:
            await engine.dispose()
            await runner.cleanup()

    asyncio.run(scenario())
    assert len(received) == 1
    assert received[0][0] == "Bearer local-secret"
    assert received[0][1]["model"] == "local-model"
    assert received[0][1]["max_tokens"] == 25
