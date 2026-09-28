"""Archive safety and profile pool behavior without Telegram network calls."""
import asyncio
from pathlib import Path
from zipfile import ZipFile

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.handlers.accounts.tdata_v2 import _labels
from database.models import Base
from database.profile_templates import add_pool_batch, pool_counts, random_profile
from utils.safe_zip import extract_zip_safely


def test_zip_extraction_rejects_traversal(tmp_path: Path):
    archive = tmp_path / "danger.zip"
    with ZipFile(archive, "w") as output:
        output.writestr("../outside.txt", "should not be written")
    with pytest.raises(ValueError, match="путь"):
        extract_zip_safely(archive, tmp_path / "inside")
    assert not (tmp_path / "outside.txt").exists()


def test_one_or_many_tdata_labels():
    assert _labels("Colombia", 1) == ["Colombia"]
    assert _labels("Colombia", 3) == ["Colombia ·1", "Colombia ·2", "Colombia ·3"]
    assert _labels("A\nB\nC", 3) == ["A", "B", "C"]
    with pytest.raises(ValueError):
        _labels("A\nB", 3)


def test_profile_pool_persists_independent_candidates():
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions() as session:
                added = await add_pool_batch(session, {
                    "name": ["Anna", "Maria", "Anna"],
                    "bio": ["Hello"],
                    "photo": ["photo.jpg"],
                })
                assert added == {"name": 2, "bio": 1, "photo": 1}
                assert await pool_counts(session) == added
                chosen = await random_profile(session)
                assert chosen["name"] in {"Anna", "Maria"}
                assert chosen["bio"] == "Hello"
                assert chosen["photo"] == "photo.jpg"
        finally:
            await engine.dispose()

    asyncio.run(scenario())
