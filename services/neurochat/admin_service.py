from __future__ import annotations

from pathlib import Path
from typing import Any

from database.repositories import NeuroActionRepository
from utils.neuro_prompts import load_system_prompt, neuro_prompt_file_path, prompt_file_exists
from utils.neuro_sampling import merge_sampling_for_request, parse_sampling_mailing_column


async def count_actions_by_mailing(session, mailing_id: int) -> dict[str, int]:
    return await NeuroActionRepository.count_by_action(session, mailing_id)


def get_prompt_path(mailing_id: int) -> Path:
    return neuro_prompt_file_path(mailing_id)


def has_prompt_file(mailing_id: int) -> bool:
    return prompt_file_exists(mailing_id)


def load_prompt_text(mailing_id: int) -> str:
    return load_system_prompt(mailing_id)


def get_sampling_overrides(mailing: Any) -> dict[str, Any]:
    return parse_sampling_mailing_column(getattr(mailing, "neuro_sampling_json", None))


def get_sampling_effective(mailing: Any) -> dict[str, Any]:
    return merge_sampling_for_request(get_sampling_overrides(mailing))

