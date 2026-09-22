"""Conflict-safe persistence for parsed entities and task counters."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import (
    ParsedChannel,
    ParsedGroup,
    ParsedUser,
    ParsedUserSource,
    ParsingTask,
)


async def upsert_channel(
    session: AsyncSession,
    *,
    telegram_id: int,
    source_task_id: int,
    username: Optional[str] = None,
    title: Optional[str] = None,
    subscribers: Optional[int] = None,
    is_public: Optional[bool] = None,
    has_discussion: Optional[bool] = None,
    lang: Optional[str] = None,
    last_post_at: Optional[datetime] = None,
    is_active_7d: Optional[bool] = None,
) -> str:
    existed = await session.scalar(
        select(ParsedChannel.id).where(ParsedChannel.telegram_id == telegram_id)
    )
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    stmt = sqlite_insert(ParsedChannel).values(
        telegram_id=telegram_id,
        username=username,
        title=title,
        subscribers=subscribers,
        is_public=is_public,
        has_discussion=has_discussion,
        lang=lang,
        last_post_at=last_post_at,
        is_active_7d=is_active_7d,
        source_task_id=source_task_id,
        updated_at=now,
    )
    excluded = stmt.excluded
    stmt = stmt.on_conflict_do_update(
        index_elements=[ParsedChannel.telegram_id],
        set_={
            "username": func.coalesce(excluded.username, ParsedChannel.username),
            "title": func.coalesce(excluded.title, ParsedChannel.title),
            "subscribers": func.coalesce(excluded.subscribers, ParsedChannel.subscribers),
            "is_public": func.coalesce(excluded.is_public, ParsedChannel.is_public),
            "has_discussion": func.coalesce(excluded.has_discussion, ParsedChannel.has_discussion),
            "lang": func.coalesce(excluded.lang, ParsedChannel.lang),
            "last_post_at": func.coalesce(excluded.last_post_at, ParsedChannel.last_post_at),
            "is_active_7d": func.coalesce(excluded.is_active_7d, ParsedChannel.is_active_7d),
            "source_task_id": source_task_id,
            "updated_at": now,
        },
    )
    await session.execute(stmt)
    return "update" if existed is not None else "insert"


async def upsert_group(
    session: AsyncSession,
    *,
    telegram_id: int,
    source_task_id: int,
    username: Optional[str] = None,
    title: Optional[str] = None,
    members_count: Optional[int] = None,
    group_type: Optional[str] = None,
    lang: Optional[str] = None,
    is_active_7d: Optional[bool] = None,
) -> str:
    existed = await session.scalar(
        select(ParsedGroup.id).where(ParsedGroup.telegram_id == telegram_id)
    )
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    stmt = sqlite_insert(ParsedGroup).values(
        telegram_id=telegram_id,
        username=username,
        title=title,
        members_count=members_count,
        group_type=group_type,
        lang=lang,
        is_active_7d=is_active_7d,
        source_task_id=source_task_id,
        updated_at=now,
    )
    excluded = stmt.excluded
    stmt = stmt.on_conflict_do_update(
        index_elements=[ParsedGroup.telegram_id],
        set_={
            "username": func.coalesce(excluded.username, ParsedGroup.username),
            "title": func.coalesce(excluded.title, ParsedGroup.title),
            "members_count": func.coalesce(excluded.members_count, ParsedGroup.members_count),
            "group_type": func.coalesce(excluded.group_type, ParsedGroup.group_type),
            "lang": func.coalesce(excluded.lang, ParsedGroup.lang),
            "is_active_7d": func.coalesce(excluded.is_active_7d, ParsedGroup.is_active_7d),
            "source_task_id": source_task_id,
            "updated_at": now,
        },
    )
    await session.execute(stmt)
    return "update" if existed is not None else "insert"


async def upsert_user(
    session: AsyncSession,
    *,
    telegram_id: int,
    source_task_id: int,
    username: Optional[str] = None,
    display_name: Optional[str] = None,
    has_avatar: Optional[bool] = None,
    last_seen_at: Optional[datetime] = None,
    lang_guess: Optional[str] = None,
    is_deleted: bool = False,
    is_suspicious: bool = False,
) -> ParsedUser:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    stmt = sqlite_insert(ParsedUser).values(
        telegram_id=telegram_id,
        username=username,
        display_name=display_name,
        has_avatar=has_avatar,
        last_seen_at=last_seen_at,
        lang_guess=lang_guess,
        is_deleted=is_deleted,
        is_suspicious=is_suspicious,
        source_task_id=source_task_id,
        updated_at=now,
    )
    excluded = stmt.excluded
    stmt = stmt.on_conflict_do_update(
        index_elements=[ParsedUser.telegram_id],
        set_={
            "username": func.coalesce(excluded.username, ParsedUser.username),
            "display_name": func.coalesce(excluded.display_name, ParsedUser.display_name),
            "has_avatar": func.coalesce(excluded.has_avatar, ParsedUser.has_avatar),
            "last_seen_at": func.coalesce(excluded.last_seen_at, ParsedUser.last_seen_at),
            "lang_guess": func.coalesce(excluded.lang_guess, ParsedUser.lang_guess),
            "is_deleted": is_deleted,
            "is_suspicious": is_suspicious,
            "source_task_id": source_task_id,
            "updated_at": now,
        },
    ).returning(ParsedUser.id)
    user_id = int((await session.execute(stmt)).scalar_one())
    row = await session.get(ParsedUser, user_id)
    if row is None:
        raise RuntimeError(f"parsed user upsert did not return row: {telegram_id}")
    return row


async def add_user_source_edge(
    session: AsyncSession,
    *,
    parsed_user: ParsedUser,
    source_entity_id: int,
    source_entity_kind: str,
    source_kind: str,
    source_task_id: int,
) -> None:
    stmt = sqlite_insert(ParsedUserSource).values(
        parsed_user_id=parsed_user.id,
        source_entity_id=source_entity_id,
        source_entity_kind=source_entity_kind,
        source_kind=source_kind,
        source_task_id=source_task_id,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=[
            ParsedUserSource.parsed_user_id,
            ParsedUserSource.source_entity_id,
            ParsedUserSource.source_entity_kind,
            ParsedUserSource.source_kind,
        ]
    )
    await session.execute(stmt)


async def bump_task_counters(
    session: AsyncSession,
    task_id: int,
    *,
    found_delta: int = 0,
    filtered_delta: int = 0,
    error_delta: int = 0,
    progress_percent: Optional[int] = None,
    current_stage: Optional[str] = None,
    current_account_id: Optional[int] = None,
    current_query: Optional[str] = None,
) -> None:
    vals: dict[str, Any] = dict(
        found_count=ParsingTask.found_count + found_delta,
        filtered_count=ParsingTask.filtered_count + filtered_delta,
        error_count=ParsingTask.error_count + error_delta,
    )
    if progress_percent is not None:
        vals["progress_percent"] = progress_percent
    if current_stage is not None:
        vals["current_stage"] = current_stage
    if current_account_id is not None:
        vals["current_account_id"] = current_account_id
    if current_query is not None:
        vals["current_query"] = current_query
    await session.execute(update(ParsingTask).where(ParsingTask.id == task_id).values(**vals))
