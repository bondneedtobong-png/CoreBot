from __future__ import annotations

from dataclasses import dataclass

from database.repositories import InstanceSettingsRepository


@dataclass(slots=True)
class NeuroGlobalConfig:
    """
    Каркас глобальной конфигурации v2.
    Сейчас поведение не меняет: считаем модуль включенным.
    """

    enabled: bool = True


async def get_global_config(session) -> NeuroGlobalConfig:
    enabled = await InstanceSettingsRepository.get_effective_neurochat_enabled(session)
    return NeuroGlobalConfig(enabled=bool(enabled))

