"""Persistence for profile presets and randomization pools."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import ProfilePoolItem, ProfileTemplate
from database.sqlite_pragmas import commit_with_busy_retry

POOL_CATEGORIES = {"m", "f", "u"}
POOL_KINDS = {"name", "bio", "photo"}


async def list_templates(session: AsyncSession) -> list[ProfileTemplate]:
    result = await session.execute(select(ProfileTemplate).order_by(ProfileTemplate.id.desc()))
    return list(result.scalars().all())


async def get_template(session: AsyncSession, template_id: int) -> ProfileTemplate | None:
    return await session.get(ProfileTemplate, template_id)


async def create_template(
    session: AsyncSession, *, name: str, first_name: str,
    last_name: str | None, bio: str, photo_file: str | None,
) -> ProfileTemplate:
    template = ProfileTemplate(
        name=name, first_name=first_name, last_name=last_name,
        bio=bio, photo_file=photo_file,
    )
    session.add(template)
    await commit_with_busy_retry(session, op_name="profile-template-create")
    await session.refresh(template)
    return template


async def pool_counts(session: AsyncSession, category: str | None = None) -> dict[str, int]:
    if category is not None and category not in POOL_CATEGORIES:
        raise ValueError("Неизвестная категория профиля")
    query = select(ProfilePoolItem.kind, func.count(ProfilePoolItem.id))
    if category is not None:
        query = query.where(ProfilePoolItem.category == category)
    result = await session.execute(
        query.group_by(ProfilePoolItem.kind)
    )
    return {kind: int(count) for kind, count in result.all()}


async def add_pool_batch(
    session: AsyncSession, values_by_kind: dict[str, list[str]], *, category: str = "u",
) -> dict[str, int]:
    if category not in POOL_CATEGORIES:
        raise ValueError("Неизвестная категория профиля")
    counts: dict[str, int] = {}
    for kind, values in values_by_kind.items():
        if kind not in POOL_KINDS:
            raise ValueError("Неизвестный тип данных профиля")
        unique = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if len(unique) > 1000:
            raise ValueError("Не больше 1000 элементов каждого типа за загрузку")
        existing: set[str] = set()
        for start in range(0, len(unique), 500):
            rows = await session.scalars(select(ProfilePoolItem.value).where(
                ProfilePoolItem.kind == kind,
                ProfilePoolItem.category == category,
                ProfilePoolItem.value.in_(unique[start:start + 500]),
            ))
            existing.update(rows.all())
        new_values = [value for value in unique if value not in existing]
        session.add_all(
            ProfilePoolItem(kind=kind, category=category, value=value) for value in new_values
        )
        counts[kind] = len(new_values)
    await commit_with_busy_retry(session, op_name="profile-pool-add")
    return counts


async def list_pool_items(
    session: AsyncSession, kind: str, category: str, *, limit: int = 20, offset: int = 0,
) -> tuple[list[ProfilePoolItem], int]:
    if kind not in POOL_KINDS or category not in POOL_CATEGORIES:
        raise ValueError("Неизвестный набор профиля")
    total = await session.scalar(select(func.count(ProfilePoolItem.id)).where(
        ProfilePoolItem.kind == kind, ProfilePoolItem.category == category,
    ))
    rows = await session.execute(
        select(ProfilePoolItem).where(
            ProfilePoolItem.kind == kind, ProfilePoolItem.category == category,
        ).order_by(ProfilePoolItem.id.desc()).limit(min(max(limit, 1), 20)).offset(max(offset, 0))
    )
    return list(rows.scalars().all()), int(total or 0)


async def random_profile(session: AsyncSession, category: str = "u") -> dict[str, str | None]:
    if category not in POOL_CATEGORIES:
        raise ValueError("Неизвестная категория профиля")
    result: dict[str, str | None] = {}
    for kind in ("name", "bio", "photo"):
        row = await session.scalar(
            select(ProfilePoolItem.value)
            .where(ProfilePoolItem.kind == kind, ProfilePoolItem.category == category)
            .order_by(func.random())
            .limit(1)
        )
        result[kind] = row
    return result
