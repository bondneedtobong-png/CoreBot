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
from sqlalchemy.orm import Session

from control_plane.business.tdata import find_tdata_roots
from control_plane.business.db import get_bot_db
from control_plane.deps import require_operator_write
from control_plane.models import User
from database.models import Account, AccountStatus
from utils.logger import log
from workers.session_converter import convert_tdata_to_session

router = APIRouter(prefix="/business/tdata", tags=["business-tdata"])


def _create_account_from_tdata(db: Session, result: dict, list_label: Optional[str] = None) -> int:
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
    db.commit()
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
