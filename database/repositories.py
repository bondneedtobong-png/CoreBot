"""
Репозитории для CRUD операций с базой данных.
"""
import json
from datetime import datetime, timedelta
from utils.time import utcnow_naive
from typing import Any, Dict, Optional, List

from bot.config import (
    MAILING_BASE_UTC_OFFSET,
    NEUROCHAT_ENABLED as ENV_NEUROCHAT_ENABLED,
    OPENROUTER_API_KEY as ENV_OPENROUTER_API_KEY,
)
from utils.crypto_openrouter import decrypt_openrouter_key, encrypt_openrouter_key
from sqlalchemy import select, update, delete, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database.models import (
    Account, AccountStatus,
    Client, ClientClassCounter, ClientStatus,
    Group,
    InstanceSettings,
    Mailing, MailingAccountState, MailingStatus,
    MailingLog,
    MailingTestRecipient,
    NeuroActionLog,
    NeuroChatMessage,
    NeuroStopList,
    OutboundQueue,
    ProxyGroup,
    WarmupProfile,
    WarmupLog,
    Proxy, ProxyType,
    account_groups,
)


# ==================== Proxy Repository ====================

class ProxyRepository:
    """Репозиторий для работы с прокси."""
    
    @staticmethod
    async def create(
        session: AsyncSession,
        name: str,
        host: str,
        port: int,
        username: Optional[str] = None,
        password: Optional[str] = None,
        group_id: Optional[int] = None,
        proxy_type: ProxyType = ProxyType.SOCKS5,
    ) -> Proxy:
        """Создание нового прокси."""
        proxy = Proxy(
            name=name,
            host=host,
            port=port,
            username=username,
            password=password,
            group_id=group_id,
            proxy_type=proxy_type,
        )
        session.add(proxy)
        await session.commit()
        await session.refresh(proxy)
        return proxy
    
    @staticmethod
    async def get_by_id(session: AsyncSession, proxy_id: int) -> Optional[Proxy]:
        """Получение прокси по ID."""
        result = await session.execute(
            select(Proxy).where(Proxy.id == proxy_id)
        )
        return result.scalar_one_or_none()
    
    @staticmethod
    async def get_by_name(session: AsyncSession, name: str) -> Optional[Proxy]:
        """Получение прокси по имени."""
        result = await session.execute(
            select(Proxy).where(Proxy.name == name)
        )
        return result.scalar_one_or_none()
    
    @staticmethod
    async def get_all(session: AsyncSession) -> List[Proxy]:
        """Получение всех прокси."""
        result = await session.execute(
            select(Proxy).options(selectinload(Proxy.group)).order_by(Proxy.id)
        )
        return list(result.scalars().all())
    
    @staticmethod
    async def get_active(session: AsyncSession) -> List[Proxy]:
        """Получение активных рабочих прокси."""
        result = await session.execute(
            select(Proxy)
            .options(selectinload(Proxy.group))
            .where(Proxy.is_active == True, Proxy.is_working == True)
            .order_by(Proxy.id)
        )
        return list(result.scalars().all())

    @staticmethod
    async def get_free_by_group(session: AsyncSession, group_id: int) -> List[Proxy]:
        """Свободные прокси группы (не назначены аккаунтам)."""
        assigned_subq = select(Account.proxy_id).where(Account.proxy_id.isnot(None))
        result = await session.execute(
            select(Proxy)
            .where(
                Proxy.group_id == group_id,
                Proxy.is_active == True,
                Proxy.id.not_in(assigned_subq),
            )
            .order_by(Proxy.id)
        )
        return list(result.scalars().all())

    @staticmethod
    async def get_by_group(session: AsyncSession, group_id: int) -> List[Proxy]:
        result = await session.execute(
            select(Proxy).where(Proxy.group_id == group_id).order_by(Proxy.id)
        )
        return list(result.scalars().all())
    
    @staticmethod
    async def update_status(
        session: AsyncSession,
        proxy_id: int,
        is_working: bool,
        last_checked: datetime = None,
    ) -> bool:
        """Обновление статуса прокси."""
        if not last_checked:
            last_checked = utcnow_naive()
        await session.execute(
            update(Proxy)
            .where(Proxy.id == proxy_id)
            .values(
                is_working=is_working,
                last_checked=last_checked,
            )
        )
        await session.commit()
        return True

    @staticmethod
    async def update(
        session: AsyncSession,
        proxy_id: int,
        name: Optional[str] = None,
        host: Optional[str] = None,
        port: Optional[int] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ) -> bool:
        """Обновление данных прокси."""
        update_data = {}
        if name is not None:
            update_data["name"] = name
        if host is not None:
            update_data["host"] = host
        if port is not None:
            update_data["port"] = port
        if username is not None:
            update_data["username"] = username
        if password is not None:
            update_data["password"] = password

        if update_data:
            await session.execute(
                update(Proxy)
                .where(Proxy.id == proxy_id)
                .values(**update_data)
            )
            await session.commit()
        return True

    @staticmethod
    async def delete(session: AsyncSession, proxy_id: int) -> bool:
        """Удаление прокси."""
        await session.execute(delete(Proxy).where(Proxy.id == proxy_id))
        await session.commit()
        return True


# ==================== Instance settings (ключ OpenRouter) ====================


class InstanceSettingsRepository:
    """Одна строка instance_settings.id = 1."""

    _ROW_ID = 1
    _MAILING_TZ_MIN = -12
    _MAILING_TZ_MAX = 14

    @staticmethod
    def clamp_mailing_base_utc_offset(hours: int) -> int:
        return max(
            InstanceSettingsRepository._MAILING_TZ_MIN,
            min(InstanceSettingsRepository._MAILING_TZ_MAX, int(hours)),
        )

    @staticmethod
    async def get_row(session: AsyncSession) -> InstanceSettings:
        result = await session.execute(
            select(InstanceSettings).where(InstanceSettings.id == InstanceSettingsRepository._ROW_ID)
        )
        row = result.scalar_one_or_none()
        if row is None:
            row = InstanceSettings(id=InstanceSettingsRepository._ROW_ID)
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return row

    @staticmethod
    async def get_effective_openrouter_key(session: AsyncSession) -> Optional[str]:
        """Ключ из БД (расшифрованный) или из переменной окружения OPENROUTER_API_KEY."""
        row = await InstanceSettingsRepository.get_row(session)
        blob = (row.openrouter_key_ciphertext or "").strip()
        if blob:
            dec = decrypt_openrouter_key(blob)
            if dec:
                return dec
        env = (ENV_OPENROUTER_API_KEY or "").strip()
        return env or None

    @staticmethod
    async def set_openrouter_key(session: AsyncSession, plain_key: str) -> None:
        row = await InstanceSettingsRepository.get_row(session)
        row.openrouter_key_ciphertext = encrypt_openrouter_key(plain_key.strip())
        await session.commit()

    @staticmethod
    async def clear_openrouter_key(session: AsyncSession) -> None:
        row = await InstanceSettingsRepository.get_row(session)
        row.openrouter_key_ciphertext = None
        await session.commit()

    @staticmethod
    async def has_stored_key(session: AsyncSession) -> bool:
        row = await InstanceSettingsRepository.get_row(session)
        blob = (row.openrouter_key_ciphertext or "").strip()
        if not blob:
            return False
        return bool(decrypt_openrouter_key(blob))

    @staticmethod
    async def get_stored_mailing_base_utc_offset(session: AsyncSession) -> Optional[int]:
        row = await InstanceSettingsRepository.get_row(session)
        v = getattr(row, "mailing_base_utc_offset", None)
        if v is None:
            return None
        return int(v)

    @staticmethod
    async def get_effective_mailing_base_utc_offset(session: AsyncSession) -> int:
        stored = await InstanceSettingsRepository.get_stored_mailing_base_utc_offset(session)
        if stored is not None:
            return int(stored)
        return int(MAILING_BASE_UTC_OFFSET)

    @staticmethod
    async def set_mailing_base_utc_offset(session: AsyncSession, hours: int) -> int:
        h = InstanceSettingsRepository.clamp_mailing_base_utc_offset(hours)
        row = await InstanceSettingsRepository.get_row(session)
        row.mailing_base_utc_offset = h
        await session.commit()
        return h

    @staticmethod
    async def clear_mailing_base_utc_offset(session: AsyncSession) -> None:
        row = await InstanceSettingsRepository.get_row(session)
        row.mailing_base_utc_offset = None
        await session.commit()

    @staticmethod
    async def get_stored_neurochat_enabled(session: AsyncSession) -> Optional[bool]:
        row = await InstanceSettingsRepository.get_row(session)
        v = getattr(row, "neurochat_enabled", None)
        if v is None:
            return None
        return bool(v)

    @staticmethod
    async def get_effective_neurochat_enabled(session: AsyncSession) -> bool:
        stored = await InstanceSettingsRepository.get_stored_neurochat_enabled(session)
        if stored is not None:
            return bool(stored)
        return bool(ENV_NEUROCHAT_ENABLED)

    @staticmethod
    async def set_neurochat_enabled(session: AsyncSession, enabled: bool) -> None:
        row = await InstanceSettingsRepository.get_row(session)
        row.neurochat_enabled = bool(enabled)
        await session.commit()

    @staticmethod
    async def clear_neurochat_enabled(session: AsyncSession) -> None:
        row = await InstanceSettingsRepository.get_row(session)
        row.neurochat_enabled = None
        await session.commit()


# ==================== Account Repository ====================

class AccountRepository:
    """Репозиторий для работы с аккаунтами."""

    @staticmethod
    async def create(
        session: AsyncSession,
        phone: str,
        session_name: str,
        username: Optional[str] = None,
        proxy_id: Optional[int] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        bio: Optional[str] = None,
        avatar_path: Optional[str] = None,
        membership: "Membership" = None,
        status: AccountStatus = AccountStatus.INACTIVE,
        list_label: Optional[str] = None,
        **kwargs
    ) -> Account:
        """Создание нового аккаунта."""
        from database.models import Membership
        
        account = Account(
            phone=phone,
            session_name=session_name,
            username=username,
            proxy_id=proxy_id,
            first_name=first_name,
            last_name=last_name,
            bio=bio,
            avatar_path=avatar_path,
            membership=membership if membership else Membership.READY,
            status=status,
            list_label=list_label,
        )
        session.add(account)
        await session.commit()
        await session.refresh(account)
        return account
    
    @staticmethod
    async def get_by_id(session: AsyncSession, account_id: int) -> Optional[Account]:
        """Получение аккаунта по ID."""
        result = await session.execute(
            select(Account)
            .options(selectinload(Account.proxy))
            .where(Account.id == account_id)
        )
        return result.scalar_one_or_none()
    
    @staticmethod
    async def get_by_phone(session: AsyncSession, phone: str) -> Optional[Account]:
        """Получение аккаунта по номеру телефона."""
        result = await session.execute(
            select(Account).where(Account.phone == phone)
        )
        return result.scalar_one_or_none()
    
    @staticmethod
    async def get_by_session_name(session: AsyncSession, session_name: str) -> Optional[Account]:
        """Получение аккаунта по имени сессии."""
        result = await session.execute(
            select(Account).where(Account.session_name == session_name)
        )
        return result.scalar_one_or_none()
    
    @staticmethod
    async def get_all(session: AsyncSession) -> List[Account]:
        """Получение всех аккаунтов."""
        result = await session.execute(
            select(Account)
            .options(selectinload(Account.proxy))
            .order_by(Account.id)
        )
        return list(result.scalars().all())

    @staticmethod
    async def get_all_in_group(session: AsyncSession, group_id: int) -> List[Account]:
        """Все аккаунты, входящие в группу (для загрузки воркеров под рассылку)."""
        result = await session.execute(
            select(Account)
            .options(selectinload(Account.proxy))
            .where(
                Account.id.in_(
                    select(account_groups.c.account_id).where(
                        account_groups.c.group_id == group_id
                    )
                )
            )
            .order_by(Account.id)
        )
        return list(result.scalars().all())
    
    @staticmethod
    async def get_active(session: AsyncSession) -> List[Account]:
        """Получение активных аккаунтов (готовых к работе)."""
        result = await session.execute(
            select(Account)
            .options(selectinload(Account.proxy))
            .where(Account.status == AccountStatus.ACTIVE)
            .order_by(Account.last_activity)
        )
        return list(result.scalars().all())
    
    @staticmethod
    async def set_tags(
        session: AsyncSession,
        account_id: int,
        tags_str: str,
    ) -> bool:
        """
        Установка тегов аккаунту с нормализацией.
        Формат хранения: ",USA,Warmup,Main," (с запятыми по краям для точного LIKE).

        Args:
            session: DB-сессия
            account_id: ID аккаунта
            tags_str: Строка тегов через запятую (например "USA, Warmup, Main")

        Returns:
            bool: True если успешно
        """
        # Нормализация: split → strip → unique (с сохранением порядка) → wrap
        raw_tags = [t.strip() for t in tags_str.split(",") if t.strip()]
        seen = set()
        unique_tags = []
        for tag in raw_tags:
            tag_upper = tag.upper()
            if tag_upper not in seen:
                seen.add(tag_upper)
                unique_tags.append(tag)

        # Оборачиваем запятыми: ",USA,Warmup,Main,"
        normalized = "," + ",".join(unique_tags) + "," if unique_tags else ""

        await session.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(tags=normalized)
        )
        await session.commit()
        return True

    @staticmethod
    async def get_all_tags(session: AsyncSession) -> List[str]:
        """
        Получить все уникальные теги из всех аккаунтов.

        Returns:
            list[str]: Отсортированный список уникальных тегов
        """
        result = await session.execute(
            select(Account.tags).where(
                Account.tags.isnot(None),
                Account.tags != "",
            )
        )
        rows = result.scalars().all()

        all_tags = set()
        for tags_str in rows:
            # Разбираем: ",USA,Warmup," → ["USA", "Warmup"]
            parts = tags_str.strip(",").split(",")
            for part in parts:
                part = part.strip()
                if part:
                    all_tags.add(part)

        return sorted(all_tags)

    @staticmethod
    async def get_available_for_mailing(
        session: AsyncSession,
        tags_filter: Optional[List[str]] = None,
        group_id: Optional[int] = None,
        mailing_id: Optional[int] = None,
    ) -> List[Account]:
        """
        Получение аккаунтов доступных для рассылки.

        Args:
            session: DB-сессия
            tags_filter: Список тегов (OR). Учитывается только если group_id is None.
            group_id: Если задан — только аккаунты из этой группы (account_groups).
            mailing_id: Если задан — исключить аккаунты в кулдауне первой фазы этой рассылки.

        Returns:
            list[Account]: Доступные аккаунты
        """
        now = utcnow_naive()

        query = (
            select(Account)
            .options(selectinload(Account.proxy))
            .where(
                Account.status == AccountStatus.ACTIVE,
                Account.is_spam_blocked == False,
                # FloodWait истёк или отсутствует
                (Account.flood_wait_until == None) | (Account.flood_wait_until < now),
                # Лимит не превышен
                Account.messages_today < Account.daily_limit,
            )
        )

        if mailing_id is not None:
            cooled = select(MailingAccountState.account_id).where(
                MailingAccountState.mailing_id == mailing_id,
                MailingAccountState.cooldown_until > now,
            )
            query = query.where(~Account.id.in_(cooled))

        if group_id is not None:
            query = query.where(
                Account.id.in_(
                    select(account_groups.c.account_id).where(
                        account_groups.c.group_id == group_id
                    )
                )
            )
        elif tags_filter:
            tag_conditions = [
                Account.tags.like(f"%,{tag},%") for tag in tags_filter
            ]
            query = query.where(or_(*tag_conditions))

        query = query.order_by(Account.last_activity)
        result = await session.execute(query)
        return list(result.scalars().all())

    @staticmethod
    async def update_status(
        session: AsyncSession,
        account_id: int,
        status: AccountStatus,
    ) -> bool:
        """Обновление статуса аккаунта."""
        await session.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(status=status, updated_at=utcnow_naive())
        )
        await session.commit()
        return True
    
    @staticmethod
    async def set_proxy(
        session: AsyncSession,
        account_id: int,
        proxy_id: Optional[int],
    ) -> bool:
        """Привязка прокси к аккаунту."""
        await session.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(proxy_id=proxy_id, updated_at=utcnow_naive())
        )
        await session.commit()
        return True

    @staticmethod
    async def update_warmup_settings(
        session: AsyncSession,
        account_id: int,
        *,
        enabled: Optional[bool] = None,
        profile: Optional[str] = None,
        pause_reason: Optional[str] = None,
    ) -> bool:
        data: dict = {"updated_at": utcnow_naive()}
        if enabled is not None:
            data["warmup_enabled"] = enabled
            if enabled:
                data["warmup_pause_reason"] = None
                data["warmup_paused_until"] = None
        if profile is not None:
            data["warmup_profile"] = profile
        if pause_reason is not None:
            data["warmup_pause_reason"] = pause_reason
        await session.execute(
            update(Account).where(Account.id == account_id).values(**data)
        )
        await session.commit()
        return True

    @staticmethod
    async def update_warmup_for_accounts(
        session: AsyncSession,
        account_ids: List[int],
        *,
        enabled: bool,
        profile: Optional[str] = None,
    ) -> int:
        if not account_ids:
            return 0
        data: dict = {
            "warmup_enabled": enabled,
            "updated_at": utcnow_naive(),
        }
        if profile is not None:
            data["warmup_profile"] = profile
        result = await session.execute(
            update(Account).where(Account.id.in_(account_ids)).values(**data)
        )
        await session.commit()
        return int(result.rowcount or 0)

    @staticmethod
    async def update_warmup_for_group(
        session: AsyncSession,
        group_id: int,
        *,
        enabled: bool,
        profile: Optional[str] = None,
    ) -> int:
        result_ids = await session.execute(
            select(account_groups.c.account_id).where(account_groups.c.group_id == group_id)
        )
        ids = [int(x) for x in result_ids.scalars().all()]
        return await AccountRepository.update_warmup_for_accounts(
            session, ids, enabled=enabled, profile=profile
        )

    @staticmethod
    async def list_warmup_candidates(session: AsyncSession, limit: int = 50) -> List[Account]:
        now = utcnow_naive()
        result = await session.execute(
            select(Account)
            .options(selectinload(Account.proxy))
            .where(
                Account.warmup_enabled == True,
                Account.status == AccountStatus.ACTIVE,
                (Account.warmup_paused_until == None) | (Account.warmup_paused_until < now),
                (Account.warmup_next_run_at == None) | (Account.warmup_next_run_at < now),
            )
            .order_by(Account.warmup_next_run_at.asc().nullsfirst(), Account.id.asc())
            .limit(limit)
        )
        return list(result.scalars().all())

    @staticmethod
    async def mark_warmup_action(
        session: AsyncSession,
        account_id: int,
        next_run_at: datetime,
    ) -> bool:
        await session.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(
                warmup_actions_today=Account.warmup_actions_today + 1,
                warmup_last_action_at=utcnow_naive(),
                warmup_next_run_at=next_run_at,
                updated_at=utcnow_naive(),
            )
        )
        await session.commit()
        return True

    @staticmethod
    async def set_warmup_pause(
        session: AsyncSession,
        account_id: int,
        until: Optional[datetime],
        reason: Optional[str],
    ) -> bool:
        await session.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(
                warmup_paused_until=until,
                warmup_pause_reason=reason,
                updated_at=utcnow_naive(),
            )
        )
        await session.commit()
        return True

    @staticmethod
    async def reset_warmup_daily(session: AsyncSession) -> int:
        cutoff = utcnow_naive() - timedelta(days=1)
        result = await session.execute(
            update(Account)
            .where((Account.last_reset == None) | (Account.last_reset < cutoff))
            .values(
                warmup_actions_today=0,
            )
        )
        await session.commit()
        return int(result.rowcount or 0)

    @staticmethod
    async def count_by_proxy_id(session: AsyncSession, proxy_id: int) -> int:
        result = await session.execute(
            select(func.count(Account.id)).where(Account.proxy_id == proxy_id)
        )
        return int(result.scalar() or 0)

    @staticmethod
    async def count_using_proxy_group(session: AsyncSession, group_id: int) -> int:
        """Сколько аккаунтов привязано к прокси из группы."""
        result = await session.execute(
            select(func.count(Account.id))
            .select_from(Account)
            .join(Proxy, Proxy.id == Account.proxy_id)
            .where(Proxy.group_id == group_id)
        )
        return int(result.scalar() or 0)

    @staticmethod
    async def sample_labels_using_proxy_group(
        session: AsyncSession, group_id: int, limit: int = 5
    ) -> List[str]:
        """Краткие подписи аккаунтов (для предупреждения при удалении группы)."""
        result = await session.execute(
            select(Account.username, Account.phone)
            .join(Proxy, Proxy.id == Account.proxy_id)
            .where(Proxy.group_id == group_id)
            .limit(limit)
        )
        labels: List[str] = []
        for username, phone in result.all():
            if username:
                labels.append(f"@{username}")
            else:
                labels.append(phone or "?")
        return labels
    
    @staticmethod
    async def increment_stats(
        session: AsyncSession,
        account_id: int,
        sent: int = 0,
        failed: int = 0,
    ) -> bool:
        """Обновление статистики аккаунта."""
        await session.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(
                messages_sent=Account.messages_sent + sent,
                messages_failed=Account.messages_failed + failed,
                messages_today=Account.messages_today + sent,
                last_activity=utcnow_naive(),
            )
        )
        await session.commit()
        return True
    
    @staticmethod
    async def set_flood_wait(
        session: AsyncSession,
        account_id: int,
        until: datetime,
    ) -> bool:
        """Установка статуса FloodWait."""
        await session.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(
                status=AccountStatus.FLOOD_WAIT,
                flood_wait_until=until,
                updated_at=utcnow_naive(),
            )
        )
        await session.commit()
        return True
    
    @staticmethod
    async def clear_flood_wait(
        session: AsyncSession,
        account_id: int,
    ) -> bool:
        """Сброс FloodWait статуса."""
        await session.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(
                status=AccountStatus.ACTIVE,
                flood_wait_until=None,
                updated_at=utcnow_naive(),
            )
        )
        await session.commit()
        return True
    
    @staticmethod
    async def set_spam_block(
        session: AsyncSession,
        account_id: int,
        is_blocked: bool,
    ) -> bool:
        """Установка статуса спам-блока."""
        await session.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(
                is_spam_blocked=is_blocked,
                spam_check_date=utcnow_naive(),
                updated_at=utcnow_naive(),
            )
        )
        await session.commit()
        return True
    
    @staticmethod
    async def reset_daily_stats(
        session: AsyncSession,
        account_id: int,
    ) -> bool:
        """Сброс дневной статистики."""
        await session.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(
                messages_today=0,
                last_reset=utcnow_naive(),
            )
        )
        await session.commit()
        return True
    
    @staticmethod
    async def check_and_reset_daily_limit(
        session: AsyncSession,
        account: Account,
    ) -> bool:
        """Проверка и сброс дневного лимита если прошли сутки."""
        now = utcnow_naive()
        if account.last_reset:
            if now - account.last_reset > timedelta(days=1):
                await AccountRepository.reset_daily_stats(session, account.id)
                return True
        return False
    
    @staticmethod
    async def update_profile(
        session: AsyncSession,
        account_id: int,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        bio: Optional[str] = None,
    ) -> bool:
        """Обновление профиля аккаунта (имя, фамилия, bio)."""
        update_data = {"updated_at": utcnow_naive()}
        if first_name is not None:
            update_data["first_name"] = first_name
        if last_name is not None:
            update_data["last_name"] = last_name
        if bio is not None:
            update_data["bio"] = bio

        await session.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(**update_data)
        )
        await session.commit()
        return True

    @staticmethod
    async def update_username(
        session: AsyncSession,
        account_id: int,
        username: str,
    ) -> bool:
        """Обновление username аккаунта."""
        await session.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(username=username, updated_at=utcnow_naive())
        )
        await session.commit()
        return True

    @staticmethod
    async def update_list_label(
        session: AsyncSession,
        account_id: int,
        list_label: Optional[str],
    ) -> bool:
        """Локальная подпись аккаунта в списке бота (None — сброс)."""
        val = (list_label or "").strip() or None
        await session.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(list_label=val, updated_at=utcnow_naive())
        )
        await session.commit()
        return True

    @staticmethod
    async def set_membership(
        session: AsyncSession,
        account_id: int,
        membership: "Membership",
    ) -> bool:
        """Смена принадлежности аккаунта."""
        from database.models import Membership
        await session.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(membership=membership, updated_at=utcnow_naive())
        )
        await session.commit()
        return True

    @staticmethod
    async def set_ai_mode(
        session: AsyncSession,
        account_id: int,
        ai_mode: str,
    ) -> bool:
        """Переключение режима автоответа аккаунта: AI_ACTIVE | MANUAL.

        Используется веб-панелью; бот при следующем входящем перечитает
        Account из БД и применит новый режим без рестарта.
        """
        normalized = (ai_mode or "").strip().upper()
        if normalized not in ("AI_ACTIVE", "MANUAL"):
            return False
        await session.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(ai_mode=normalized, updated_at=utcnow_naive())
        )
        await session.commit()
        return True

    @staticmethod
    async def set_avatar(
        session: AsyncSession,
        account_id: int,
        avatar_path: Optional[str],
    ) -> bool:
        """
        Установка пути к локальной копии главной аватарки или сброс (None).
        None — нет актуального локального файла (аватар только в Telegram).
        """
        await session.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(avatar_path=avatar_path, updated_at=utcnow_naive())
        )
        await session.commit()
        return True

    @staticmethod
    async def delete(session: AsyncSession, account_id: int) -> bool:
        """Удаление аккаунта."""
        await session.execute(delete(Account).where(Account.id == account_id))
        await session.commit()
        return True


# ==================== Group Repository ====================

class GroupRepository:
    """Группы аккаунтов (many-to-many с Account)."""

    @staticmethod
    async def create(session: AsyncSession, name: str) -> Group:
        g = Group(name=name.strip())
        session.add(g)
        await session.commit()
        await session.refresh(g)
        return g

    @staticmethod
    async def get_by_id(session: AsyncSession, group_id: int) -> Optional[Group]:
        result = await session.execute(
            select(Group)
            .options(
                selectinload(Group.accounts).selectinload(Account.proxy),
            )
            .where(Group.id == group_id)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_all(session: AsyncSession) -> List[Group]:
        result = await session.execute(select(Group).order_by(Group.name))
        return list(result.scalars().all())

    @staticmethod
    async def get_member_account_ids(session: AsyncSession, group_id: int) -> set[int]:
        """ID аккаунтов, входящих в группу (таблица account_groups)."""
        result = await session.execute(
            select(account_groups.c.account_id).where(account_groups.c.group_id == group_id)
        )
        return {row[0] for row in result.all()}

    @staticmethod
    async def delete(session: AsyncSession, group_id: int) -> bool:
        g = await GroupRepository.get_by_id(session, group_id)
        if not g:
            return False
        await session.delete(g)
        await session.commit()
        return True

    @staticmethod
    async def count_empty(session: AsyncSession) -> int:
        """Сколько групп без аккаунтов (для чистки старых групп)."""
        used = select(account_groups.c.group_id)
        return int(
            await session.scalar(
                select(func.count(Group.id)).where(~Group.id.in_(used))
            )
            or 0
        )

    @staticmethod
    async def delete_empty(session: AsyncSession) -> int:
        """Удалить все группы без аккаунтов. Возвращает число удалённых."""
        used = select(account_groups.c.group_id)
        ids = list(
            (await session.execute(select(Group.id).where(~Group.id.in_(used)))).scalars().all()
        )
        if ids:
            await session.execute(delete(Group).where(Group.id.in_(ids)))
            await session.commit()
        return len(ids)

    @staticmethod
    async def add_account(session: AsyncSession, group_id: int, account_id: int) -> bool:
        g = await GroupRepository.get_by_id(session, group_id)
        a = await AccountRepository.get_by_id(session, account_id)
        if not g or not a:
            return False
        if a in g.accounts:
            return True
        g.accounts.append(a)
        await session.commit()
        return True

    @staticmethod
    async def remove_account(session: AsyncSession, group_id: int, account_id: int) -> bool:
        g = await GroupRepository.get_by_id(session, group_id)
        if not g:
            return False
        a = await AccountRepository.get_by_id(session, account_id)
        if not a or a not in g.accounts:
            return False
        g.accounts.remove(a)
        await session.commit()
        return True


class ProxyGroupRepository:
    """Группы прокси и автораздача свободных прокси."""

    @staticmethod
    async def create(session: AsyncSession, name: str) -> ProxyGroup:
        row = ProxyGroup(name=name.strip())
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row

    @staticmethod
    async def get_by_id(session: AsyncSession, group_id: int) -> Optional[ProxyGroup]:
        result = await session.execute(
            select(ProxyGroup).where(ProxyGroup.id == group_id)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_name(session: AsyncSession, name: str) -> Optional[ProxyGroup]:
        result = await session.execute(
            select(ProxyGroup).where(ProxyGroup.name == name.strip())
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_or_create(session: AsyncSession, name: str) -> ProxyGroup:
        existing = await ProxyGroupRepository.get_by_name(session, name)
        if existing:
            return existing
        return await ProxyGroupRepository.create(session, name)

    @staticmethod
    async def get_all(session: AsyncSession) -> List[ProxyGroup]:
        result = await session.execute(select(ProxyGroup).order_by(ProxyGroup.name))
        return list(result.scalars().all())

    @staticmethod
    async def list_with_usage(session: AsyncSession) -> List[tuple[ProxyGroup, int, int]]:
        groups = await ProxyGroupRepository.get_all(session)
        out: List[tuple[ProxyGroup, int, int]] = []
        for g in groups:
            total_res = await session.execute(
                select(func.count(Proxy.id)).where(Proxy.group_id == g.id)
            )
            used_res = await session.execute(
                select(func.count(Account.id))
                .join(Proxy, Proxy.id == Account.proxy_id)
                .where(Proxy.group_id == g.id)
            )
            total = int(total_res.scalar() or 0)
            used = int(used_res.scalar() or 0)
            out.append((g, used, total))
        return out

    @staticmethod
    async def acquire_next_free_proxy(session: AsyncSession, group_id: int) -> Optional[Proxy]:
        """Round-robin по свободным прокси группы."""
        group = await ProxyGroupRepository.get_by_id(session, group_id)
        if not group:
            return None
        free = await ProxyRepository.get_free_by_group(session, group_id)
        if not free:
            return None

        cursor = int(group.rr_cursor or 0)
        idx = cursor % len(free)
        selected = free[idx]

        await session.execute(
            update(ProxyGroup)
            .where(ProxyGroup.id == group_id)
            .values(rr_cursor=cursor + 1)
        )
        await session.commit()
        return selected

    @staticmethod
    async def delete_with_proxies(session: AsyncSession, group_id: int) -> bool:
        """
        Удалить все прокси группы и саму группу.
        Вызывать только если ни один аккаунт не использует прокси этой группы.
        """
        g = await ProxyGroupRepository.get_by_id(session, group_id)
        if not g:
            return False
        await session.execute(delete(Proxy).where(Proxy.group_id == group_id))
        await session.execute(delete(ProxyGroup).where(ProxyGroup.id == group_id))
        await session.commit()
        return True


class WarmupProfileRepository:
    @staticmethod
    async def get_all(session: AsyncSession) -> List[WarmupProfile]:
        result = await session.execute(
            select(WarmupProfile).order_by(WarmupProfile.name.asc())
        )
        return list(result.scalars().all())

    @staticmethod
    async def get_by_name(session: AsyncSession, name: str) -> Optional[WarmupProfile]:
        result = await session.execute(
            select(WarmupProfile).where(WarmupProfile.name == name)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_effective_for_account(session: AsyncSession, account: Account) -> Optional[WarmupProfile]:
        profile_name = (account.warmup_profile or "safe").strip() or "safe"
        row = await WarmupProfileRepository.get_by_name(session, profile_name)
        if row:
            return row
        return await WarmupProfileRepository.get_by_name(session, "safe")

    @staticmethod
    async def update_profile_settings(
        session: AsyncSession,
        name: str,
        *,
        base_delay_sec: Optional[float] = None,
        jitter_sec: Optional[float] = None,
        daily_action_limit: Optional[int] = None,
        target_chats_text: Optional[str] = None,
    ) -> bool:
        row = await WarmupProfileRepository.get_by_name(session, name)
        if not row:
            return False
        data: dict = {"updated_at": utcnow_naive()}
        if base_delay_sec is not None:
            data["base_delay_sec"] = float(base_delay_sec)
        if jitter_sec is not None:
            data["jitter_sec"] = float(jitter_sec)
        if daily_action_limit is not None:
            data["daily_action_limit"] = int(daily_action_limit)
        if target_chats_text is not None:
            data["target_chats_text"] = target_chats_text
        await session.execute(
            update(WarmupProfile).where(WarmupProfile.name == name).values(**data)
        )
        await session.commit()
        return True

    @staticmethod
    async def generate_unique_profile_name(session: AsyncSession, base_name: str) -> str:
        cleaned = (base_name or "").strip()
        if not cleaned:
            cleaned = "profile_copy"
        cleaned = cleaned.replace(" ", "_")
        candidate = cleaned
        idx = 1
        while True:
            row = await WarmupProfileRepository.get_by_name(session, candidate)
            if not row:
                return candidate
            candidate = f"{cleaned}_copy_{idx}"
            idx += 1

    @staticmethod
    async def create_profile_from_template(
        session: AsyncSession,
        source_name: str,
        new_name: str,
    ) -> Optional[WarmupProfile]:
        src = await WarmupProfileRepository.get_by_name(session, source_name)
        if not src:
            return None
        unique_name = await WarmupProfileRepository.generate_unique_profile_name(session, new_name)
        row = WarmupProfile(
            name=unique_name,
            base_delay_sec=src.base_delay_sec,
            jitter_sec=src.jitter_sec,
            daily_action_limit=src.daily_action_limit,
            target_chats_text=src.target_chats_text,
            enabled=src.enabled,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row

    @staticmethod
    async def overwrite_profile_from_template(
        session: AsyncSession,
        source_name: str,
        target_name: str,
    ) -> bool:
        src = await WarmupProfileRepository.get_by_name(session, source_name)
        if not src:
            return False
        target_norm = (target_name or "").strip().lower()
        if target_norm == "safe":
            return False
        target = await WarmupProfileRepository.get_by_name(session, target_name)
        if not target:
            return False
        await session.execute(
            update(WarmupProfile)
            .where(WarmupProfile.name == target.name)
            .values(
                base_delay_sec=src.base_delay_sec,
                jitter_sec=src.jitter_sec,
                daily_action_limit=src.daily_action_limit,
                target_chats_text=src.target_chats_text,
                enabled=src.enabled,
                updated_at=utcnow_naive(),
            )
        )
        await session.commit()
        return True


class WarmupLogRepository:
    @staticmethod
    async def create(
        session: AsyncSession,
        account_id: int,
        action: str,
        status: str = "ok",
        details: Optional[str] = None,
    ) -> None:
        session.add(
            WarmupLog(
                account_id=account_id,
                action=action,
                status=status,
                details=details,
            )
        )
        await session.commit()

    @staticmethod
    async def summary(session: AsyncSession) -> dict:
        total_enabled = await session.execute(
            select(func.count(Account.id)).where(Account.warmup_enabled == True)
        )
        paused = await session.execute(
            select(func.count(Account.id)).where(
                Account.warmup_enabled == True,
                Account.warmup_pause_reason.isnot(None),
            )
        )
        logs_today = await session.execute(
            select(func.count(WarmupLog.id)).where(
                WarmupLog.created_at >= utcnow_naive() - timedelta(days=1)
            )
        )
        return {
            "enabled": int(total_enabled.scalar() or 0),
            "paused": int(paused.scalar() or 0),
            "actions_24h": int(logs_today.scalar() or 0),
        }


# ==================== Client Repository ====================

DEFAULT_MAILING_AUDIENCE: Dict[str, Any] = {
    "client_status": "new",
    "include_classes": [],
    "exclude_classes": ["bl"],
}


class ClientRepository:
    """Репозиторий для работы с клиентами."""

    @staticmethod
    def parse_mailing_audience(mailing) -> Dict[str, Any]:
        """Разбор audience_filter_json рассылки с дефолтами."""
        raw = getattr(mailing, "audience_filter_json", None) or ""
        try:
            d = json.loads(raw) if str(raw).strip() else {}
        except Exception:
            d = {}
        base = dict(DEFAULT_MAILING_AUDIENCE)
        for k in ("client_status", "include_classes", "exclude_classes"):
            if k in d:
                base[k] = d[k]
        if base.get("client_status") not in ("new", "open"):
            base["client_status"] = "new"
        base["include_classes"] = [
            str(x).strip().lower()
            for x in (base.get("include_classes") or [])
            if str(x).strip()
        ]
        ex_raw = base.get("exclude_classes")
        if "exclude_classes" not in d:
            ex_raw = DEFAULT_MAILING_AUDIENCE["exclude_classes"]
        base["exclude_classes"] = [
            str(x).strip().lower()
            for x in (ex_raw or [])
            if str(x).strip()
        ]
        return base

    @staticmethod
    async def get_clients_for_mailing(
        session: AsyncSession,
        audience: Dict[str, Any],
        *,
        mailing_id: Optional[int] = None,
    ) -> List[Client]:
        """
        Очередь клиентов для рассылки.
        client_status: new — только NEW; open — NEW и CONTACTED.
        exclude_classes: не брать, если счётчик класса > 0.
        include_classes: нужны все перечисленные классы с count > 0.
        mailing_id: не возвращать клиентов с успешной отправкой в этой рассылке (повтор исключён).
        """
        st = audience.get("client_status") or "new"
        inc = audience.get("include_classes") or []
        exc_raw = audience.get("exclude_classes")
        if exc_raw is None:
            exc = ["bl"]
        else:
            exc = [str(x).strip().lower() for x in exc_raw if str(x).strip()]

        q = select(Client)
        if st == "new":
            q = q.where(Client.status == ClientStatus.NEW)
        elif st == "open":
            q = q.where(Client.status.in_([ClientStatus.NEW, ClientStatus.CONTACTED]))
        q = q.where(~Client.status.in_([ClientStatus.INVALID, ClientStatus.BLOCKED]))

        if mailing_id is not None:
            mailed = select(MailingLog.client_id).where(
                MailingLog.mailing_id == mailing_id,
                MailingLog.success == True,
            )
            q = q.where(~Client.id.in_(mailed))

        for key in exc:
            bad = select(ClientClassCounter.client_id).where(
                ClientClassCounter.class_key == key,
                ClientClassCounter.count > 0,
            )
            q = q.where(~Client.id.in_(bad))

        for key in inc:
            ok = select(ClientClassCounter.client_id).where(
                ClientClassCounter.class_key == key,
                ClientClassCounter.count > 0,
            )
            q = q.where(Client.id.in_(ok))

        q = q.order_by(Client.id)
        result = await session.execute(q)
        return list(result.scalars().all())

    @staticmethod
    async def _get_test_queue_clients(
        session: AsyncSession,
        mailing: Mailing,
    ) -> List[Client]:
        """
        Очередь тестовой рассылки.

        В пределах одного запуска (от mailing.started_at) исключаем клиентов,
        которым уже успешно отправили — чтобы не было повторной отправки
        в один и тот же чат с разных аккаунтов и чтобы цикл рассылки
        корректно завершился, когда список исчерпан.

        Между запусками started_at обновляется (см. update_status(RUNNING)),
        поэтому вся тест-аудитория автоматически снова становится eligible.
        """
        run_started = getattr(mailing, "started_at", None)
        sent_in_run = select(MailingLog.client_id).where(
            MailingLog.mailing_id == mailing.id,
            MailingLog.success.is_(True),
        )
        if run_started is not None:
            sent_in_run = sent_in_run.where(MailingLog.sent_at >= run_started)

        q = (
            select(Client)
            .join(MailingTestRecipient, MailingTestRecipient.client_id == Client.id)
            .where(MailingTestRecipient.mailing_id == mailing.id)
            .where(~Client.id.in_(sent_in_run))
            .where(~Client.status.in_([ClientStatus.INVALID, ClientStatus.BLOCKED]))
            .order_by(MailingTestRecipient.id)
        )
        result = await session.execute(q)
        return list(result.scalars().all())

    @staticmethod
    async def get_test_recipients_all(
        session: AsyncSession, mailing_id: int
    ) -> List[Client]:
        """
        Все тестовые получатели рассылки (без исключения уже отправленных) —
        для тестового режима, где КАЖДЫЙ аккаунт пишет КАЖДОМУ получателю.
        Невалидные/заблокированные исключаются.
        """
        q = (
            select(Client)
            .join(MailingTestRecipient, MailingTestRecipient.client_id == Client.id)
            .where(MailingTestRecipient.mailing_id == mailing_id)
            .where(~Client.status.in_([ClientStatus.INVALID, ClientStatus.BLOCKED]))
            .order_by(MailingTestRecipient.id)
        )
        result = await session.execute(q)
        return list(result.scalars().all())

    @staticmethod
    async def get_mailing_queue(session: AsyncSession, mailing: Mailing) -> List[Client]:
        """Очередь по audience_mode рассылки."""
        mode = (getattr(mailing, "audience_mode", None) or "classes").strip().lower()
        if mode == "test":
            return await ClientRepository._get_test_queue_clients(session, mailing)
        if mode == "new":
            parsed = ClientRepository.parse_mailing_audience(mailing)
            aud = {
                "client_status": "new",
                "include_classes": [],
                "exclude_classes": parsed["exclude_classes"],
            }
            return await ClientRepository.get_clients_for_mailing(
                session, aud, mailing_id=mailing.id
            )
        aud = ClientRepository.parse_mailing_audience(mailing)
        return await ClientRepository.get_clients_for_mailing(
            session, aud, mailing_id=mailing.id
        )

    @staticmethod
    async def count_mailing_queue(session: AsyncSession, mailing: Mailing) -> int:
        """Число клиентов в очереди (для превью)."""
        clients = await ClientRepository.get_mailing_queue(session, mailing)
        return len(clients)
    
    @staticmethod
    async def create(
        session: AsyncSession,
        username: str,
        status: ClientStatus = ClientStatus.NEW,
    ) -> Client:
        """Создание нового клиента."""
        client = Client(username=username, status=status)
        session.add(client)
        await session.commit()
        await session.refresh(client)
        return client
    
    @staticmethod
    async def create_many(
        session: AsyncSession,
        usernames: List[str],
    ) -> int:
        """Массовое создание клиентов. Возвращает количество добавленных."""
        existing = await ClientRepository.get_all_usernames(session)
        new_usernames = [u for u in usernames if u not in existing]
        
        clients = [Client(username=u) for u in new_usernames]
        session.add_all(clients)
        await session.commit()
        return len(new_usernames)
    
    @staticmethod
    async def get_by_id(session: AsyncSession, client_id: int) -> Optional[Client]:
        """Получение клиента по ID."""
        result = await session.execute(
            select(Client).where(Client.id == client_id)
        )
        return result.scalar_one_or_none()
    
    @staticmethod
    async def get_by_username(session: AsyncSession, username: str) -> Optional[Client]:
        """Получение клиента по username."""
        result = await session.execute(
            select(Client).where(Client.username == username)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_telegram_user_id(
        session: AsyncSession, telegram_user_id: int
    ) -> Optional[Client]:
        """Поиск клиента по Telegram user id."""
        result = await session.execute(
            select(Client).where(Client.telegram_user_id == telegram_user_id)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def set_telegram_user_id(
        session: AsyncSession,
        client_id: int,
        telegram_user_id: int,
    ) -> bool:
        """Сохраняет peer id в Telegram для изоляции диалогов нейрочата."""
        await session.execute(
            update(Client)
            .where(Client.id == client_id)
            .values(telegram_user_id=telegram_user_id)
        )
        await session.commit()
        return True
    
    @staticmethod
    async def get_all(session: AsyncSession) -> List[Client]:
        """Получение всех клиентов."""
        result = await session.execute(select(Client).order_by(Client.id))
        return list(result.scalars().all())
    
    @staticmethod
    async def get_all_usernames(session: AsyncSession) -> List[str]:
        """Получение всех username."""
        result = await session.execute(select(Client.username))
        return list(result.scalars().all())
    
    @staticmethod
    async def get_new(session: AsyncSession, limit: Optional[int] = None) -> List[Client]:
        """Получение новых клиентов (которым ещё не отправляли)."""
        query = select(Client).where(Client.status == ClientStatus.NEW).order_by(Client.id)
        if limit:
            query = query.limit(limit)
        result = await session.execute(query)
        return list(result.scalars().all())

    @staticmethod
    async def count_new(session: AsyncSession) -> int:
        """Сколько клиентов со статусом NEW (для оценки очереди рассылки)."""
        result = await session.execute(
            select(func.count()).select_from(Client).where(Client.status == ClientStatus.NEW)
        )
        return int(result.scalar_one() or 0)
    
    @staticmethod
    async def update_status(
        session: AsyncSession,
        client_id: int,
        status: ClientStatus,
    ) -> bool:
        """Обновление статуса клиента."""
        update_data = {"status": status}
        if status == ClientStatus.CONTACTED:
            update_data["last_contacted_at"] = utcnow_naive()
        
        await session.execute(
            update(Client)
            .where(Client.id == client_id)
            .values(**update_data)
        )
        await session.commit()
        return True
    
    @staticmethod
    async def count(session: AsyncSession) -> dict:
        """Получение статистики по клиентам."""
        result = await session.execute(
            select(Client.status, func.count(Client.id))
            .group_by(Client.status)
        )
        return {row[0].value: row[1] for row in result.all()}
    
    @staticmethod
    async def delete(session: AsyncSession, client_id: int) -> bool:
        """Удаление клиента."""
        await session.execute(delete(Client).where(Client.id == client_id))
        await session.commit()
        return True
    
    @staticmethod
    async def clear_all(session: AsyncSession) -> bool:
        """Очистка всей таблицы клиентов."""
        await session.execute(delete(Client))
        await session.commit()
        return True


class MailingTestRecipientRepository:
    """Тестовая аудитория: список username, привязанный к рассылке."""

    @staticmethod
    async def client_in_test_list(
        session: AsyncSession,
        mailing_id: int,
        client_id: int,
    ) -> bool:
        r = await session.execute(
            select(MailingTestRecipient.id).where(
                MailingTestRecipient.mailing_id == mailing_id,
                MailingTestRecipient.client_id == client_id,
            ).limit(1)
        )
        return r.scalar_one_or_none() is not None

    @staticmethod
    async def replace_from_usernames(
        session: AsyncSession,
        mailing_id: int,
        usernames: List[str],
    ) -> tuple[int, int]:
        """
        Полная замена списка. Возвращает (уникальных добавлено, дубликатов строк в файле).
        """
        seen: set[str] = set()
        unique: List[str] = []
        dups = 0
        for raw in usernames:
            u = (raw or "").strip().lstrip("@").lower()
            if not u:
                continue
            if u in seen:
                dups += 1
                continue
            seen.add(u)
            unique.append(u)

        await session.execute(
            delete(MailingTestRecipient).where(MailingTestRecipient.mailing_id == mailing_id)
        )
        await session.flush()

        for u in unique:
            existing = await ClientRepository.get_by_username(session, u)
            if existing is None:
                existing = await ClientRepository.create(session, u, status=ClientStatus.NEW)
            tr = MailingTestRecipient(
                mailing_id=mailing_id,
                username=u,
                client_id=existing.id,
            )
            session.add(tr)
        await session.commit()
        return len(unique), dups

    @staticmethod
    async def count_for_mailing(session: AsyncSession, mailing_id: int) -> int:
        r = await session.execute(
            select(func.count(MailingTestRecipient.id)).where(
                MailingTestRecipient.mailing_id == mailing_id
            )
        )
        return int(r.scalar() or 0)


class MailingAccountStateRepository:
    """Волна первых сообщений и кулдаун рассылки по аккаунту."""

    @staticmethod
    async def record_successful_first_message(
        session: AsyncSession,
        mailing_id: int,
        account_id: int,
        *,
        wave_limit: int,
        cooldown_hours: float,
    ) -> None:
        now = utcnow_naive()
        r = await session.execute(
            select(MailingAccountState).where(
                MailingAccountState.mailing_id == mailing_id,
                MailingAccountState.account_id == account_id,
            )
        )
        row = r.scalar_one_or_none()
        if row is None:
            row = MailingAccountState(
                mailing_id=mailing_id,
                account_id=account_id,
                sent_in_wave=0,
            )
            session.add(row)
            await session.flush()
        row.sent_in_wave = int(row.sent_in_wave or 0) + 1
        wl = max(1, int(wave_limit))
        if row.sent_in_wave >= wl:
            row.cooldown_until = now + timedelta(hours=float(cooldown_hours))
            row.sent_in_wave = 0
        await session.commit()


# ==================== Mailing Repository ====================

class MailingRepository:
    """Репозиторий для работы с рассылками."""
    
    @staticmethod
    async def create(
        session: AsyncSession,
        message_text: str,
        name: Optional[str] = None,
        delay_between_messages: float = 5.0,
        delay_between_accounts: float = 10.0,
        use_typing: bool = True,
        typing_delay: float = 3.0,
    ) -> Mailing:
        """Создание новой рассылки."""
        mailing = Mailing(
            name=name,
            message_text=message_text,
            delay_between_messages=delay_between_messages,
            delay_between_accounts=delay_between_accounts,
            status=MailingStatus.DRAFT,
        )
        session.add(mailing)
        await session.commit()
        await session.refresh(mailing)
        return mailing
    
    @staticmethod
    async def get_by_id(session: AsyncSession, mailing_id: int) -> Optional[Mailing]:
        """Получение рассылки по ID."""
        result = await session.execute(
            select(Mailing)
            .options(
                selectinload(Mailing.logs),
                selectinload(Mailing.target_group),
            )
            .where(Mailing.id == mailing_id)
        )
        return result.scalar_one_or_none()
    
    @staticmethod
    async def get_all(session: AsyncSession) -> List[Mailing]:
        """Получение всех рассылок."""
        result = await session.execute(
            select(Mailing).order_by(Mailing.created_at.desc())
        )
        return list(result.scalars().all())
    
    @staticmethod
    async def get_running(session: AsyncSession) -> Optional[Mailing]:
        """Получение активной рассылки."""
        result = await session.execute(
            select(Mailing).where(Mailing.status == MailingStatus.RUNNING)
        )
        return result.scalar_one_or_none()
    
    @staticmethod
    async def update_status(
        session: AsyncSession,
        mailing_id: int,
        status: MailingStatus,
    ) -> bool:
        """Обновление статуса рассылки."""
        update_data = {"status": status, "updated_at": utcnow_naive()}
        
        if status == MailingStatus.RUNNING:
            update_data["started_at"] = utcnow_naive()
        elif status in (MailingStatus.COMPLETED, MailingStatus.CANCELLED):
            update_data["completed_at"] = utcnow_naive()
        
        await session.execute(
            update(Mailing)
            .where(Mailing.id == mailing_id)
            .values(**update_data)
        )
        await session.commit()
        return True
    
    @staticmethod
    async def increment_stats(
        session: AsyncSession,
        mailing_id: int,
        sent: int = 0,
        failed: int = 0,
    ) -> bool:
        """Обновление статистики рассылки."""
        await session.execute(
            update(Mailing)
            .where(Mailing.id == mailing_id)
            .values(
                messages_sent=Mailing.messages_sent + sent,
                messages_failed=Mailing.messages_failed + failed,
            )
        )
        await session.commit()
        return True

    @staticmethod
    async def reset_stats_for_new_run(session: AsyncSession, mailing_id: int) -> bool:
        """Обнулить счётчики на старте запуска (накопление ведётся в логах / мониторинге)."""
        await session.execute(
            update(Mailing)
            .where(Mailing.id == mailing_id)
            .values(
                messages_sent=0,
                messages_failed=0,
                updated_at=utcnow_naive(),
            )
        )
        await session.execute(
            delete(MailingAccountState).where(MailingAccountState.mailing_id == mailing_id)
        )
        await session.commit()
        return True

    @staticmethod
    async def update_neuro(
        session: AsyncSession,
        mailing_id: int,
        neurochat_enabled: Optional[bool] = None,
        neuro_model: Optional[str] = None,
        neuro_sampling_json: Optional[str] = None,
    ) -> bool:
        """Обновление настроек нейрочата."""
        if (
            neurochat_enabled is None
            and neuro_model is None
            and neuro_sampling_json is None
        ):
            return False
        data: dict = {"updated_at": utcnow_naive()}
        if neurochat_enabled is not None:
            data["neurochat_enabled"] = neurochat_enabled
        if neuro_model is not None:
            data["neuro_model"] = neuro_model
        if neuro_sampling_json is not None:
            data["neuro_sampling_json"] = neuro_sampling_json
        await session.execute(
            update(Mailing).where(Mailing.id == mailing_id).values(**data)
        )
        await session.commit()
        return True

    @staticmethod
    async def delete(session: AsyncSession, mailing_id: int) -> bool:
        """Удаление рассылки."""
        await session.execute(delete(Mailing).where(Mailing.id == mailing_id))
        await session.commit()
        return True


# ==================== MailingLog Repository ====================

class MailingLogRepository:
    """Репозиторий для работы с логами рассылок."""
    
    @staticmethod
    async def create(
        session: AsyncSession,
        mailing_id: int,
        account_id: int,
        client_id: int,
        success: bool,
        error_message: Optional[str] = None,
        message_id: Optional[int] = None,
    ) -> MailingLog:
        """Создание записи лога."""
        log_entry = MailingLog(
            mailing_id=mailing_id,
            account_id=account_id,
            client_id=client_id,
            success=success,
            error_message=error_message,
            message_id=message_id,
        )
        session.add(log_entry)
        await session.commit()
        await session.refresh(log_entry)
        return log_entry
    
    @staticmethod
    async def get_by_mailing(
        session: AsyncSession,
        mailing_id: int,
        limit: Optional[int] = None,
    ) -> List[MailingLog]:
        """Получение логов по рассылке."""
        query = select(MailingLog).where(MailingLog.mailing_id == mailing_id)
        if limit:
            query = query.limit(limit)
        result = await session.execute(query)
        return list(result.scalars().all())

    @staticmethod
    async def success_counts_by_account(
        session: AsyncSession,
        mailing_id: int,
    ) -> dict[int, int]:
        """Число успешных отправок по account_id для рассылки (первая фаза)."""
        result = await session.execute(
            select(MailingLog.account_id, func.count())
            .where(MailingLog.mailing_id == mailing_id, MailingLog.success == True)
            .group_by(MailingLog.account_id)
        )
        return {int(row[0]): int(row[1]) for row in result.all()}
    
    @staticmethod
    async def get_errors(
        session: AsyncSession,
        mailing_id: int,
    ) -> List[MailingLog]:
        """Получение ошибок по рассылке."""
        result = await session.execute(
            select(MailingLog)
            .where(MailingLog.mailing_id == mailing_id, MailingLog.success == False)
        )
        return list(result.scalars().all())

    @staticmethod
    async def get_send_stats_by_account(
        session: AsyncSession,
        mailing_id: int,
    ) -> List[tuple[int, int, int, str]]:
        """
        Статистика отправок по аккаунтам для рассылки.

        Returns:
            Список (account_id, успешных, ошибок, подпись @username или телефон)
        """
        from collections import defaultdict

        logs = await MailingLogRepository.get_by_mailing(session, mailing_id)
        agg: dict[int, list[int]] = defaultdict(lambda: [0, 0])
        for log in logs:
            if log.success:
                agg[log.account_id][0] += 1
            else:
                agg[log.account_id][1] += 1

        out: List[tuple[int, int, int, str]] = []
        for aid in sorted(agg.keys()):
            ok, fail = agg[aid]
            acc = await AccountRepository.get_by_id(session, aid)
            if acc:
                label = acc.list_row_caption
            else:
                label = f"id{aid}"
            out.append((aid, ok, fail, label))
        return out

    @staticmethod
    async def get_mailing_for_neuro_reply(
        session: AsyncSession,
        account_id: int,
        client_id: int,
    ) -> Optional[Mailing]:
        """
        Последняя рассылка с включённым нейрочатом, где был успешный контакт
        этой пары аккаунт–клиент.
        """
        result = await session.execute(
            select(Mailing)
            .join(MailingLog, MailingLog.mailing_id == Mailing.id)
            .where(
                MailingLog.account_id == account_id,
                MailingLog.client_id == client_id,
                MailingLog.success == True,
                Mailing.neurochat_enabled == True,
            )
            .order_by(MailingLog.sent_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def has_successful_outbound_to_client(
        session: AsyncSession,
        account_id: int,
        client_id: int,
    ) -> bool:
        """Был ли хотя бы один успешный исходящий контакт по паре аккаунт–клиент (любая рассылка)."""
        result = await session.execute(
            select(MailingLog.id).where(
                MailingLog.account_id == account_id,
                MailingLog.client_id == client_id,
                MailingLog.success == True,
            ).limit(1)
        )
        return result.scalar_one_or_none() is not None


# ==================== Neuro chat history ====================


class NeuroChatRepository:
    """История нейрочата: ключ (account_id, peer_user_id), не смешивать диалоги."""

    @staticmethod
    async def append(
        session: AsyncSession,
        account_id: int,
        peer_user_id: int,
        role: str,
        content: str,
    ) -> None:
        session.add(
            NeuroChatMessage(
                account_id=account_id,
                peer_user_id=peer_user_id,
                role=role,
                content=content,
            )
        )
        await session.commit()
        await NeuroChatRepository._trim(session, account_id, peer_user_id)

    @staticmethod
    async def _trim(
        session: AsyncSession,
        account_id: int,
        peer_user_id: int,
    ) -> None:
        from bot.config import NEURO_HISTORY_LIMIT

        limit = NEURO_HISTORY_LIMIT
        result = await session.execute(
            select(NeuroChatMessage.id)
            .where(
                NeuroChatMessage.account_id == account_id,
                NeuroChatMessage.peer_user_id == peer_user_id,
            )
            .order_by(NeuroChatMessage.created_at.asc())
        )
        ids = list(result.scalars().all())
        if len(ids) <= limit:
            return
        to_delete = ids[: len(ids) - limit]
        await session.execute(
            delete(NeuroChatMessage).where(NeuroChatMessage.id.in_(to_delete))
        )
        await session.commit()

    @staticmethod
    async def get_messages_for_llm(
        session: AsyncSession,
        account_id: int,
        peer_user_id: int,
    ) -> list[dict[str, str]]:
        """Последние N сообщений в хронологическом порядке для OpenRouter."""
        from bot.config import NEURO_HISTORY_LIMIT

        result = await session.execute(
            select(NeuroChatMessage)
            .where(
                NeuroChatMessage.account_id == account_id,
                NeuroChatMessage.peer_user_id == peer_user_id,
            )
            .order_by(NeuroChatMessage.created_at.desc())
            .limit(NEURO_HISTORY_LIMIT)
        )
        rows = list(reversed(list(result.scalars().all())))
        out: list[dict[str, str]] = []
        for r in rows:
            role = r.role if r.role in ("user", "assistant") else "user"
            out.append({"role": role, "content": r.content})
        return out


class NeuroActionRepository:
    """События команд нейрочата: [SEND_LINK], [STOP], [ACCEPT], [DECLINE], [HATER]."""

    @staticmethod
    async def create(
        session: AsyncSession,
        mailing_id: int,
        account_id: int,
        client_id: int,
        action: str,
    ) -> None:
        session.add(
            NeuroActionLog(
                mailing_id=mailing_id,
                account_id=account_id,
                client_id=client_id,
                action=action,
            )
        )
        await session.commit()

    @staticmethod
    async def count_by_action(
        session: AsyncSession,
        mailing_id: int,
    ) -> dict[str, int]:
        result = await session.execute(
            select(NeuroActionLog.action, func.count(NeuroActionLog.id))
            .where(NeuroActionLog.mailing_id == mailing_id)
            .group_by(NeuroActionLog.action)
        )
        return {str(row[0]): int(row[1]) for row in result.all()}


class NeuroStopRepository:
    """STOP-лист нейрочата: блокировка диалога по account_id + client_id."""

    @staticmethod
    async def is_blocked(
        session: AsyncSession,
        account_id: int,
        client_id: int,
    ) -> bool:
        result = await session.execute(
            select(NeuroStopList.id).where(
                NeuroStopList.account_id == account_id,
                NeuroStopList.client_id == client_id,
            )
        )
        return result.scalar_one_or_none() is not None

    @staticmethod
    async def add(
        session: AsyncSession,
        mailing_id: int,
        account_id: int,
        client_id: int,
    ) -> None:
        result = await session.execute(
            select(NeuroStopList).where(
                NeuroStopList.account_id == account_id,
                NeuroStopList.client_id == client_id,
            )
        )
        row = result.scalar_one_or_none()
        if row:
            await session.execute(
                update(NeuroStopList)
                .where(NeuroStopList.id == row.id)
                .values(mailing_id=mailing_id, created_at=utcnow_naive())
            )
        else:
            session.add(
                NeuroStopList(
                    mailing_id=mailing_id,
                    account_id=account_id,
                    client_id=client_id,
                )
            )
        await session.commit()

    @staticmethod
    async def remove(
        session: AsyncSession,
        account_id: int,
        client_id: int,
    ) -> bool:
        result = await session.execute(
            delete(NeuroStopList).where(
                NeuroStopList.account_id == account_id,
                NeuroStopList.client_id == client_id,
            )
        )
        await session.commit()
        return bool(result.rowcount and result.rowcount > 0)

    @staticmethod
    async def list_for_mailing(
        session: AsyncSession,
        mailing_id: int,
        limit: int = 20,
    ) -> list[tuple[int, int, str, str]]:
        """
        Возвращает список: (account_id, client_id, username, created_at_iso).
        """
        result = await session.execute(
            select(NeuroStopList, Client)
            .join(Client, Client.id == NeuroStopList.client_id)
            .where(NeuroStopList.mailing_id == mailing_id)
            .order_by(NeuroStopList.created_at.desc())
            .limit(limit)
        )
        out: list[tuple[int, int, str, str]] = []
        for stop_row, client in result.all():
            out.append(
                (
                    int(stop_row.account_id),
                    int(stop_row.client_id),
                    str(client.username or ""),
                    stop_row.created_at.isoformat() if stop_row.created_at else "",
                )
            )
        return out


class OutboundQueueRepository:
    """
    Очередь ручных исходящих сообщений из веб-панели.

    Вызывается:
      * веб-панелью (enqueue)               — добавляет 'pending'
      * outbound consumer воркером бота      — забирает 'pending' и шлёт через Telethon
    """

    @staticmethod
    async def enqueue(
        session: AsyncSession,
        *,
        account_id: int,
        peer_user_id: int,
        text: str,
        client_id: Optional[int] = None,
        requested_by: Optional[str] = None,
    ) -> OutboundQueue:
        row = OutboundQueue(
            account_id=int(account_id),
            peer_user_id=int(peer_user_id),
            client_id=int(client_id) if client_id is not None else None,
            text=text,
            status="pending",
            requested_by=(requested_by or None),
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row

    @staticmethod
    async def fetch_pending_batch(
        session: AsyncSession,
        limit: int = 20,
    ) -> list[OutboundQueue]:
        now = utcnow_naive()
        result = await session.execute(
            select(OutboundQueue)
            .where(
                OutboundQueue.status == "pending",
                or_(
                    OutboundQueue.next_attempt_at.is_(None),
                    OutboundQueue.next_attempt_at <= now,
                ),
            )
            .order_by(OutboundQueue.created_at.asc(), OutboundQueue.id.asc())
            .limit(int(limit))
        )
        return list(result.scalars().all())

    @staticmethod
    async def mark_sent(
        session: AsyncSession,
        queue_id: int,
        telegram_message_id: Optional[int],
    ) -> None:
        await session.execute(
            update(OutboundQueue)
            .where(OutboundQueue.id == int(queue_id))
            .values(
                status="sent",
                telegram_message_id=int(telegram_message_id) if telegram_message_id else None,
                sent_at=utcnow_naive(),
                error=None,
                next_attempt_at=None,
            )
        )
        await session.commit()

    @staticmethod
    async def mark_failed(
        session: AsyncSession,
        queue_id: int,
        error: str,
    ) -> None:
        await session.execute(
            update(OutboundQueue)
            .where(OutboundQueue.id == int(queue_id))
            .values(
                status="failed",
                error=(error or "")[:1000],
                sent_at=utcnow_naive(),
                next_attempt_at=None,
            )
        )
        await session.commit()

    @staticmethod
    async def reschedule(
        session: AsyncSession,
        queue_id: int,
        *,
        delay_sec: float,
        last_error: Optional[str] = None,
    ) -> None:
        """
        Не падаем сразу: помечаем как pending с next_attempt_at = now + delay,
        чтобы fetch_pending_batch не выбирал её до истечения паузы.
        """
        next_at = utcnow_naive() + timedelta(seconds=max(0.0, float(delay_sec)))
        await session.execute(
            update(OutboundQueue)
            .where(OutboundQueue.id == int(queue_id))
            .values(
                status="pending",
                error=(last_error or "")[:1000] if last_error else None,
                attempts=OutboundQueue.attempts + 1,
                next_attempt_at=next_at,
            )
        )
        await session.commit()

    @staticmethod
    async def cancel(session: AsyncSession, queue_id: int) -> bool:
        res = await session.execute(
            update(OutboundQueue)
            .where(
                OutboundQueue.id == int(queue_id),
                OutboundQueue.status.in_(["pending", "failed"]),
            )
            .values(status="cancelled", next_attempt_at=None)
        )
        await session.commit()
        return (res.rowcount or 0) > 0

    @staticmethod
    async def retry(session: AsyncSession, queue_id: int) -> bool:
        res = await session.execute(
            update(OutboundQueue)
            .where(
                OutboundQueue.id == int(queue_id),
                OutboundQueue.status.in_(["failed", "cancelled"]),
            )
            .values(
                status="pending",
                error=None,
                attempts=0,
                next_attempt_at=None,
                sent_at=None,
            )
        )
        await session.commit()
        return (res.rowcount or 0) > 0

    @staticmethod
    async def list_recent_for_dialog(
        session: AsyncSession,
        account_id: int,
        peer_user_id: int,
        limit: int = 50,
    ) -> list[OutboundQueue]:
        result = await session.execute(
            select(OutboundQueue)
            .where(
                OutboundQueue.account_id == int(account_id),
                OutboundQueue.peer_user_id == int(peer_user_id),
            )
            .order_by(OutboundQueue.created_at.desc())
            .limit(int(limit))
        )
        return list(result.scalars().all())
