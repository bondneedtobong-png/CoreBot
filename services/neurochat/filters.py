from __future__ import annotations

from dataclasses import dataclass

from database.crm_repositories import ClientClassCounterRepository

@dataclass(slots=True)
class NeuroFilterSettings:
    """
    Каркас фильтров v2.
    Поля будут расширяться по мере внедрения global config и ignored_* списков.
    """

    block_bl: bool = True
    block_ignore: bool = False


async def get_filter_settings() -> NeuroFilterSettings:
    # TODO(v2): вынести в БД/админ-настройки.
    return NeuroFilterSettings()


async def check_client_filters(session, client_id: int) -> tuple[bool, str]:
    cfg = await get_filter_settings()
    counts = await ClientClassCounterRepository.get_counts(session, client_id)
    if cfg.block_bl and counts.get("bl", 0) > 0:
        return False, "client_class_bl"
    if cfg.block_ignore and counts.get("ignore", 0) > 0:
        return False, "client_class_ignore"
    return True, "ok"

