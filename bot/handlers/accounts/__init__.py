"""
Хендлеры управления аккаунтами (пакет).

Модули по зонам ответственности:
  tdata              — ZIP Tdata, прокси, название в списке, конвертация
  list_card          — список, карточка аккаунта, перепроверка авторизации
  profile            — имя, bio, username
  photos             — управление фото профиля (Telethon)
  twofa              — установка 2FA
  membership_delete  — принадлежность READY/WARMUP/TEST, удаление аккаунта
  groups             — группы аккаунтов, массовые проверки по группе
  cancel             — callback «cancel_accounts»
  proxy_assign       — смена прокси у аккаунта
  tags               — теги в БД

Общие утилиты: common.py, состояния FSM: states.py
"""
from aiogram import Router

from . import (
    cancel,
    groups,
    list_card,
    membership_delete,
    photos,
    profile,
    proxy_assign,
    tags,
    tdata,
    tdata_check,
    twofa,
    warmup,
)

router = Router()

router.include_router(tdata.router)
router.include_router(tdata_check.router)
router.include_router(list_card.router)
router.include_router(groups.router)
router.include_router(profile.router)
router.include_router(photos.router)
router.include_router(twofa.router)
router.include_router(membership_delete.router)
router.include_router(cancel.router)
router.include_router(proxy_assign.router)
router.include_router(tags.router)
router.include_router(warmup.router)

__all__ = ["router"]
