"""Загрузка system-промпта из data/neuro/mailings/{id}/system.txt."""
from pathlib import Path

from bot.config import DEFAULT_NEURO_SYSTEM_PROMPT, NEURO_MAILING_PROMPTS_DIR


def neuro_prompt_file_path(mailing_id: int) -> Path:
    return NEURO_MAILING_PROMPTS_DIR / str(mailing_id) / "system.txt"


def load_system_prompt(mailing_id: int) -> str:
    path = neuro_prompt_file_path(mailing_id)
    if path.exists():
        try:
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text
        except OSError:
            pass
    return DEFAULT_NEURO_SYSTEM_PROMPT


def prompt_file_exists(mailing_id: int) -> bool:
    return neuro_prompt_file_path(mailing_id).is_file()
