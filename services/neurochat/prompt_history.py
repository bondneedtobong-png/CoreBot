"""Database-backed, incarnation-scoped system prompt history."""

from __future__ import annotations

import hashlib
from datetime import timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select, text, update
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from bot.config import DATABASE_URL, DEFAULT_NEURO_SYSTEM_PROMPT, NEURO_MAILING_PROMPTS_DIR
from database.models import Mailing, NeuroPromptVersion
from database.sqlite_pragmas import (
    commit_with_busy_retry,
    execute_with_busy_retry,
    run_sync_with_busy_retry,
    run_with_busy_retry,
)
from utils.neuro_prompts import prepare_system_prompt_text


class PromptConflict(Exception):
    """The active version changed since the editor read it."""


def _same_database(actual_url: str, configured_url: str | None = None) -> bool:
    actual = make_url(actual_url).database
    configured = make_url(configured_url or DATABASE_URL).database
    return bool(actual and configured and Path(actual).resolve() == Path(configured).resolve())


def migrate_prompt_history(conn: Connection, database_url: str) -> None:
    """Idempotently add prompt columns and import legacy files for the live DB."""
    columns = {row[1] for row in conn.execute(text("PRAGMA table_info(mailings)"))}
    if "prompt_scope_uuid" not in columns or "prompt_revision" not in columns:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS mailings_pre_prompt_history_backup "
            "AS SELECT * FROM mailings"
        ))
    if "prompt_scope_uuid" not in columns:
        conn.execute(text("ALTER TABLE mailings ADD COLUMN prompt_scope_uuid VARCHAR(32)"))
    if "prompt_revision" not in columns:
        conn.execute(text("ALTER TABLE mailings ADD COLUMN prompt_revision INTEGER NOT NULL DEFAULT 0"))
    rows = conn.execute(text("SELECT id, prompt_scope_uuid, prompt_revision FROM mailings")).all()
    import_files = _same_database(database_url)
    for mailing_id, scope_uuid, revision in rows:
        if not scope_uuid:
            scope_uuid = uuid4().hex
            conn.execute(
                text("UPDATE mailings SET prompt_scope_uuid=:scope WHERE id=:id"),
                {"scope": scope_uuid, "id": mailing_id},
            )
        if revision or not import_files:
            continue
        path = NEURO_MAILING_PROMPTS_DIR / str(mailing_id) / "system.txt"
        if not path.is_file():
            continue
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            continue
        if len(raw) > 20000:
            raise ValueError(f"legacy prompt for mailing {mailing_id} exceeds 20000 characters")
        conn.execute(
            text("INSERT INTO neuro_prompt_versions "
                 "(mailing_id,scope_uuid,revision,raw_text,action,actor,sha256,created_at) "
                 "VALUES (:id,:scope,1,:raw,'import','migration',:digest,CURRENT_TIMESTAMP)"),
            {"id": mailing_id, "scope": scope_uuid, "raw": raw,
             "digest": hashlib.sha256(raw.encode("utf-8")).hexdigest()},
        )
        conn.execute(
            text("UPDATE mailings SET prompt_revision=1 WHERE id=:id"),
            {"id": mailing_id},
        )


def _version_query(mailing: Mailing):
    return select(NeuroPromptVersion).where(
        NeuroPromptVersion.mailing_id == mailing.id,
        NeuroPromptVersion.scope_uuid == mailing.prompt_scope_uuid,
    )


def current_version_sync(db: Session, mailing: Mailing) -> NeuroPromptVersion | None:
    if not mailing.prompt_revision:
        return None
    return db.scalar(_version_query(mailing).where(
        NeuroPromptVersion.revision == mailing.prompt_revision,
    ))


async def current_version_async(db: AsyncSession, mailing: Mailing) -> NeuroPromptVersion | None:
    if not mailing.prompt_revision:
        return None
    return await db.scalar(_version_query(mailing).where(
        NeuroPromptVersion.revision == mailing.prompt_revision,
    ))


def prepared_text(version: NeuroPromptVersion | None) -> str:
    raw = version.raw_text if version is not None else None
    return prepare_system_prompt_text(raw if raw is not None else DEFAULT_NEURO_SYSTEM_PROMPT)


def prompt_state(db: Session, mailing: Mailing) -> dict:
    version = current_version_sync(db, mailing)
    return {
        "mailing_id": mailing.id,
        "text": prepared_text(version),
        "has_custom_file": bool(version and version.raw_text is not None),
        "version_id": version.id if version else None,
    }


def version_metadata(version: NeuroPromptVersion, current_id: int | None) -> dict:
    created = version.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return {
        "id": version.id,
        "created_at": created.isoformat(),
        "actor": version.actor,
        "action": version.action,
        "sha256": version.sha256,
        "is_active": version.id == current_id,
        "restored_from_version_id": version.restored_from_version_id,
    }


def list_versions(db: Session, mailing: Mailing) -> dict:
    current = current_version_sync(db, mailing)
    versions = db.scalars(_version_query(mailing).order_by(NeuroPromptVersion.revision)).all()
    return {
        "current_version_id": current.id if current else None,
        "versions": [version_metadata(item, current.id if current else None) for item in versions],
    }


def get_version(db: Session, mailing: Mailing, version_id: int) -> NeuroPromptVersion | None:
    return db.scalar(_version_query(mailing).where(NeuroPromptVersion.id == version_id))


def save_version(
    db: Session, mailing: Mailing, *, raw_text: str | None, action: str,
    actor: str, expected_version_id: int | None = None, check_expected: bool = False,
    restored_from_version_id: int | None = None,
) -> dict:
    try:
        current = current_version_sync(db, mailing)
        current_id = current.id if current else None
        if check_expected and current_id != expected_version_id:
            raise PromptConflict
        previous_revision = mailing.prompt_revision
        changed = run_sync_with_busy_retry(
            lambda: db.execute(
                update(Mailing).where(
                    Mailing.id == mailing.id,
                    Mailing.prompt_scope_uuid == mailing.prompt_scope_uuid,
                    Mailing.prompt_revision == previous_revision,
                ).values(prompt_revision=previous_revision + 1),
            ), op_name="prompt-version-cas",
        )
        if changed.rowcount != 1:
            raise PromptConflict
        value = (raw_text or "").strip() if raw_text is not None else None
        version = NeuroPromptVersion(
            mailing_id=mailing.id,
            scope_uuid=mailing.prompt_scope_uuid,
            revision=previous_revision + 1,
            raw_text=value,
            action=action,
            actor=actor[:255],
            sha256=hashlib.sha256(value.encode("utf-8")).hexdigest() if value is not None else None,
            restored_from_version_id=restored_from_version_id,
        )
        db.add(version)
        run_sync_with_busy_retry(lambda: db.flush(), op_name="prompt-version-flush")
        run_sync_with_busy_retry(lambda: db.commit(), op_name="prompt-version-commit")
        db.refresh(version)
        return {
            "mailing_id": mailing.id,
            "text": prepared_text(version),
            "has_custom_file": value is not None,
            "version_id": version.id,
        }
    except Exception:
        db.rollback()
        raise


async def save_version_async(
    db: AsyncSession, mailing: Mailing, *, raw_text: str, actor: str,
) -> NeuroPromptVersion:
    try:
        previous_revision = mailing.prompt_revision
        changed = await execute_with_busy_retry(
            db,
            update(Mailing).where(
                Mailing.id == mailing.id,
                Mailing.prompt_scope_uuid == mailing.prompt_scope_uuid,
                Mailing.prompt_revision == previous_revision,
            ).values(prompt_revision=previous_revision + 1),
            op_name="prompt-upload-cas",
        )
        if changed.rowcount != 1:
            raise PromptConflict
        version = NeuroPromptVersion(
            mailing_id=mailing.id, scope_uuid=mailing.prompt_scope_uuid,
            revision=previous_revision + 1, raw_text=raw_text,
            action="upload", actor=actor[:255],
            sha256=hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
        )
        db.add(version)
        await run_with_busy_retry(lambda: db.flush(), op_name="prompt-upload-flush")
        await commit_with_busy_retry(db, op_name="prompt-upload-commit")
        return version
    except Exception:
        await db.rollback()
        raise
