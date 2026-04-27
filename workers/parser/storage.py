"""Upsert распарсенных сущностей + счётчики (found/filtered) на задаче."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select, update
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
    """Возвращает 'insert' | 'update'."""
    row = await session.scalar(select(ParsedChannel).where(ParsedChannel.telegram_id == telegram_id))
    now = datetime.utcnow()
    if row:
        row.username = username or row.username
        row.title = title or row.title
        if subscribers is not None:
            row.subscribers = subscribers
        if is_public is not None:
            row.is_public = is_public
        if has_discussion is not None:
            row.has_discussion = has_discussion
        if lang is not None:
            row.lang = lang
        if last_post_at is not None:
            row.last_post_at = last_post_at
        if is_active_7d is not None:
            row.is_active_7d = is_active_7d
        row.source_task_id = source_task_id
        row.updated_at = now
        return "update"
    session.add(
        ParsedChannel(
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
    )
    return "insert"


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
    row = await session.scalar(select(ParsedGroup).where(ParsedGroup.telegram_id == telegram_id))
    now = datetime.utcnow()
    if row:
        row.username = username or row.username
        row.title = title or row.title
        if members_count is not None:
            row.members_count = members_count
        if group_type is not None:
            row.group_type = group_type
        if lang is not None:
            row.lang = lang
        if is_active_7d is not None:
            row.is_active_7d = is_active_7d
        row.source_task_id = source_task_id
        row.updated_at = now
        return "update"
    session.add(
        ParsedGroup(
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
    )
    return "insert"


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
    row = await session.scalar(select(ParsedUser).where(ParsedUser.telegram_id == telegram_id))
    now = datetime.utcnow()
    if row:
        if username:
            row.username = username
        if display_name:
            row.display_name = display_name
        if has_avatar is not None:
            row.has_avatar = has_avatar
        if last_seen_at is not None:
            row.last_seen_at = last_seen_at
        if lang_guess is not None:
            row.lang_guess = lang_guess
        row.is_deleted = is_deleted
        row.is_suspicious = is_suspicious
        row.source_task_id = source_task_id
        row.updated_at = now
        return row
    u = ParsedUser(
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
    session.add(u)
    await session.flush()
    return u


async def add_user_source_edge(
    session: AsyncSession,
    *,
    parsed_user: ParsedUser,
    source_entity_id: int,
    source_entity_kind: str,
    source_kind: str,
    source_task_id: int,
) -> None:
    exists = await session.scalar(
        select(ParsedUserSource.id).where(
            ParsedUserSource.parsed_user_id == parsed_user.id,
            ParsedUserSource.source_entity_id == source_entity_id,
            ParsedUserSource.source_entity_kind == source_entity_kind,
            ParsedUserSource.source_kind == source_kind,
        )
    )
    if exists:
        return
    session.add(
        ParsedUserSource(
            parsed_user_id=parsed_user.id,
            source_entity_id=source_entity_id,
            source_entity_kind=source_entity_kind,
            source_kind=source_kind,
            source_task_id=source_task_id,
        )
    )


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
