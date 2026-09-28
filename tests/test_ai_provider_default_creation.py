"""New mailings inherit the selected AI provider through both creation paths."""

import asyncio
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from control_plane.business.mailings import create_mailing as create_panel_mailing
from control_plane.business.schemas import MailingCreate
from database.models import AIProvider, Base, InstanceSettings, Mailing
from database.repositories import MailingRepository


def test_default_provider_is_snapshotted_by_bot_and_panel(tmp_path):
    path = tmp_path / "provider-default.db"

    async def create_bot_mailing():
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with maker() as session:
                provider = AIProvider(
                    name="Supplier", kind="custom", base_url="https://api.example.test/v1",
                    api_key_ciphertext="test-ciphertext", default_model="supplier-model",
                    request_type="chat_completions", config_json="{}",
                )
                session.add(provider)
                await session.flush()
                session.add(InstanceSettings(id=1, default_ai_provider_id=provider.id))
                await session.commit()

                first = await MailingRepository.create(session, "hello", name="bot default")
                assert first.neuro_provider_id == provider.id

                settings = await session.get(InstanceSettings, 1)
                settings.default_ai_provider_id = None
                await session.commit()
                second = await MailingRepository.create(session, "hello", name="bot legacy")
                assert second.neuro_provider_id is None
                assert first.neuro_provider_id == provider.id
                return provider.id
        finally:
            await engine.dispose()

    provider_id = asyncio.run(create_bot_mailing())
    engine = create_engine(f"sqlite:///{path}")
    try:
        with Session(engine) as session:
            settings = session.get(InstanceSettings, 1)
            settings.default_ai_provider_id = provider_id
            settings.mailing_neurochat_default = True
            session.commit()
            created = create_panel_mailing(
                MailingCreate(name="panel default", message_text="hello"),
                db=session, _user=SimpleNamespace(username="owner"),
            )
            assert session.get(Mailing, created.id).neuro_provider_id == provider_id
            assert session.get(Mailing, created.id).neurochat_enabled is True

            explicitly_disabled = create_panel_mailing(
                MailingCreate(name="panel override", message_text="hello", neurochat_enabled=False),
                db=session, _user=SimpleNamespace(username="owner"),
            )
            assert session.get(Mailing, explicitly_disabled.id).neurochat_enabled is False

            settings.default_ai_provider_id = None
            settings.mailing_neurochat_default = False
            session.commit()
            legacy = create_panel_mailing(
                MailingCreate(name="panel legacy", message_text="hello"),
                db=session, _user=SimpleNamespace(username="owner"),
            )
            assert session.get(Mailing, legacy.id).neuro_provider_id is None
            assert session.get(Mailing, created.id).neuro_provider_id == provider_id
    finally:
        engine.dispose()
