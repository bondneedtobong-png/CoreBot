"""A reused mailing ID must never inherit a removed campaign's prompt."""
from pathlib import Path

from utils import neuro_prompts


def test_orphan_prompt_is_archived_only_for_the_live_database(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(neuro_prompts, "NEURO_MAILING_PROMPTS_DIR", tmp_path / "mailings")
    monkeypatch.setattr(neuro_prompts, "DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'live.db'}")
    source = neuro_prompts.neuro_prompt_file_path(3)
    source.parent.mkdir(parents=True)
    source.write_text("Old campaign identity", encoding="utf-8")

    assert neuro_prompts.archive_orphan_prompt(
        3, database_url=f"sqlite:///{tmp_path / 'test.db'}"
    ) is None
    assert source.read_text(encoding="utf-8") == "Old campaign identity"

    archived = neuro_prompts.archive_orphan_prompt(
        3, database_url=f"sqlite:///{tmp_path / 'live.db'}"
    )
    assert archived is not None
    assert archived.read_text(encoding="utf-8") == "Old campaign identity"
    assert not source.exists()
    assert "Old campaign identity" not in neuro_prompts.load_system_prompt(3)
