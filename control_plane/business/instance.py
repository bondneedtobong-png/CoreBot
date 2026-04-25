"""
Бизнес-API глобальных настроек инстанса.

Хранилище — таблица `instance_settings` (single-row, id=1):
    - openrouter_key_ciphertext  (Fernet или 'p:'/'e:' префиксы)
    - mailing_base_utc_offset    (int | NULL → берётся из .env)
    - neurochat_enabled          (bool | NULL → берётся из .env)

Эндпоинты:
    GET    /business/instance/settings           — текущее значение + effective
    PATCH  /business/instance/settings           — частичное обновление
    POST   /business/instance/openrouter-key     — установить (зашифровать) ключ
    DELETE /business/instance/openrouter-key     — удалить ключ
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from bot.config import MAILING_BASE_UTC_OFFSET as ENV_BASE_UTC_OFFSET
from bot.config import NEUROCHAT_ENABLED as ENV_NEUROCHAT_ENABLED
from control_plane.business.db import get_bot_db
from control_plane.business.schemas import (
    InstanceSettingsOut,
    InstanceSettingsPatch,
    OpenRouterKeyIn,
)
from control_plane.deps import get_current_user
from control_plane.models import User
from database.models import InstanceSettings
from utils.crypto_openrouter import (
    decrypt_openrouter_key,
    encrypt_openrouter_key,
    mask_api_key,
)


router = APIRouter(prefix="/business/instance", tags=["business-instance"])


def _ensure_row(db: Session) -> InstanceSettings:
    row = db.get(InstanceSettings, 1)
    if row is None:
        row = InstanceSettings(id=1)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _serialize(row: InstanceSettings) -> InstanceSettingsOut:
    stored = (row.openrouter_key_ciphertext or "").strip()
    has_key = bool(stored)
    plain = decrypt_openrouter_key(stored) if has_key else ""
    masked = mask_api_key(plain) if plain else ("•••" if has_key else None)
    encrypted = stored.startswith("e:") if has_key else False

    base_offset_db = row.mailing_base_utc_offset
    base_offset_eff = (
        int(base_offset_db) if base_offset_db is not None else int(ENV_BASE_UTC_OFFSET)
    )
    nc_db = row.neurochat_enabled
    nc_eff = bool(nc_db) if nc_db is not None else bool(ENV_NEUROCHAT_ENABLED)

    return InstanceSettingsOut(
        neurochat_enabled_db=nc_db,
        neurochat_enabled_effective=nc_eff,
        mailing_base_utc_offset_db=base_offset_db,
        mailing_base_utc_offset_effective=base_offset_eff,
        openrouter_key_set=has_key,
        openrouter_key_masked=masked,
        openrouter_key_encrypted=encrypted,
    )


@router.get("/settings", response_model=InstanceSettingsOut)
def get_settings(
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    row = _ensure_row(db)
    return _serialize(row)


@router.patch("/settings", response_model=InstanceSettingsOut)
def patch_settings(
    payload: InstanceSettingsPatch,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    row = _ensure_row(db)
    changed = False

    if payload.reset_neurochat_enabled:
        row.neurochat_enabled = None
        changed = True
    elif payload.neurochat_enabled is not None:
        row.neurochat_enabled = bool(payload.neurochat_enabled)
        changed = True

    if payload.reset_mailing_base_utc_offset:
        row.mailing_base_utc_offset = None
        changed = True
    elif payload.mailing_base_utc_offset is not None:
        row.mailing_base_utc_offset = int(payload.mailing_base_utc_offset)
        changed = True

    if changed:
        db.commit()
        db.refresh(row)
    return _serialize(row)


@router.post(
    "/openrouter-key",
    response_model=InstanceSettingsOut,
    status_code=status.HTTP_201_CREATED,
)
def set_openrouter_key(
    payload: OpenRouterKeyIn,
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    row = _ensure_row(db)
    plain = (payload.key or "").strip()
    if not plain:
        raise HTTPException(status_code=400, detail="empty key")
    row.openrouter_key_ciphertext = encrypt_openrouter_key(plain)
    db.commit()
    db.refresh(row)
    # Если в окружении нет ключа шифрования — об этом стоит сообщить через detail.
    if not os.getenv("OPENROUTER_KEY_ENCRYPTION_KEY"):
        # Не ошибка, но фронт может показать предупреждение.
        pass
    return _serialize(row)


@router.delete("/openrouter-key", status_code=status.HTTP_204_NO_CONTENT)
def clear_openrouter_key(
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    row = _ensure_row(db)
    row.openrouter_key_ciphertext = None
    db.commit()
