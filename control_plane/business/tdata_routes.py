"""TData ZIP import endpoint for the web panel."""
from __future__ import annotations

import asyncio
import io
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from control_plane.business.tdata import find_tdata_roots
from control_plane.business.db import get_bot_db
from control_plane.deps import require_operator_write
from control_plane.models import User
from database.models import Account, AccountStatus
from database.sqlite_pragmas import run_sync_with_busy_retry
from utils.logger import log
from workers.session_converter import convert_tdata_to_session

router = APIRouter(prefix="/business/tdata", tags=["business-tdata"])


def _create_account_from_tdata(db: Session, result: dict, list_label: Optional[str] = None) -> int:
    """Идемпотентное создание аккаунта из TData.

    Повторный импорт той же session_name возвращает существующий id вместо
    падения (409/skip на границе, не blanket-обработчик).
    """
    session_name = result.get("session_name", "")
    existing = db.query(Account).filter(Account.session_name == session_name).first()
    if existing:
        return int(existing.id)

    row = Account(
        phone=result.get("phone") or "unknown",
        session_name=session_name,
        username=result.get("username"),
        first_name=result.get("first_name"),
        last_name=result.get("last_name"),
        status=AccountStatus.ACTIVE,
        list_label=list_label,
    )
    db.add(row)
    try:
        run_sync_with_busy_retry(db.commit, op_name="tdata-create-account")
    except IntegrityError:
        # Конкурентный дубль: откат и возврат существующей строки.
        db.rollback()
        existing = db.query(Account).filter(Account.session_name == session_name).first()
        if existing is not None:
            log.info(f"TData дубль {session_name}: уже в БД, пропуск (idempotent)")
            return int(existing.id)
        raise
    db.refresh(row)
    return int(row.id)


async def run_tdata_import(
    data: bytes,
    db: Session,
    requested_by: str,
    sessions_dir: Path,
) -> dict[str, Any]:
    if not data:
        raise HTTPException(status_code=400, detail="archive is empty")

    tmp_dir = Path(tempfile.mkdtemp(prefix="tdata-web-"))
    try:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                zf.extractall(tmp_dir)
        except zipfile.BadZipFile as exc:
            raise HTTPException(status_code=400, detail="not a valid ZIP archive") from exc

        roots = find_tdata_roots(tmp_dir)
        if not roots:
            raise HTTPException(status_code=400, detail="no valid tdata folders found")

        sessions_dir.mkdir(parents=True, exist_ok=True)
        total = len(roots)
        converted = 0
        failed = 0
        errors: list[str] = []
        created_ids: list[int] = []

        for idx, root in enumerate(roots, 1):
            try:
                result = await convert_tdata_to_session(root, sessions_dir)
                if result and result.get("success"):
                    acc_id = _create_account_from_tdata(db, result)
                    created_ids.append(acc_id)
                    converted += 1
                else:
                    failed += 1
                    errors.append(str(result.get("error", "unknown conversion error")))
            except Exception as exc:
                failed += 1
                errors.append(str(exc))

        return {
            "ok": failed == 0,
            "total": total,
            "converted": converted,
            "failed": failed,
            "errors": errors[:20],
            "requested_by": requested_by,
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@router.post("/import")
async def import_tdata(
    file: UploadFile,
    db: Session = Depends(get_bot_db),
    user: User = Depends(require_operator_write),
) -> dict:
    data = await file.read()
    return await run_tdata_import(data, db, user.username, Path("data/sessions"))
