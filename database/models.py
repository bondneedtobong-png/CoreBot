"""
SQLAlchemy модели базы данных.
"""
from datetime import datetime
from sqlalchemy import (
    Column,
    Integer,
    BigInteger,
    String,
    Text,
    DateTime,
    Boolean,
    Float,
    ForeignKey,
    Enum,
    Table,
    Index,
)
from sqlalchemy.orm import declarative_base, relationship
import enum

Base = declarative_base()


# ==================== Ассоциативная таблица Many-to-Many ====================
# Должна быть объявлена ДО моделей, так как используется в relationship

account_groups = Table(
    "account_groups",
    Base.metadata,
    Column("account_id", Integer, ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True),
    Column("group_id", Integer, ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True),
)


# ==================== Enums ====================

class AccountStatus(enum.Enum):
    """Статусы рабочего аккаунта."""
    ACTIVE = "active"           # Активен и готов к работе
    INACTIVE = "inactive"       # Деактивирован вручную
    BANNED = "banned"           # Забанен Telegram
    FLOOD_WAIT = "flood_wait"   # Временный блок (FloodWait)
    ERROR = "error"             # Ошибка сессии/авторизации
    SPAM_BLOCKED = "spam_blocked"  # Спам-блок от Telegram


class Membership(str, enum.Enum):
    """Принадлежность аккаунта."""
    READY = "READY"      # Готов к работе
    WARMUP = "WARMUP"    # На прогреве
    TEST = "TEST"        # Тестовый аккаунт


class ClientStatus(enum.Enum):
    """Статусы клиента в базе."""
    NEW = "new"                 # Новый, ещё не получал сообщения
    CONTACTED = "contacted"     # Уже было отправлено сообщение
    INVALID = "invalid"         # Невалидный username
    BLOCKED = "blocked"         # Заблокировал бота


class MailingStatus(enum.Enum):
    """Статусы рассылки."""
    DRAFT = "draft"             # Черновик
    PENDING = "pending"         # Ожидает запуска
    RUNNING = "running"         # В процессе
    PAUSED = "paused"           # На паузе
    COMPLETED = "completed"     # Завершена
    CANCELLED = "cancelled"     # Отменена
    ERROR = "error"             # Ошибка


class ProxyType(enum.Enum):
    """Типы прокси."""
    SOCKS5 = "socks5"
    HTTP = "http"
    MTProxy = "mtproxy"


class Proxy(Base):
    """
    Прокси для аккаунтов.
    """
    __tablename__ = "proxies"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False)  # Удобное имя (usa1, ger2)
    group_id = Column(Integer, ForeignKey("proxy_groups.id", ondelete="SET NULL"), nullable=True)
    
    # Данные прокси
    host = Column(String(255), nullable=False)
    port = Column(Integer, nullable=False)
    username = Column(String(100), nullable=True)
    password = Column(String(100), nullable=True)
    proxy_type = Column(Enum(ProxyType), default=ProxyType.SOCKS5)
    
    # Статус
    is_active = Column(Boolean, default=True)
    last_checked = Column(DateTime, nullable=True)
    is_working = Column(Boolean, default=True)  # Результат последней проверки
    
    # Метаданные
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Связи
    accounts = relationship("Account", back_populates="proxy")
    group = relationship("ProxyGroup", back_populates="proxies")
    
    def __repr__(self):
        return f"<Proxy {self.name} ({self.host}:{self.port})>"
    
    @property
    def connection_string(self) -> str:
        """Строка подключения для Telethon."""
        if self.username and self.password:
            return f"{self.username}:{self.password}@{self.host}:{self.port}"
        return f"{self.host}:{self.port}"


class Account(Base):
    """
    Рабочий аккаунт (userbot).
    """
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    phone = Column(String(20), unique=True, nullable=False)  # Номер телефона
    username = Column(String(100), unique=True, nullable=True)  # @username аккаунта
    session_name = Column(String(255), unique=True, nullable=False)  # Имя .session файла

    # Профиль аккаунта
    first_name = Column(String(100), nullable=True)  # Имя аккаунта
    last_name = Column(String(100), nullable=True)  # Фамилия аккаунта
    bio = Column(Text, nullable=True)  # Описание / Bio
    avatar_path = Column(String(255), nullable=True)  # Путь к аватарке

    # Принадлежность
    membership = Column(Enum(Membership), default=Membership.READY)

    # Теги (CSV в формате ",USA,Warmup,Main," — с запятыми по краям для точного LIKE)
    tags = Column(String(500), nullable=True, default="")

    # Подпись в списке аккаунтов бота (не меняет профиль Telegram)
    list_label = Column(String(64), nullable=True)

    # Прокси
    proxy_id = Column(Integer, ForeignKey("proxies.id"), nullable=True)
    proxy = relationship("Proxy", back_populates="accounts")

    # Статус
    status = Column(Enum(AccountStatus), default=AccountStatus.INACTIVE)

    # Статистика
    messages_sent = Column(Integer, default=0)
    messages_failed = Column(Integer, default=0)
    last_activity = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # FloodWait информация
    flood_wait_until = Column(DateTime, nullable=True)  # До какого времени блок

    # Лимиты
    daily_limit = Column(Integer, default=20)  # Лимит сообщений в день
    messages_today = Column(Integer, default=0)  # Отправлено сегодня
    last_reset = Column(DateTime, default=datetime.utcnow)  # Сброс счётчика

    # Спам-блок
    spam_check_date = Column(DateTime, nullable=True)
    is_spam_blocked = Column(Boolean, default=False)

    # Прогрев аккаунта (фаза 2)
    warmup_enabled = Column(Boolean, default=False)
    warmup_profile = Column(String(50), default="safe")
    warmup_actions_today = Column(Integer, default=0)
    warmup_last_action_at = Column(DateTime, nullable=True)
    warmup_next_run_at = Column(DateTime, nullable=True)
    warmup_paused_until = Column(DateTime, nullable=True)
    warmup_pause_reason = Column(String(255), nullable=True)

    # Метаданные
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Связи
    mailing_logs = relationship("MailingLog", back_populates="account")
    neuro_chat_messages = relationship("NeuroChatMessage", back_populates="account", cascade="all, delete-orphan")
    groups = relationship("Group", secondary=account_groups, back_populates="accounts")

    def __repr__(self):
        return f"<Account {self.username or self.phone} ({self.status.value})>"

    @property
    def display_title(self) -> str:
        """Подпись для списков и кнопок: list_label или @username или телефон."""
        if self.list_label and str(self.list_label).strip():
            return str(self.list_label).strip()
        if self.username:
            return self.username
        return self.phone or f"#{self.id}"

    @property
    def list_row_caption(self) -> str:
        """
        Строка списка аккаунтов: «название при загрузке[Имя Фамилия]».
        Без list_label слева — username/телефон; если имя неизвестно — «—» в скобках.
        """
        left = (self.list_label or "").strip() if self.list_label else ""
        if not left:
            left = self.username or self.phone or f"#{self.id}"
        fn = (self.first_name or "").strip() if self.first_name else ""
        ln = (self.last_name or "").strip() if self.last_name else ""
        full = f"{fn} {ln}".strip() or "—"
        return f"{left} [{full}]"


class ProxyGroup(Base):
    """
    Группа прокси для пулов (например USA).
    """
    __tablename__ = "proxy_groups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False)
    # Курсор round-robin по свободным прокси внутри группы.
    rr_cursor = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    proxies = relationship("Proxy", back_populates="group")

    def __repr__(self):
        return f"<ProxyGroup {self.name}>"


# ==================== Группы аккаунтов ====================

class Group(Base):
    """
    Группа аккаунтов для рассылок.
    """
    __tablename__ = "groups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False)  # Название группы (Колумбия, USA и т.д.)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Связи
    accounts = relationship("Account", secondary=account_groups, back_populates="groups")

    def __repr__(self):
        return f"<Group {self.name} (id={self.id})>"


class Client(Base):
    """
    Клиент для рассылки.
    """
    __tablename__ = "clients"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(100), unique=True, nullable=False)  # @username без @
    # Telegram user id (для изоляции диалогов нейрочата и привязки без @username)
    telegram_user_id = Column(BigInteger, nullable=True, unique=True)
    status = Column(Enum(ClientStatus), default=ClientStatus.NEW)
    
    # Метаданные
    added_at = Column(DateTime, default=datetime.utcnow)
    last_contacted_at = Column(DateTime, nullable=True)
    
    # Связи
    mailing_logs = relationship("MailingLog", back_populates="client")
    
    def __repr__(self):
        return f"<Client @{self.username}>"


class Mailing(Base):
    """
    Рассылка (кампания).
    """
    __tablename__ = "mailings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=True)  # Название для идентификации (с суффиксом)
    message_text = Column(Text, nullable=False, default="")  # Текст сообщения

    # Настройки рассылки
    status = Column(Enum(MailingStatus), default=MailingStatus.DRAFT)
    delay_between_messages = Column(Float, default=10.0)  # Задержка между сообщениями (сек)
    delay_between_accounts = Column(Float, default=10.0)  # Задержка между аккаунтами (сек)
    
    # Дополнительные настройки
    use_typing = Column(Boolean, default=True)  # Имитация набора текста
    typing_delay = Column(Float, default=3.0)  # Устар.: в воркере рассылки пауза 5–10 с случайно
    smart_delay = Column(Boolean, default=False)  # Умная задержка
    messages_per_batch = Column(Integer, default=10)  # Успешных на аккаунт за кампанию + ротация
    batch_delay = Column(Float, default=45.0)  # Задержка между пакетами (сек)
    # Автоматическое завершение через N часов. None/0 — работать до ручной остановки.
    auto_stop_hours = Column(Float, nullable=True, default=None)
    daily_limit = Column(Integer, default=20)  # Лимит сообщений в день на аккаунт
    
    # Статистика
    total_messages = Column(Integer, default=0)  # Сколько всего сообщений отправить
    messages_sent = Column(Integer, default=0)
    messages_failed = Column(Integer, default=0)
    unique_accounts_used = Column(Integer, default=0)

    # Метаданные
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Аудитория и контент (расширения)
    target_group_id = Column(Integer, ForeignKey("groups.id", ondelete="SET NULL"), nullable=True)
    # JSON-массив строк — дополнительные варианты текста (основной текст в message_text)
    message_variants_json = Column(Text, nullable=True, default="[]")
    # Зарезервировано под автоответы / нейросеть
    neurochat_enabled = Column(Boolean, default=False)
    # Идентификатор модели OpenRouter (например google/gemini-2.0-flash-001:free)
    neuro_model = Column(String(255), nullable=True)
    # Кастомная ссылка для плейсхолдера {link}
    community_link = Column(String(1024), nullable=True)

    # Связи
    logs = relationship("MailingLog", back_populates="mailing")
    target_group = relationship("Group", foreign_keys=[target_group_id])

    def __repr__(self):
        return f"<Mailing {self.name or self.id} ({self.status.value})>"


class MailingLog(Base):
    """
    Лог отдельного сообщения в рассылке.
    """
    __tablename__ = "mailing_logs"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    mailing_id = Column(Integer, ForeignKey("mailings.id"), nullable=False)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False)
    
    # Результат
    success = Column(Boolean, default=False)
    error_message = Column(Text, nullable=True)  # Текст ошибки если failed
    message_id = Column(Integer, nullable=True)  # ID отправленного сообщения
    
    # Метаданные
    sent_at = Column(DateTime, default=datetime.utcnow)
    
    # Связи
    mailing = relationship("Mailing", back_populates="logs")
    account = relationship("Account", back_populates="mailing_logs")
    client = relationship("Client", back_populates="mailing_logs")
    
    def __repr__(self):
        return f"<MailingLog {self.mailing_id} -> {self.client_id} ({'OK' if self.success else 'FAIL'})>"


class NeuroChatMessage(Base):
    """
    История сообщений для нейрочата (один диалог = account_id + peer_user_id в Telegram).
    """

    __tablename__ = "neuro_chat_messages"
    __table_args__ = (
        Index("ix_neuro_chat_account_peer", "account_id", "peer_user_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    peer_user_id = Column(BigInteger, nullable=False)
    role = Column(String(20), nullable=False)  # user | assistant
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    account = relationship("Account", back_populates="neuro_chat_messages")

    def __repr__(self):
        return f"<NeuroChat {self.account_id} peer={self.peer_user_id} {self.role}>"


class NeuroActionLog(Base):
    """
    Логи команд нейрочата для статистики ([SEND_LINK], [STOP]).
    """
    __tablename__ = "neuro_action_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mailing_id = Column(Integer, ForeignKey("mailings.id"), nullable=False)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False)
    action = Column(String(50), nullable=False)  # SEND_LINK | STOP
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<NeuroAction {self.action} mailing={self.mailing_id} client={self.client_id}>"


class NeuroStopList(Base):
    """
    STOP-лист нейрочата (кого не обслуживать после команды [STOP]).
    """
    __tablename__ = "neuro_stop_list"
    __table_args__ = (
        Index("ix_neuro_stop_account_client", "account_id", "client_id", unique=True),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    mailing_id = Column(Integer, ForeignKey("mailings.id"), nullable=False)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<NeuroStop mailing={self.mailing_id} account={self.account_id} client={self.client_id}>"


class WarmupProfile(Base):
    """
    Профиль прогрева аккаунтов.
    """
    __tablename__ = "warmup_profiles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(50), unique=True, nullable=False)  # safe, normal, custom...
    base_delay_sec = Column(Float, default=45.0)
    jitter_sec = Column(Float, default=25.0)
    daily_action_limit = Column(Integer, default=40)
    # Сообщества/чаты для активности (строка: по одному username/ссылке на строку).
    target_chats_text = Column(Text, nullable=True, default="")
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<WarmupProfile {self.name}>"


class WarmupLog(Base):
    """
    Лог действий прогрева по аккаунтам.
    """
    __tablename__ = "warmup_logs"
    __table_args__ = (
        Index("ix_warmup_logs_account_created", "account_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    action = Column(String(64), nullable=False)
    status = Column(String(20), nullable=False, default="ok")  # ok | skip | fail
    details = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<WarmupLog acc={self.account_id} {self.action} {self.status}>"
