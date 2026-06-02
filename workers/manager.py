"""
Worker Manager — управление рабочими аккаунтами и рассылками.
"""
import asyncio
import html
import json
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List, Callable, Any, Union
from pathlib import Path
from dotenv import load_dotenv

import sqlalchemy
from sqlalchemy import select
from telethon import TelegramClient, errors
from telethon.tl.functions.messages import SetTypingRequest
from telethon.tl.types import PeerUser, SendMessageTypingAction
from telethon.errors import FloodWaitError, PeerFloodError
from telethon.network.connection.tcpabridged import ConnectionTcpAbridged

from database.models import Account, AccountStatus, ClientStatus, Proxy, ProxyType, Mailing, MailingStatus
from bot.config import (
    BANDWIDTH_SKIP_PROFILE_ENRICH,
    BANDWIDTH_SKIP_SPAMBOT_CHECK,
    MAILING_BASE_UTC_OFFSET,
)
from database.repositories import (
    AccountRepository,
    GroupRepository,
    InstanceSettingsRepository,
    MailingAccountStateRepository,
    ProxyRepository,
    ClientRepository,
    MailingRepository,
    MailingLogRepository,
)
from database.crm_repositories import ClientMailSessionRepository
from database.repository import db
from database.session import session_scope
from utils.logger import log
from utils.links import normalize_public_link, plain_text_to_telegram_link_message
from utils.telemetry import telemetry_emitter

# Загружаем переменные окружения
load_dotenv()

def _mailing_jittered_delay(base: float, smart: bool) -> float:
    """
    Умная задержка: случайный разброс вокруг базового интервала (не шаблонные паузы).
    """
    b = max(0.0, float(base))
    if not smart:
        return b
    if b <= 0:
        return 0.0
    return max(1.0, b * random.uniform(0.72, 1.32))


def _account_log_label(account: Account) -> str:
    """Краткая метка аккаунта для логов рассылки."""
    phone = (getattr(account, "phone", None) or "").strip() or "—"
    un = (getattr(account, "username", None) or "").strip()
    un_lbl = f"@{un}" if un else "без @username"
    fn = (getattr(account, "first_name", None) or "").strip()
    tail = f" ({fn})" if fn else ""
    return f"id={account.id} {phone} {un_lbl}{tail}"


def _mailing_send_failure_hint(error: Optional[str]) -> str:
    """Человекочитаемая подсказка по тексту ошибки Telethon/воркера."""
    if not error:
        return "Проверьте доступность клиента и статус аккаунта в мониторинге."
    e = error
    if "PEER_FLOOD" in e or "PeerFlood" in e:
        return (
            "Лимит Telegram на первые исходящие в новые диалоги с этого аккаунта (не то же, что @SpamBot). "
            "Подождите от нескольких часов до суток, увеличьте задержку между сообщениями и между аккаунтами; "
            "не спамьте повторно в тот же чат — лимит только усиливается. При необходимости смените аккаунт."
        )
    if "FloodWait" in e or "FLOOD_WAIT" in e:
        return "FloodWait: дождитесь окончания блокировки и увеличьте паузы в настройках рассылки."
    if "No user has" in e and "username" in e:
        return "Пользователь с таким @username не найден — пометьте клиента невалидным или проверьте ник."
    return "См. текст ошибки; часто помогает увеличение задержек и снижение частоты первых сообщений."


def _fmt_utc_offset(hours: int) -> str:
    sign = "+" if hours >= 0 else "-"
    return f"UTC{sign}{abs(int(hours)):02d}:00"


# Глобальные API credentials (кэшируются)
_api_initialized = False
_api_id = 0
_api_hash = ""
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _get_api_credentials():
    """
    Получение API ID и Hash из переменных окружения.
    Кэшируется после первого вызова.
    """
    global _api_initialized, _api_id, _api_hash

    if not _api_initialized:
        _api_id = int(os.getenv("API_ID", "0"))
        _api_hash = os.getenv("API_HASH", "")

        if not _api_id or not _api_hash:
            raise ValueError("В .env файле отсутствуют API_ID или API_HASH")

        _api_initialized = True
        log.info(f"✅ Worker Manager: API credentials загружены (ID={_api_id})")

    return _api_id, _api_hash


def get_telethon_proxy_dict(proxy: Proxy) -> Optional[dict]:
    """
    Преобразование объекта Proxy в словарь для Telethon.

    Telethon принимает прокси в формате:
    {
        'proxy_type': 'socks5' | 'http' | 'mtproto',
        'addr': 'host',
        'port': 1080,
        'username': 'user',   # опционально
        'password': 'pass',   # опционально
        'rdns': True
    }

    Args:
        proxy: Объект Proxy из БД

    Returns:
        dict для Telethon или None если прокси None
    """
    if not proxy:
        return None

    # Определяем тип прокси для Telethon (строкой, не enum!)
    if proxy.proxy_type == ProxyType.SOCKS5:
        proxy_type_str = 'socks5'
    elif proxy.proxy_type == ProxyType.HTTP:
        proxy_type_str = 'http'
    elif proxy.proxy_type == ProxyType.MTProxy:
        proxy_type_str = 'mtproto'
    else:
        proxy_type_str = 'socks5'  # по умолчанию

    result = {
        'proxy_type': proxy_type_str,
        'addr': proxy.host,
        'port': proxy.port,
        'rdns': True,  # DNS resolution через прокси
    }

    if proxy.username and proxy.password:
        result['username'] = proxy.username
        result['password'] = proxy.password

    return result


async def verify_session_via_proxy(
    session_path: str | Path,
    proxy: Optional[Proxy],
) -> tuple[bool, str]:
    """
    Проверка, что .session авторизован при подключении через указанный прокси.
    Без прокси — (True, "") (проверка не выполняется).
    """
    if proxy is None:
        return True, ""
    session_path = Path(session_path)
    proxy_config = get_telethon_proxy_dict(proxy)
    if not proxy_config:
        return False, "некорректный прокси"
    client: Optional[TelegramClient] = None
    try:
        client = create_telethon_client(session_path, proxy_config)
        await client.connect()
        if not await client.is_user_authorized():
            return False, "сессия не авторизована через прокси"
        await client.get_me()
        return True, ""
    except Exception as e:
        return False, str(e)[:400]
    finally:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass


def create_telethon_client(
    session_path: str | Path,
    proxy: Optional[dict] = None,
) -> TelegramClient:
    """
    Создание TelegramClient с правильными API credentials.

    Args:
        session_path: Путь к .session файлу
        proxy: Словарь прокси от get_telethon_proxy_dict() (опционально)

    Returns:
        TelegramClient: Настроенный клиент
    """
    api_id, api_hash = _get_api_credentials()

    return TelegramClient(
        str(session_path),
        api_id=api_id,
        api_hash=api_hash,
        proxy=proxy,
        connection=ConnectionTcpAbridged,
        auto_reconnect=True,
        raise_last_call_error=True,
    )


class Worker:
    """
    Рабочий аккаунт (Telethon клиент).
    """
    
    def __init__(self, account: Account, session_path: Path, proxy: Optional[Proxy] = None):
        self.account = account
        self.session_path = session_path
        self.proxy = proxy
        self.client: Optional[TelegramClient] = None
        self.is_connected = False
        self.is_running = False
        
    async def connect(self, *, quiet: bool = False) -> bool:
        """
        Подключение аккаунта с прокси (если привязан).

        quiet: если True — неавторизованная сессия логируется кратко (для массового connect после рассылки).

        Returns:
            bool: True если успешно подключился
        """
        try:
            # Создаём словарь прокси для Telethon (если прокси привязан)
            proxy_config = get_telethon_proxy_dict(self.proxy)

            # Логирование перед подключением
            log.info(f"🔌 Подключение аккаунта {self.account.id} ({self.account.session_name})...")
            if proxy_config:
                log.info(
                    f"   🌐 Подключение через прокси "
                    f"{self.proxy.name} ({self.proxy.host}:{self.proxy.port})"
                )
            else:
                log.warning(
                    f"   ⚠️ Подключение без прокси (не рекомендуется для аккаунта {self.account.id})"
                )

            # Создание клиента через хелпер-функцию
            self.client = create_telethon_client(
                session_path=self.session_path,
                proxy=proxy_config,
            )

            # SQLite .session на Windows иногда даёт «database is locked» при быстром подряд connect
            last_err: Optional[Exception] = None
            for attempt in range(5):
                try:
                    await self.client.connect()
                    last_err = None
                    break
                except Exception as e:
                    err_s = str(e).lower()
                    if "database is locked" in err_s or "locked" in err_s:
                        last_err = e
                        log.warning(
                            f"   ⏳ SQLite session занят (попытка {attempt + 1}/5), "
                            f"пауза {0.5 * (attempt + 1):.1f} с…"
                        )
                        try:
                            await self.client.disconnect()
                        except Exception:
                            pass
                        await asyncio.sleep(0.5 * (attempt + 1))
                        self.client = create_telethon_client(
                            session_path=self.session_path,
                            proxy=proxy_config,
                        )
                        continue
                    raise
            if last_err is not None:
                raise last_err

            # Проверка авторизации
            is_auth = await self.client.is_user_authorized()
            log.info(f"   📊 Сессия {self.account.session_name}: is_user_authorized() = {is_auth}")

            if is_auth:
                self.is_connected = True
                me = await self.client.get_me()
                log.info(f"✅ Аккаунт подключён: {me.username or me.phone} (ID: {me.id})")
                await telemetry_emitter.emit_event(
                    "info",
                    "worker_connect",
                    f"Account connected: {self.account.id}",
                    payload={"account_id": self.account.id},
                )

                # Обновляем информацию о профиле в БД
                await self._update_account_info(me)

                try:
                    self._register_neuro_handler()
                except Exception as e:
                    # Нейро-хендлер не должен валить подключение аккаунта.
                    log.error(
                        f"⚠️ Нейро-хендлер не подключён для аккаунта {self.account.id}: {e}"
                    )
                return True
            else:
                if quiet:
                    log.debug(
                        f"Сессия неавторизована, пропуск: account_id={self.account.id} "
                        f"({self.session_path.name})"
                    )
                else:
                    log.warning(f"❌ Сессия неавторизована: {self.account.session_name}")
                    log.warning(f"   📁 Session path: {self.session_path}")
                    log.warning(f"   📁 Session exists: {self.session_path.exists()}")
                    log.warning(
                        f"   ⚠️ Нужно заново загрузить Tdata или авторизовать аккаунт."
                    )

                await self.client.disconnect()
                return False

        except Exception as e:
            log.error(f"❌ Ошибка подключения аккаунта {self.account.id}: {e}")
            import traceback
            log.error(traceback.format_exc())
            await telemetry_emitter.emit_event(
                "error",
                "worker_connect",
                f"Account connect failed: {self.account.id}",
                payload={"account_id": self.account.id, "error": str(e)},
            )
            self.is_connected = False
            return False

    async def _update_account_info(self, me):
        """
        Обновление информации об аккаунте в БД после подключения.

        Args:
            me: Объект пользователя от get_me()
        """
        try:
            async with session_scope() as session:
                    bio = None
                    if not BANDWIDTH_SKIP_PROFILE_ENRICH:
                        # Полная карточка профиля требует дополнительный запрос.
                        full_user = await self.client.get_entity(me.id)
                        if hasattr(full_user, 'about'):
                            bio = full_user.about

                    await AccountRepository.update_profile(
                        session,
                        self.account.id,
                        first_name=me.first_name,
                        last_name=me.last_name,
                        bio=bio,
                    )

                    # Обновляем username если есть
                    if me.username:
                        await session.execute(
                            sqlalchemy.update(Account)
                            .where(Account.id == self.account.id)
                            .values(username=me.username)
                        )
                        await session.commit()

                    log.info(f"Аккаунт {self.account.id}: профиль обновлён ({me.first_name} {me.last_name})")

        except Exception as e:
            log.debug(f"Не удалось обновить профиль аккаунта {self.account.id}: {e}")

    def _register_neuro_handler(self) -> None:
        from workers.neuro_incoming import register_neuro_handler_on_worker

        register_neuro_handler_on_worker(self)

    async def disconnect(self):
        """Отключение аккаунта."""
        if self.client:
            await self.client.disconnect()
            self.is_connected = False
            log.info(f"Аккаунт {self.account.id} отключён")
    
    async def check_proxy(self) -> bool:
        """
        Проверка работы прокси через попытку get_me().

        Returns:
            bool: True если прокси работает, False если нет, None если не настроен
        """
        if not self.proxy:
            log.debug(f"✓ Прокси не настроен для аккаунта {self.account.id} - проверка пропущена")
            return None  # Явно возвращаем None (нет прокси)

        try:
            # Простая проверка — попытка получить информацию о себе
            if not self.client or not self.is_connected:
                # Подключаемся если ещё не подключён
                connected = await self.connect()
                if not connected:
                    log.warning(f"❌ Не удалось подключить аккаунт {self.account.id} для проверки прокси")
                    # Обновляем статус в БД
                    async with session_scope() as session:
                            await ProxyRepository.update_status(session, self.proxy.id, False)
                    return False

            # Уже подключён — просто проверяем
            await self.client.get_me()

            # Обновляем статус в БД
            async with session_scope() as session:
                    await ProxyRepository.update_status(session, self.proxy.id, True)

            log.info(f"✅ Прокси {self.proxy.name} работает")
            return True

        except Exception as e:
            log.error(f"❌ Прокси {self.proxy.name} не работает: {e}")

            # Обновляем статус в БД
            async with session_scope() as session:
                    await ProxyRepository.update_status(session, self.proxy.id, False)

            return False
    
    async def check_spam_block(self) -> bool:
        """
        Проверка на спам-блок через @SpamBot.

        Returns:
            bool: True если есть спам-блок
        """
        if not self.client or not self.is_connected:
            return False
        if BANDWIDTH_SKIP_SPAMBOT_CHECK:
            log.info(f"SpamBot check skipped for account {self.account.id} (bandwidth saver)")
            return False

        try:
            spam_bot = await self.client.get_entity("SpamBot")

            # Отправляем /start
            await self.client.send_message(spam_bot, "/start")

            # Ждём ответ (увеличиваем время ожидания)
            await asyncio.sleep(3)

            # Получаем последние сообщения от SpamBot
            messages = await self.client.get_messages(spam_bot, limit=3)

            if messages:
                # Берём самое последнее сообщение
                text = messages[0].text.lower()

                log.debug(f"SpamBot ответ для аккаунта {self.account.id}: {text[:200]}")

                # Проверяем ответ
                # Хорошие признаки:
                is_clean = (
                    "good" in text or
                    "your account is in good standing" in text or
                    "у вас нет нарушений" in text or
                    "всё в порядке" in text or
                    "no restrictions" in text or
                    "никаких ограничений" in text
                )

                # Плохие признаки:
                is_blocked = (
                    "limit" in text or
                    "block" in text or
                    "spam" in text or
                    "ограничен" in text or
                    "заблокирован" in text or
                    "restriction" in text or
                    "ban" in text
                )

                # Если есть плохие признаки и нет хороших — значит есть блок
                result = is_blocked and not is_clean

                # Обновляем статус в БД
                async with session_scope() as session:
                        await AccountRepository.set_spam_block(
                            session, self.account.id, result
                        )

                if result:
                    log.warning(f"Аккаунт {self.account.id} ({self.account.username or self.account.phone}) имеет спам-блок")
                else:
                    log.info(f"Аккаунт {self.account.id} ({self.account.username or self.account.phone}) чист")

                return result

            # Если нет сообщений от SpamBot — считаем что блок есть
            log.warning(f"SpamBot не ответил для аккаунта {self.account.id}")
            return False

        except Exception as e:
            log.error(f"Ошибка проверки спам-блока для аккаунта {self.account.id}: {e}")
            return False
    
    async def get_profile_photos(self, limit: int = 10) -> list[dict]:
        """
        Получить все фотографии профиля текущего аккаунта.

        Args:
            limit: Максимальное количество фото (по умолчанию 10)

        Returns:
            Список dict: [{photo_id, dc_id, is_main}, ...]
        """
        if not self.client or not self.is_connected:
            log.warning(f"Нельзя получить фото: аккаунт {self.account.id} не подключён")
            return []

        try:
            photos = await self.client.get_profile_photos('me', limit=limit)
            result = []
            for i, photo in enumerate(photos):
                result.append({
                    'photo_id': photo.id,
                    'dc_id': photo.dc_id,
                    'is_main': (i == 0),  # Первое фото — главная аватарка
                    'index': i,
                })
            log.info(f"Аккаунт {self.account.id}: получено {len(result)} фото профиля")
            return result
        except Exception as e:
            log.error(f"Ошибка получения фото профиля аккаунта {self.account.id}: {e}")
            return []

    async def delete_profile_photo(self, photo_id: int) -> bool:
        """
        Удалить фотографию профиля по ID.

        Args:
            photo_id: ID фотографии (из get_profile_photos)

        Returns:
            bool: True если удалено успешно
        """
        if not self.client or not self.is_connected:
            log.warning(f"Нельзя удалить фото: аккаунт {self.account.id} не подключён")
            return False

        try:
            from telethon.tl.functions.photos import DeletePhotosRequest
            from telethon.tl.types import InputPhoto

            # Находим фото в списке, чтобы получить access_hash
            photos = await self.client.get_profile_photos('me', limit=100)
            target = None
            for p in photos:
                if p.id == photo_id:
                    target = p
                    break

            if not target:
                log.warning(f"Фото {photo_id} не найдено на аккаунте {self.account.id}")
                return False

            # Создаём InputPhoto для удаления
            input_photo = InputPhoto(
                id=target.id,
                access_hash=target.access_hash,
                file_reference=target.file_reference,
            )

            await self.client(DeletePhotosRequest(id=[input_photo]))
            log.info(f"Аккаунт {self.account.id}: фото {photo_id} удалено")
            return True

        except Exception as e:
            log.error(f"Ошибка удаления фото профиля аккаунта {self.account.id}: {e}")
            return False

    async def set_profile_photo(self, file_path: str) -> bool:
        """
        Установить новую фотографию профиля.

        Args:
            file_path: Путь к файлу фотографии

        Returns:
            bool: True если загружено успешно
        """
        if not self.client or not self.is_connected:
            log.warning(f"Нельзя установить фото: аккаунт {self.account.id} не подключён")
            return False

        try:
            from telethon.tl.functions.photos import UploadProfilePhotoRequest

            if not os.path.exists(file_path):
                log.error(f"Файл аватарки не найден: {file_path}")
                return False

            # Загружаем файл на сервер Telegram
            uploaded = await self.client.upload_file(file_path)

            # Устанавливаем как фото профиля
            await self.client(UploadProfilePhotoRequest(file=uploaded))

            log.info(f"Аккаунт {self.account.id}: аватарка обновлена ({file_path})")
            return True

        except Exception as e:
            log.error(f"Ошибка установки аватарки аккаунта {self.account.id}: {e}")
            return False

    async def send_message_with_typing(
        self,
        peer: Union[int, str],
        text: str,
        typing_delay: float = 3.0,
        use_typing: bool = True,
        parse_mode: Optional[str] = None,
        buttons=None,
        formatting_entities: Optional[List[Any]] = None,
    ) -> tuple[bool, Optional[int], Optional[str], Optional[int]]:
        """
        Отправка сообщения с имитацией набора текста.

        peer: username (строка без/с @) или числовой user id. Для холодной рассылки
        предпочтительно username — иначе Telethon часто не находит entity по одному id
        без диалога в сессии («Could not find the input entity»).

        Returns:
            (success, message_id, error_message, peer_telegram_user_id_or_none)
        """
        if not self.client or not self.is_connected:
            return False, None, "Не подключён", None

        try:
            input_entity = await self.client.get_input_entity(peer)
        except Exception as e:
            return False, None, f"Получатель недоступен: {e}", None
        
        try:
            # Имитация набора: включается флагом рассылки use_typing (не отключаем через
            # BANDWIDTH_SAVER_MODE — иначе при дефолте BANDWIDTH_SAVER_MODE=1 набор не виден).
            if use_typing and typing_delay > 0:
                await self.client(
                    SetTypingRequest(
                        peer=input_entity,
                        action=SendMessageTypingAction(),
                    )
                )
                await asyncio.sleep(typing_delay)
            
            # Отправка сообщения
            try:
                result = await self.client.send_message(
                    input_entity,
                    text,
                    parse_mode=parse_mode,
                    link_preview=False,
                    buttons=buttons,
                    formatting_entities=formatting_entities,
                )
            except ValueError as e:
                # Fallback: если HTML разметка оказалась невалидной, шлём как plain text
                # (без parse_mode), чтобы не терять ответ пользователю.
                if parse_mode:
                    log.warning(
                        f"HTML parse fallback for account {self.account.id}: {e}. "
                        f"Sending plain text."
                    )
                    plain_text = _HTML_TAG_RE.sub("", text).replace("&amp;", "&").strip()
                    result = await self.client.send_message(
                        input_entity,
                        plain_text or text,
                        parse_mode=None,
                        link_preview=False,
                        buttons=buttons,
                        formatting_entities=None,
                    )
                else:
                    raise

            peer_uid = None
            if result and result.peer_id and isinstance(result.peer_id, PeerUser):
                peer_uid = result.peer_id.user_id

            return True, result.id, None, peer_uid

        except PeerFloodError:
            # PEER_FLOOD / «Too many requests» — лимит исходящих к контактам (часто новые ЛС), не @SpamBot.
            return False, None, (
                "PEER_FLOOD: Telegram ограничил исходящие (слишком частые сообщения в новые диалоги). "
                "Это не то же, что спамблок в @SpamBot. Увеличьте задержки между сообщениями и аккаунтами."
            ), None

        except FloodWaitError as e:
            # FloodWait — критично!
            wait_time = e.seconds + 5  # +5 секунд запас
            log.warning(
                f"FloodWait на аккаунте {self.account.id} "
                f"({self.account.username or self.account.phone}). "
                f"Ожидание {wait_time} сек"
            )
            
            # Обновляем статус в БД
            async with session_scope() as session:
                    until = datetime.utcnow() + timedelta(seconds=wait_time)
                    await AccountRepository.set_flood_wait(session, self.account.id, until)
            
            # Отключаем аккаунт временно
            self.is_running = False

            return False, None, f"FloodWait: {e.seconds} сек", None

        except errors.UserBlockedError:
            return False, None, "Пользователь заблокировал бота", None

        except errors.UserNotMutualContactError:
            return False, None, "Пользователь скрыл профиль", None

        except errors.UsernameNotOccupiedError:
            return False, None, "Невалидный username", None

        except errors.ChatWriteForbiddenError:
            return False, None, "Нет прав на запись", None

        except Exception as e:
            log.error(f"Ошибка отправки сообщения: {e}")
            return False, None, str(e), None


class WorkerManager:
    """
    Менеджер рабочих аккаунтов.
    Управляет пулом аккаунтов, запуском рассылок, обработкой ошибок.
    """
    
    def __init__(self):
        self.workers: Dict[int, Worker] = {}  # account_id -> Worker
        self.is_running = False
        self.current_mailing_id: Optional[int] = None
        self._mailing_utc_offset: Optional[int] = None  # эффективный UTC-сдвиг на время рассылки
        self._stop_event = asyncio.Event()
        # True с запуска start_mailing до конца восстановления пула (в т.ч. фоновое)
        self._mailing_busy = False
        # После рассылки восстанавливаем только этот пул (None = все аккаунты из БД).
        self._last_mailing_group_id: Optional[int] = None
        # True только если дошли до основного цикла (иначе finally не трогает пул).
        self._mailing_run_started: bool = False
        self._notify_bot = None  # aiogram Bot — для уведомлений владельца о рассылке

    def set_notify_bot(self, bot) -> None:
        """Вызывается из run_bot: отправка статуса рассылки в личку владельцу."""
        self._notify_bot = bot

    async def _notify_owner_html(self, html: str) -> None:
        if not self._notify_bot:
            return
        try:
            from aiogram.enums import ParseMode
            from bot.config import OWNER_ID

            await self._notify_bot.send_message(OWNER_ID, html, parse_mode=ParseMode.HTML)
        except Exception as e:
            log.warning(f"Уведомление владельцу не отправлено: {e}")

    async def load_accounts(self, *, group_id: Optional[int] = None):
        """
        Загрузка аккаунтов из БД и создание Worker.

        group_id:
            None — все аккаунты (мониторинг, нейрочат, общий режим).
            int — только участники этой группы (рассылка с выбранной группой).
        """
        await self.disconnect_all()
        self.workers.clear()

        async with session_scope() as session:
            if group_id is not None:
                accounts = await AccountRepository.get_all_in_group(session, group_id)
                log.info(
                    f"Выборка аккаунтов для рассылки: группа id={group_id}, "
                    f"в БД записей: {len(accounts)}"
                )
            else:
                accounts = await AccountRepository.get_all(session)

        sessions_dir = Path("data/sessions")
        sessions_dir.mkdir(exist_ok=True)

        for account in accounts:
            session_path = sessions_dir / f"{account.session_name}.session"

            if not session_path.exists():
                log.warning(f"Сессия не найдена: {session_path}")
                continue

            worker = Worker(account, session_path, account.proxy)
            self.workers[account.id] = worker

            log.info(f"Загружен аккаунт {account.id} ({account.username or account.phone})")

        log.info(f"Загружено {len(self.workers)} аккаунтов")

    async def _interruptible_sleep(self, seconds: float) -> None:
        """Ожидание с проверкой остановки рассылки (не блокировать до 60 с)."""
        if seconds <= 0:
            return
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self._stop_event.is_set():
                return
            await asyncio.sleep(min(0.5, end - time.monotonic()))

    async def _pool_member_ids(self, gid: Optional[int]) -> set[int]:
        """ID аккаунтов, которые должны быть в пуле (группа gid или все) и у
        которых есть файл сессии — то есть ровно то, что загрузил бы load_accounts."""
        sessions_dir = Path("data/sessions")
        async with session_scope() as session:
            if gid is not None:
                member_ids = await GroupRepository.get_member_account_ids(session, gid)
                accounts = [
                    a for a in await AccountRepository.get_all(session) if a.id in member_ids
                ]
            else:
                accounts = await AccountRepository.get_all(session)
        return {
            a.id
            for a in accounts
            if (sessions_dir / f"{a.session_name}.session").exists()
        }

    async def _restore_workers_after_mailing(self) -> None:
        """
        Восстановление пула после рассылки (только группа этой рассылки).

        Важно: если нужный пул уже загружен (а после рассылки аккаунты и так
        подключены для нейрочата) — НЕ делаем лишний disconnect_all + reconnect.
        Переподключение уже-авторизованной сессии бессмысленно и зря дёргает
        сессии. Reconnect выполняется только если состав пула изменился.
        """
        try:
            gid = self._last_mailing_group_id
            needed_ids = await self._pool_member_ids(gid)
            if needed_ids and needed_ids == set(self.workers.keys()):
                connected = sum(1 for w in self.workers.values() if w.is_connected)
                log.info(
                    f"Пул после рассылки уже актуален ({connected}/{len(needed_ids)} "
                    "подключено) — лишний реконнект пропущен."
                )
                return
            await self.load_accounts(group_id=gid)
            await self.connect_all(quiet_unauthorized=True)
            if gid is not None:
                log.info(
                    f"Пул воркеров восстановлен после рассылки (только группа аккаунтов id={gid})"
                )
            else:
                log.info(
                    "Пул воркеров восстановлен после рассылки (все аккаунты из БД)"
                )
        except Exception as e:
            log.warning(f"Не удалось восстановить пул после рассылки: {e}")
        finally:
            self._mailing_busy = False

    async def ensure_workers_for_account_ids(self, account_ids: List[int]) -> None:
        """
        Если в пуле нет хотя бы одного из нужных аккаунтов — перезагрузить всех из БД.
        Нужно после рассылки с ограниченным пулом: проверки группы, 2FA и т.д.
        """
        need = set(account_ids)
        if not need.issubset(self.workers.keys()):
            await self.load_accounts()
    
    async def precheck_proxies(self, *, timeout: int = 8) -> Dict[int, Optional[bool]]:
        """
        Параллельная проверка прокси всех загруженных воркеров (через сам прокси,
        запрос exit-IP). Возвращает {account_id: True|False|None}:
          True  — прокси рабочий;
          False — прокси не отвечает;
          None  — прокси не назначен.
        Заодно обновляет `is_working` у прокси в БД.
        """
        from utils.proxy_checker import check_proxy

        if not self.workers:
            return {}

        sem = asyncio.Semaphore(25)

        async def _one(worker: "Worker"):
            p = worker.proxy
            if not p:
                return worker.account.id, None, None
            ptype = p.proxy_type.value if hasattr(p.proxy_type, "value") else str(p.proxy_type)
            async with sem:
                try:
                    ok, _ip = await check_proxy(ptype, p.host, p.port, p.username, p.password, timeout=timeout)
                except Exception as e:
                    log.debug(f"precheck proxy acc={worker.account.id}: {e}")
                    ok = False
            return worker.account.id, bool(ok), p.id

        pairs = await asyncio.gather(*[_one(w) for w in self.workers.values()])
        results: Dict[int, Optional[bool]] = {}
        proxy_status: Dict[int, bool] = {}
        for aid, ok, pid in pairs:
            results[aid] = ok
            if pid is not None and ok is not None:
                proxy_status[pid] = ok
        if proxy_status:
            async with session_scope() as session:
                for pid, ok in proxy_status.items():
                    try:
                        await ProxyRepository.update_status(session, pid, ok)
                    except Exception as e:
                        log.debug(f"update proxy status {pid}: {e}")
        return results

    async def connect_all(
        self,
        *,
        quiet_unauthorized: bool = False,
        require_working_proxy: bool = True,
    ) -> dict:
        """
        Подключение аккаунтов (последовательно, с паузой — меньше блокировок SQLite).

        Безопасность по умолчанию (`require_working_proxy=True`): сначала
        проверяются прокси, занятые аккаунтами; **аккаунты без прокси или с
        мёртвым прокси НЕ подключаются и НЕ авторизуются** — иначе вход пойдёт
        с «чужого» IP (сервера) и Telegram может забанить. Передать
        `require_working_proxy=False` — старое поведение (подключать всех).
        """
        summary = {
            "connected": 0,
            "unauthorized": 0,
            "skipped_no_proxy": 0,
            "skipped_dead_proxy": 0,
        }
        proxy_ok: Dict[int, Optional[bool]] = {}
        if require_working_proxy and self.workers:
            log.info("🔍 Проверка прокси перед подключением аккаунтов…")
            proxy_ok = await self.precheck_proxies()

        items = list(self.workers.values())
        for i, worker in enumerate(items):
            if require_working_proxy:
                st = proxy_ok.get(worker.account.id)
                if st is None:
                    log.warning(
                        f"⛔ Аккаунт {worker.account.id}: нет прокси — пропускаю "
                        f"подключение (защита от бана)"
                    )
                    summary["skipped_no_proxy"] += 1
                    continue
                if st is False:
                    log.warning(
                        f"⛔ Аккаунт {worker.account.id}: прокси не работает — "
                        f"пропускаю подключение/авторизацию"
                    )
                    summary["skipped_dead_proxy"] += 1
                    continue
            ok = await worker.connect(quiet=quiet_unauthorized)
            summary["connected" if ok else "unauthorized"] += 1
            if i + 1 < len(items):
                await asyncio.sleep(0.35)

        if require_working_proxy:
            log.info(
                f"connect_all: подключено {summary['connected']}, "
                f"не авторизованы {summary['unauthorized']}, "
                f"без прокси {summary['skipped_no_proxy']}, "
                f"мёртвый прокси {summary['skipped_dead_proxy']}"
            )
        return summary
    
    async def disconnect_all(self):
        """Отключение всех аккаунтов."""
        for worker in self.workers.values():
            await worker.disconnect()

    async def reconnect_account(self, account_id: int):
        """
        Переподключить аккаунт с актуальными данными из БД (после смены прокси).
        Отключает старый Worker, загружает аккаунт с новым proxy, создаёт новый Worker.

        Args:
            account_id: ID аккаунта для переподключения
        """
        # 1. Отключить старый Worker
        old_worker = self.workers.get(account_id)
        if old_worker:
            await old_worker.disconnect()
            del self.workers[account_id]
            log.info(f"Аккаунт {account_id}: старый Worker отключён")

        # 2. Загрузить аккаунт из БД с новым proxy — сессия сразу закрывается
        async with db.async_session_maker() as session:
            account = await AccountRepository.get_by_id(session, account_id)

        if not account:
            log.warning(f"Аккаунт {account_id} не найден при переподключении")
            return

        # 3. Создать новый Worker с актуальным proxy
        session_path = Path("data/sessions") / f"{account.session_name}.session"
        if session_path.exists():
            new_worker = Worker(account, session_path, account.proxy)
            self.workers[account_id] = new_worker
            log.info(
                f"Аккаунт {account_id} переподключён "
                f"(прокси: {account.proxy.name if account.proxy else 'без прокси'})"
            )
        else:
            log.warning(f"Сессия не найдена при переподключении аккаунта {account_id}")

    async def check_all_proxies(self) -> dict:
        """
        Проверка всех прокси.
        
        Returns:
            dict: {account_id: is_working}
        """
        results = {}
        for account_id, worker in self.workers.items():
            is_working = await worker.check_proxy()
            results[account_id] = is_working
        return results
    
    async def check_all_spam_blocks(self) -> dict:
        """
        Проверка всех аккаунтов на спам-блок.
        Подключает неподключённые аккаунты перед проверкой.

        Returns:
            dict: {account_id: is_blocked}
        """
        results = {}
        for account_id, worker in self.workers.items():
            # Если аккаунт не подключён — пробуем подключить
            if not worker.is_connected:
                log.info(f"Подключаю аккаунт {account_id} для проверки спам-блока...")
                connected = await worker.connect()
                if not connected:
                    log.warning(f"Не удалось подключить аккаунт {account_id} для проверки спам-блока")
                    results[account_id] = None  # None = не удалось проверить
                    continue

            is_blocked = await worker.check_spam_block()
            results[account_id] = is_blocked
        return results

    async def check_proxies_for_account_ids(self, account_ids: List[int]) -> dict:
        """
        Проверка прокси только для указанных account_id (например, группа).

        Returns:
            dict: {account_id: is_working | None}; None — нет воркера / нет сессии.
        """
        results = {}
        for aid in account_ids:
            worker = self.workers.get(aid)
            if not worker:
                results[aid] = None
                continue
            results[aid] = await worker.check_proxy()
        return results

    async def check_spam_blocks_for_account_ids(self, account_ids: List[int]) -> dict:
        """
        Проверка спам-блока для указанных account_id.
        """
        results = {}
        for aid in account_ids:
            worker = self.workers.get(aid)
            if not worker:
                results[aid] = None
                continue
            if not worker.is_connected:
                connected = await worker.connect()
                if not connected:
                    results[aid] = None
                    continue
            results[aid] = await worker.check_spam_block()
        return results

    def apply_template(
        self,
        text: str,
        client_username: str,
        account: Optional[Account] = None,
        mailing_name: str = "",
        mailing_link: str = "",
    ) -> str:
        """
        Подстановка плейсхолдеров в текст первого сообщения.

        Доступно: {username} {date} {time} {datetime} {timezone} {firstname} {lastname}
        {phone} {account_id} {mailing} {link} {random4} {random6}
        Динамические офсеты: {time+3} {time-2} {datetime+1} {date-1} {timezone+2}
        """
        base = (
            self._mailing_utc_offset
            if self._mailing_utc_offset is not None
            else MAILING_BASE_UTC_OFFSET
        )
        now = datetime.now(timezone.utc) + timedelta(hours=base)

        replacements = {
            "{username}": f"@{client_username}",
            "{date}": now.strftime("%d.%m.%Y"),
            "{time}": now.strftime("%H:%M"),
            "{datetime}": now.strftime("%d.%m.%Y %H:%M"),
            "{fullname}": client_username,  # legacy alias
            "{timezone}": _fmt_utc_offset(base),
            "{firstname}": (account.first_name or "") if account else "",
            "{lastname}": (account.last_name or "") if account else "",
            "{phone}": account.phone if account else "",
            "{account_id}": str(account.id) if account else "",
            "{mailing}": mailing_name,
            "{link}": mailing_link or "",
            "{random4}": f"{random.randint(1000, 9999)}",
            "{random6}": f"{random.randint(100000, 999999)}",
        }
        result = text
        for key, value in replacements.items():
            result = result.replace(key, value)

        def _offset_sub(match: re.Match[str]) -> str:
            token = (match.group(1) or "").lower()
            delta = int(match.group(2) or "0")
            shifted = now + timedelta(hours=delta)
            if token == "time":
                return shifted.strftime("%H:%M")
            if token == "date":
                return shifted.strftime("%d.%m.%Y")
            if token == "datetime":
                return shifted.strftime("%d.%m.%Y %H:%M")
            if token == "timezone":
                return _fmt_utc_offset(base + delta)
            return ""

        result = re.sub(
            r"\{(time|date|datetime|timezone)([+-]\d{1,2})\}",
            _offset_sub,
            result,
        )
        return result
    
    async def _run_test_mailing(
        self,
        mailing_id: int,
        *,
        variants: List[str],
        variant_mode: str,
        mailing_name: str,
        mailing_link: str,
        use_typing: bool,
        smart_delay: bool,
        delay: float,
        delay_between_accounts: float,
        batch_delay: float,
        group_id: Optional[int],
        end_at: Optional[datetime],
        progress_callback: Optional[Callable] = None,
    ) -> int:
        """
        Тестовая рассылка: КАЖДЫЙ подключённый аккаунт пишет КАЖДОМУ тестовому
        получателю по очереди. Без дедупа и ротации — цель теста: убедиться, что
        отписывается каждый аккаунт (например, на свой собственный @username).
        Лимит успешных и пауза-кулдаун здесь не применяются. Возвращает processed.
        """
        async with session_scope() as session:
            recipients = await ClientRepository.get_test_recipients_all(session, mailing_id)
            if group_id:
                accounts = await AccountRepository.get_all_in_group(session, group_id)
            else:
                accounts = await AccountRepository.get_all(session)

        if not recipients:
            log.info(f"Тест {mailing_id}: список тестовых получателей пуст — нечего слать.")
            return 0

        accounts = sorted(accounts, key=lambda a: a.id)
        processed = 0
        seq_idx = 0

        for account in accounts:
            if self._stop_event.is_set() or (end_at and datetime.utcnow() >= end_at):
                break
            worker = self.workers.get(account.id)
            if not worker or not worker.is_connected:
                log.info(f"Тест {mailing_id}: аккаунт {account.id} не подключён — пропуск.")
                continue

            wrote_any = False
            for recipient in recipients:
                if self._stop_event.is_set() or (end_at and datetime.utcnow() >= end_at):
                    break

                if variant_mode == "sequential":
                    message_body = variants[seq_idx % len(variants)]
                    seq_idx += 1
                else:
                    message_body = random.choice(variants)
                final_text = self.apply_template(
                    message_body,
                    recipient.username,
                    account=worker.account,
                    mailing_name=mailing_name,
                    mailing_link=mailing_link,
                )

                uname = (getattr(recipient, "username", None) or "").strip().lstrip("@")
                if uname:
                    target_peer: Union[int, str] = uname
                elif recipient.telegram_user_id:
                    target_peer = int(recipient.telegram_user_id)
                else:
                    log.info(f"Тест {mailing_id}: у получателя id={recipient.id} нет ни @username, ни tg id — пропуск.")
                    continue

                typing_delay = random.uniform(5.0, 10.0) if use_typing else 0.0
                out_plain, out_entities = plain_text_to_telegram_link_message(final_text)
                success, msg_id, error, peer_uid = await worker.send_message_with_typing(
                    peer=target_peer,
                    text=out_plain,
                    typing_delay=typing_delay,
                    use_typing=use_typing,
                    parse_mode=None,
                    formatting_entities=out_entities or None,
                )

                async with session_scope() as session:
                    await MailingLogRepository.create(
                        session=session,
                        mailing_id=mailing_id,
                        account_id=account.id,
                        client_id=recipient.id,
                        success=success,
                        error_message=error,
                        message_id=msg_id if success else None,
                    )
                    if success:
                        await ClientRepository.update_status(
                            session, recipient.id, ClientStatus.CONTACTED
                        )
                        if peer_uid:
                            await ClientRepository.set_telegram_user_id(
                                session, recipient.id, int(peer_uid)
                            )
                        ms = await ClientMailSessionRepository.get_or_create(
                            session, recipient.id, account.id, mailing_id
                        )
                        if ms.first_outbound_at is None:
                            await ClientMailSessionRepository.set_first_outbound(session, ms.id)
                        await AccountRepository.increment_stats(session, account.id, sent=1)
                        await MailingRepository.increment_stats(session, mailing_id, sent=1)
                    else:
                        if error and "No user has" in error and "as username" in error:
                            await ClientRepository.update_status(
                                session, recipient.id, ClientStatus.INVALID
                            )
                        await AccountRepository.increment_stats(session, account.id, failed=1)
                        await MailingRepository.increment_stats(session, mailing_id, failed=1)

                processed += 1
                wrote_any = True
                if progress_callback:
                    await progress_callback(processed, processed)

                acc_lbl = _account_log_label(account)
                cl_usr = (getattr(recipient, "username", None) or "").strip() or "—"
                if success:
                    log.info(
                        f"Тест {mailing_id}: аккаунт {acc_lbl} → клиент id={recipient.id} @{cl_usr} | OK"
                    )
                else:
                    log.warning(
                        f"Тест {mailing_id}: не удалось | аккаунт {acc_lbl} → клиент id={recipient.id} "
                        f"@{cl_usr} | ошибка: {error}"
                    )

                await self._interruptible_sleep(_mailing_jittered_delay(delay, smart_delay))

            # Пауза между аккаунтами (как и в обычной рассылке).
            if wrote_any:
                extra = delay_between_accounts + batch_delay
                if extra > 0:
                    await self._interruptible_sleep(_mailing_jittered_delay(extra, smart_delay))

        log.info(f"Тест {mailing_id}: завершено, отправок всего {processed}.")
        return processed

    async def start_mailing(
        self,
        mailing_id: int,
        progress_callback: Optional[Callable] = None,
    ):
        """
        Запуск рассылки. Аудитория: mailing.target_group_id или все аккаунты.
        Текст: основной message_text + варианты из message_variants_json, выбор случайный.
        """
        if self._mailing_busy:
            log.warning("Рассылка уже выполняется или завершается")
            return

        self._mailing_busy = True
        self._mailing_run_started = False
        self.is_running = True
        self.current_mailing_id = mailing_id
        self._stop_event.clear()

        try:
            await telemetry_emitter.emit_event(
                "info",
                "mailing_start",
                f"Mailing started: {mailing_id}",
                payload={"mailing_id": mailing_id},
            )
            async with session_scope() as session:
                mailing = await MailingRepository.get_by_id(session, mailing_id)

                if not mailing:
                    log.error(f"Рассылка {mailing_id} не найдена")
                    return

                self._mailing_utc_offset = (
                    await InstanceSettingsRepository.get_effective_mailing_base_utc_offset(
                        session
                    )
                )
                variants: List[str] = []
                if mailing.message_text and str(mailing.message_text).strip():
                    variants.append(mailing.message_text.strip())
                try:
                    extra = json.loads(mailing.message_variants_json or "[]")
                    if isinstance(extra, list):
                        for x in extra:
                            s = str(x).strip()
                            if s:
                                variants.append(s)
                except Exception:
                    pass

                if not variants:
                    log.error(f"Рассылка {mailing_id}: нет ни одного варианта текста")
                    await MailingRepository.update_status(session, mailing_id, MailingStatus.ERROR)
                    return

                await MailingRepository.update_status(session, mailing_id, MailingStatus.RUNNING)
                await MailingRepository.reset_stats_for_new_run(session, mailing_id)

                group_id = mailing.target_group_id
                self._last_mailing_group_id = group_id
                self._mailing_run_started = True
                delay = float(mailing.delay_between_messages or 10.0)
                delay_between_accounts = float(mailing.delay_between_accounts or 10.0)
                batch_delay = float(mailing.batch_delay or 0.0)
                messages_per_account = max(1, int(mailing.messages_per_batch or 10))
                variant_mode = (getattr(mailing, "variant_mode", None) or "random").strip().lower()
                if variant_mode not in ("random", "sequential"):
                    variant_mode = "random"
                auto_stop_hours = (
                    float(mailing.auto_stop_hours)
                    if getattr(mailing, "auto_stop_hours", None)
                    else None
                )
                use_typing = mailing.use_typing
                # При включённой имитации набора — каждый раз случайная пауза 5–10 с (не из БД)
                smart_delay = bool(getattr(mailing, "smart_delay", False))
                mailing_name = mailing.name or str(mailing_id)
                mailing_link = normalize_public_link(
                    (getattr(mailing, "community_link", None) or "").strip()
                )
                n_group_accounts = 0
                if group_id:
                    n_group_accounts = len(
                        await GroupRepository.get_member_account_ids(session, group_id)
                    )

                if group_id:
                    log.info(f"Запуск рассылки {mailing_id}, группа аккаунтов id={group_id}")
                else:
                    log.info(f"Запуск рассылки {mailing_id} (все подходящие аккаунты)")

                mailing_cooldown_hours = float(
                    getattr(mailing, "mailing_cooldown_hours", None) or 12.0
                )
                max_recipients_cap = getattr(mailing, "max_recipients", None)
                audience_mode = (
                    getattr(mailing, "audience_mode", None) or "classes"
                ).strip().lower()
                test_mode = audience_mode == "test"

            safe_name = html.escape(str(mailing_name or ""), quote=False)
            _start_lines = [
                f"🚀 <b>Рассылка #{mailing_id}</b> «{safe_name}» запущена.",
                f"👥 Аккаунтов в группе: <b>{n_group_accounts if n_group_accounts else 'все доступные'}</b>",
                (
                    "🧪 <b>Тестовый режим:</b> каждый подключённый аккаунт пишет каждому "
                    "тестовому получателю по очереди (без дедупа — отписываются все аккаунты)."
                    if test_mode else
                    f"📨 Ротация: после <b>{messages_per_account}</b> успешных с одного аккаунта — "
                    "следующий (лимит не накапливается между запусками; логи — в мониторинге)."
                ),
                f"🧩 Перебор вариантов первого сообщения: <b>{'по очереди' if variant_mode == 'sequential' else 'случайно'}</b>.",
                "<i>Пул после рассылки восстанавливается только для этой группы — нейрочат на них остаётся.</i>",
            ]
            await self._notify_owner_html("\n".join(_start_lines))

            run_started_at = datetime.utcnow()
            end_at = (
                run_started_at + timedelta(hours=auto_stop_hours)
                if auto_stop_hours and auto_stop_hours > 0
                else None
            )
            processed = 0
            current_idx = 0
            sent_from_current = 0
            sequential_variant_idx = 0
            prev_eligible_ids: Optional[tuple[int, ...]] = None
            cap_reached = False

            # Тестовый режим — отдельная логика: каждый аккаунт пишет каждому
            # тестовому получателю (без дедупа и ротации). Production-цикл ниже
            # для test_mode сразу прерывается.
            if test_mode:
                processed = await self._run_test_mailing(
                    mailing_id,
                    variants=variants,
                    variant_mode=variant_mode,
                    mailing_name=mailing_name,
                    mailing_link=mailing_link,
                    use_typing=use_typing,
                    smart_delay=smart_delay,
                    delay=delay,
                    delay_between_accounts=delay_between_accounts,
                    batch_delay=batch_delay,
                    group_id=group_id,
                    end_at=end_at,
                    progress_callback=progress_callback,
                )

            while True:
                if test_mode:
                    break
                if self._stop_event.is_set():
                    log.info("Рассылка остановлена пользователем")
                    break
                if end_at and datetime.utcnow() >= end_at:
                    log.info(
                        f"Рассылка {mailing_id}: достигнут автостоп {auto_stop_hours:g} ч"
                    )
                    break

                async with session_scope() as session:
                    mrow = await MailingRepository.get_by_id(session, mailing_id)
                    if not mrow:
                        log.error(f"Рассылка {mailing_id} пропала из БД")
                        break
                    group_id = mrow.target_group_id
                    clients = await ClientRepository.get_mailing_queue(session, mrow)

                if not clients:
                    log.info(f"Рассылка {mailing_id}: очередь пуста — завершение.")
                    break

                queue_len = len(clients)
                for idx, client in enumerate(clients, start=1):
                    if self._stop_event.is_set():
                        break
                    if end_at and datetime.utcnow() >= end_at:
                        break
                    if cap_reached:
                        break

                    async with session_scope() as session:
                        available_accounts = await AccountRepository.get_available_for_mailing(
                            session,
                            tags_filter=None,
                            group_id=group_id,
                            mailing_id=mailing_id,
                        )

                    if not available_accounts:
                        log.warning("Нет доступных аккаунтов для рассылки")
                        await self._interruptible_sleep(60)
                        continue

                    accounts_pool = sorted(available_accounts, key=lambda a: a.id)
                    # Не ограничиваем аккаунты прошлыми успехами по этой рассылке — только ротация в этом запуске.
                    eligible_pool = accounts_pool

                    eligible_ids = tuple(a.id for a in eligible_pool)
                    if eligible_ids != prev_eligible_ids:
                        prev_eligible_ids = eligible_ids
                        current_idx = 0

                    n = len(eligible_pool)
                    log.info(
                        f"Рассылка {mailing_id}: пул (target_group_id={group_id}): "
                        f"{[a.id for a in accounts_pool]} | в работе: {[a.id for a in eligible_pool]}"
                    )

                    account = None
                    worker = None
                    used_idx: Optional[int] = None
                    for attempt in range(n):
                        i = (current_idx + attempt) % n
                        acc = eligible_pool[i]
                        w = self.workers.get(acc.id)
                        if w and w.is_connected:
                            account = acc
                            worker = w
                            used_idx = i
                            break

                    if not account or not worker or used_idx is None:
                        log.warning("Нет подключённых аккаунтов для рассылки")
                        await self._interruptible_sleep(60)
                        continue

                    if variant_mode == "sequential":
                        message_body = variants[sequential_variant_idx % len(variants)]
                        sequential_variant_idx += 1
                    else:
                        message_body = random.choice(variants)
                    final_text = self.apply_template(
                        message_body,
                        client.username,
                        account=worker.account,
                        mailing_name=mailing_name,
                        mailing_link=mailing_link,
                    )

                    # Username, а не сырой user_id: иначе Telethon часто даёт
                    # «Could not find the input entity», если с этим аккаунтом ещё не было диалога.
                    uname = (getattr(client, "username", None) or "").strip().lstrip("@")
                    target_peer: Union[int, str] = (
                        uname if uname else int(client.telegram_user_id)
                    )
                    typing_delay = random.uniform(5.0, 10.0) if use_typing else 0.0
                    out_plain, out_entities = plain_text_to_telegram_link_message(final_text)
                    success, msg_id, error, peer_uid = await worker.send_message_with_typing(
                        peer=target_peer,
                        text=out_plain,
                        typing_delay=typing_delay,
                        use_typing=use_typing,
                        parse_mode=None,
                        formatting_entities=out_entities or None,
                    )

                    async with session_scope() as session:
                        await MailingLogRepository.create(
                            session=session,
                            mailing_id=mailing_id,
                            account_id=account.id,
                            client_id=client.id,
                            success=success,
                            error_message=error,
                            message_id=msg_id if success else None,
                        )

                        if success:
                            await ClientRepository.update_status(
                                session, client.id, ClientStatus.CONTACTED
                            )
                            if peer_uid:
                                await ClientRepository.set_telegram_user_id(
                                    session, client.id, int(peer_uid)
                                )
                            ms = await ClientMailSessionRepository.get_or_create(
                                session, client.id, account.id, mailing_id
                            )
                            if ms.first_outbound_at is None:
                                await ClientMailSessionRepository.set_first_outbound(
                                    session, ms.id
                                )
                            await AccountRepository.increment_stats(
                                session, account.id, sent=1
                            )
                            await MailingRepository.increment_stats(
                                session, mailing_id, sent=1
                            )
                            await MailingAccountStateRepository.record_successful_first_message(
                                session,
                                mailing_id,
                                account.id,
                                wave_limit=messages_per_account,
                                cooldown_hours=mailing_cooldown_hours,
                            )
                        else:
                            if error and "No user has" in error and "as username" in error:
                                await ClientRepository.update_status(
                                    session, client.id, ClientStatus.INVALID
                                )
                            await AccountRepository.increment_stats(
                                session, account.id, failed=1
                            )
                            await MailingRepository.increment_stats(
                                session, mailing_id, failed=1
                            )

                    if success and max_recipients_cap:
                        async with session_scope() as session:
                            ms = await MailingRepository.get_by_id(session, mailing_id)
                            if ms and int(ms.messages_sent or 0) >= int(max_recipients_cap):
                                cap_reached = True
                                log.info(
                                    f"Рассылка {mailing_id}: достигнут лимит "
                                    f"{max_recipients_cap} успешных отправок"
                                )

                    processed += 1

                    if progress_callback:
                        await progress_callback(processed, processed)

                    acc_lbl = _account_log_label(account)
                    cl_usr = (getattr(client, "username", None) or "").strip() or "—"
                    if success:
                        log.info(
                            f"Рассылка {mailing_id}: {idx}/{queue_len} очередь | всего {processed} | "
                            f"аккаунт {acc_lbl} | клиент id={client.id} @{cl_usr} | OK"
                        )
                    else:
                        hint = _mailing_send_failure_hint(error)
                        log.warning(
                            f"Рассылка {mailing_id}: отправка не удалась | {idx}/{queue_len} очередь | всего {processed} | "
                            f"аккаунт {acc_lbl} | клиент id={client.id} @{cl_usr} | "
                            f"ошибка: {error} | что делать: {hint}"
                        )
                    if not success and error:
                        await telemetry_emitter.emit_event(
                            "error",
                            "mailing_send",
                            f"Mailing send failed account={account.id} client={client.id}",
                            payload={
                                "mailing_id": mailing_id,
                                "client_id": client.id,
                                "account_id": account.id,
                                "account_phone": getattr(account, "phone", None),
                                "error": error,
                            },
                        )

                    await self._interruptible_sleep(
                        _mailing_jittered_delay(delay, smart_delay)
                    )

                    if success:
                        sent_from_current += 1
                        if sent_from_current >= messages_per_account:
                            sent_from_current = 0
                            current_idx = (used_idx + 1) % n
                            extra = delay_between_accounts + batch_delay
                            if extra > 0:
                                await self._interruptible_sleep(
                                    _mailing_jittered_delay(extra, smart_delay)
                                )

                    if cap_reached:
                        break

                if cap_reached:
                    break

            # Завершение
            async with session_scope() as session:
                if self._stop_event.is_set():
                    await MailingRepository.update_status(
                        session, mailing_id, MailingStatus.CANCELLED
                    )
                    log.info(
                        f"Рассылка {mailing_id} остановлена пользователем. Обработано: {processed}"
                    )
                else:
                    await MailingRepository.update_status(
                        session, mailing_id, MailingStatus.COMPLETED
                    )
                    if end_at:
                        log.info(
                            f"Рассылка {mailing_id} завершена по автостопу. Обработано: {processed}"
                        )
                    else:
                        log.info(
                            f"Рассылка {mailing_id} завершена. Обработано: {processed}"
                        )
                await telemetry_emitter.emit_event(
                    "info",
                    "mailing_finish",
                    f"Mailing finished: {mailing_id}",
                    payload={"mailing_id": mailing_id, "processed": processed},
                )

            async with session_scope() as session:
                m_final = await MailingRepository.get_by_id(session, mailing_id)
            if m_final:
                _ok = int(m_final.messages_sent or 0)
                _fail = int(m_final.messages_failed or 0)
                _neuro = bool(getattr(m_final, "neurochat_enabled", False))
                if self._stop_event.is_set():
                    await self._notify_owner_html(
                        f"⏹ <b>Рассылка #{mailing_id}</b> остановлена вручную.\n"
                        f"✅ Успешно: <b>{_ok}</b> · ❌ Ошибок: <b>{_fail}</b>"
                    )
                else:
                    _extra = ""
                    if _neuro:
                        _extra = (
                            "\n🔮 <b>Нейрочат включён</b> в рассылке — можно отвечать на входящие."
                        )
                    await self._notify_owner_html(
                        f"✅ <b>Рассылка #{mailing_id}</b> завершена.{_extra}\n"
                        f"📤 Успешно: <b>{_ok}</b>\n"
                        f"❌ Ошибок / сбоев: <b>{_fail}</b>\n"
                        f"🔮 Нейрочат в настройках: <b>{'вкл' if _neuro else 'выкл'}</b>"
                    )
            
        except Exception as e:
            log.error(f"Ошибка рассылки {mailing_id}: {e}")
            await telemetry_emitter.emit_event(
                "critical",
                "mailing_error",
                f"Mailing crashed: {mailing_id}",
                payload={"mailing_id": mailing_id, "error": str(e)},
            )
            
            async with session_scope() as session:
                    await MailingRepository.update_status(
                        session, mailing_id, MailingStatus.ERROR
                    )
        
        finally:
            self.is_running = False
            self.current_mailing_id = None
            self._mailing_utc_offset = None
            if self._mailing_run_started:
                try:
                    asyncio.create_task(self._restore_workers_after_mailing())
                except Exception as e:
                    log.warning(f"Не удалось запустить восстановление пула: {e}")
                    self._mailing_busy = False
            else:
                self._mailing_busy = False

    def stop_mailing(self):
        """Остановка рассылки: UI сразу показывает «Запустить», цикл выходит по событию."""
        self._stop_event.set()
        self.is_running = False
        self.current_mailing_id = None
        log.info("Получена команда остановки рассылки")


# Глобальный экземпляр
worker_manager = WorkerManager()
