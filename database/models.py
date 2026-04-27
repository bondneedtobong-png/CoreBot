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
    UniqueConstraint,
    JSON,
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

    # Режим автоматического ответа в нейрочате:
    # AI_ACTIVE — нейрочат отвечает сам (по обычному pipeline);
    # MANUAL    — нейрочат игнорирует входящие, оператор отвечает вручную из веб-панели.
    ai_mode = Column(String(20), nullable=False, default="AI_ACTIVE")

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
    client_interactions = relationship("ClientInteraction", back_populates="account")
    client_mail_sessions = relationship("ClientMailSession", back_populates="account")

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
    class_counters = relationship(
        "ClientClassCounter",
        back_populates="client",
        cascade="all, delete-orphan",
    )
    tags = relationship(
        "ClientTag",
        back_populates="client",
        cascade="all, delete-orphan",
    )
    interactions = relationship(
        "ClientInteraction",
        back_populates="client",
        cascade="all, delete-orphan",
    )
    mail_sessions = relationship(
        "ClientMailSession",
        back_populates="client",
        cascade="all, delete-orphan",
    )

    def __repr__(self):
        return f"<Client @{self.username}>"


class ClientClassCounter(Base):
    """
    Счётчики классов по клиенту (accept, pulse, bl, …) — монотонный рост.
    """

    __tablename__ = "client_class_counters"
    __table_args__ = (
        UniqueConstraint("client_id", "class_key", name="uq_client_class_counter"),
        Index("ix_class_counters_client", "client_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    client_id = Column(Integer, ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    class_key = Column(String(64), nullable=False)
    count = Column(Integer, nullable=False, default=0)

    client = relationship("Client", back_populates="class_counters")

    def __repr__(self):
        return f"<ClientClassCounter {self.class_key}={self.count}>"


class ClientTag(Base):
    """Произвольные теги пользователя."""

    __tablename__ = "client_tags"
    __table_args__ = (
        UniqueConstraint("client_id", "tag", name="uq_client_tag"),
        Index("ix_client_tags_tag", "tag"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    client_id = Column(Integer, ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    tag = Column(String(128), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    client = relationship("Client", back_populates="tags")

    def __repr__(self):
        return f"<ClientTag {self.tag}>"


class ClientInteraction(Base):
    """
    События: pulse, сообщения нейрочата, триггеры классов и т.д.
    """

    __tablename__ = "client_interactions"
    __table_args__ = (
        Index("ix_client_interactions_client_created", "client_id", "created_at"),
        Index("ix_client_interactions_mailing", "mailing_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    client_id = Column(Integer, ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True)
    mailing_id = Column(Integer, ForeignKey("mailings.id", ondelete="SET NULL"), nullable=True)
    direction = Column(String(8), nullable=False, default="in")  # in | out | system
    kind = Column(String(64), nullable=False)
    body = Column(Text, nullable=True)
    payload_json = Column(Text, nullable=True)
    telegram_message_id = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    client = relationship("Client", back_populates="interactions")
    account = relationship("Account", back_populates="client_interactions")
    mailing = relationship("Mailing", back_populates="client_interactions")


class ClientAliveWindow(Base):
    """
    Идемпотентное окно для инкремента класса alive.
    Один alive на (mailing, account, client, window_key).
    """

    __tablename__ = "client_alive_windows"
    __table_args__ = (
        UniqueConstraint(
            "mailing_id",
            "account_id",
            "client_id",
            "window_key",
            name="uq_client_alive_window",
        ),
        Index("ix_client_alive_windows_client", "client_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    mailing_id = Column(Integer, ForeignKey("mailings.id", ondelete="CASCADE"), nullable=False)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    client_id = Column(Integer, ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    window_key = Column(Integer, nullable=False)  # int(utc_timestamp // 3600)
    created_at = Column(DateTime, default=datetime.utcnow)


class ClientMailSession(Base):
    """
    Сессия рассылки по (клиент, аккаунт, рассылка).
    Для ретенции переписки и привязки accept-транскрипта.
    """

    __tablename__ = "client_mail_sessions"
    __table_args__ = (
        UniqueConstraint(
            "client_id",
            "account_id",
            "mailing_id",
            name="uq_client_mail_session",
        ),
        Index("ix_mail_sessions_client", "client_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    client_id = Column(Integer, ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    mailing_id = Column(Integer, ForeignKey("mailings.id", ondelete="CASCADE"), nullable=False)
    first_outbound_at = Column(DateTime, nullable=True)
    success_end_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    client = relationship("Client", back_populates="mail_sessions")
    account = relationship("Account", back_populates="client_mail_sessions")
    mailing = relationship("Mailing", back_populates="client_mail_sessions")
    accept_transcript = relationship(
        "ClientAcceptTranscript",
        back_populates="mail_session",
        uselist=False,
        cascade="all, delete-orphan",
    )


class ClientAcceptTranscript(Base):
    """
    Полная переписка от начала удачной рассылки до успешного конца (при accept).
    """

    __tablename__ = "client_accept_transcripts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mail_session_id = Column(
        Integer,
        ForeignKey("client_mail_sessions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    messages_json = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    mail_session = relationship("ClientMailSession", back_populates="accept_transcript")

    def __repr__(self):
        return "<ClientAcceptTranscript>"


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
    # Режим перебора вариантов первого сообщения: random | sequential
    variant_mode = Column(String(20), nullable=False, default="random")
    # Зарезервировано под автоответы / нейросеть
    neurochat_enabled = Column(Boolean, default=False)
    # Идентификатор модели OpenRouter (например openai/gpt-oss-120b:free)
    neuro_model = Column(String(255), nullable=True)
    # JSON: параметры сэмплирования (temperature, top_p, max_tokens, …) для OpenRouter
    neuro_sampling_json = Column(Text, nullable=True, default="{}")
    # Кастомная ссылка для плейсхолдера {link}
    community_link = Column(String(1024), nullable=True)
    # Фильтр очереди рассылки: JSON {"client_status":"new"|"open","include_classes":[],"exclude_classes":["bl"]}
    audience_filter_json = Column(Text, nullable=True)
    # Режим аудитории: test | new | classes (classes — фильтр по классам из audience_filter_json)
    audience_mode = Column(String(20), nullable=False, default="classes")
    # Лимит успешных первых сообщений за запуск (None — без лимита)
    max_recipients = Column(Integer, nullable=True)
    # Пауза рассылки (первое сообщение) для аккаунта после messages_per_batch успешных отправок
    mailing_cooldown_hours = Column(Float, nullable=False, default=12.0)

    # Связи
    logs = relationship("MailingLog", back_populates="mailing")
    target_group = relationship("Group", foreign_keys=[target_group_id])
    client_interactions = relationship("ClientInteraction", back_populates="mailing")
    client_mail_sessions = relationship("ClientMailSession", back_populates="mailing")

    def __repr__(self):
        return f"<Mailing {self.name or self.id} ({self.status.value})>"


class MailingTestRecipient(Base):
    """Тестовая аудитория рассылки: username из txt, привязка к Client для отправки и локальных классов."""

    __tablename__ = "mailing_test_recipients"
    __table_args__ = (
        UniqueConstraint("mailing_id", "username", name="uq_mailing_test_username"),
        Index("ix_mailing_test_mailing", "mailing_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    mailing_id = Column(Integer, ForeignKey("mailings.id", ondelete="CASCADE"), nullable=False)
    username = Column(String(255), nullable=False)
    client_id = Column(Integer, ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class MailingLocalClassCounter(Base):
    """Счётчики классов только внутри тестовой рассылки (audience_mode=test)."""

    __tablename__ = "mailing_local_class_counters"
    __table_args__ = (
        UniqueConstraint(
            "mailing_id", "client_id", "class_key", name="uq_mailing_local_class"
        ),
        Index("ix_mailing_local_mc", "mailing_id", "client_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    mailing_id = Column(Integer, ForeignKey("mailings.id", ondelete="CASCADE"), nullable=False)
    client_id = Column(Integer, ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    class_key = Column(String(64), nullable=False)
    count = Column(Integer, default=0)


class MailingAccountState(Base):
    """Волна первых сообщений и кулдаун рассылки по аккаунту в рамках кампании."""

    __tablename__ = "mailing_account_states"
    __table_args__ = (
        UniqueConstraint("mailing_id", "account_id", name="uq_mailing_account_state"),
        Index("ix_mas_mailing_cooldown", "mailing_id", "cooldown_until"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    mailing_id = Column(Integer, ForeignKey("mailings.id", ondelete="CASCADE"), nullable=False)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    sent_in_wave = Column(Integer, default=0)
    cooldown_until = Column(DateTime, nullable=True)


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


class InstanceSettings(Base):
    """
    Единственная строка настроек инстанса (id=1): ключи API и прочее.
    """

    __tablename__ = "instance_settings"

    id = Column(Integer, primary_key=True, autoincrement=False)
    # Зашифрованное или помеченное хранение ключа OpenRouter (см. utils/crypto_openrouter)
    openrouter_key_ciphertext = Column(Text, nullable=True)
    # Базовый UTC-сдвиг для плейсхолдеров {date}/{time}/… в первом сообщении (часы, −12…+14). None = брать из .env MAILING_BASE_UTC_OFFSET
    mailing_base_utc_offset = Column(Integer, nullable=True)
    # Глобальный toggle нейрочата: None = брать из .env NEUROCHAT_ENABLED
    neurochat_enabled = Column(Boolean, nullable=True)

    def __repr__(self):
        return "<InstanceSettings>"


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


class OutboundQueue(Base):
    """
    Очередь ручных исходящих сообщений из веб-панели.

    Веб-панель добавляет сюда строки со статусом 'pending'.
    Воркер бота (workers/outbound_consumer.py) поллит таблицу,
    отправляет через Telethon и обновляет статус.
    """
    __tablename__ = "outbound_queue"
    __table_args__ = (
        Index("ix_outbound_queue_status_created", "status", "created_at"),
        Index("ix_outbound_queue_account_peer", "account_id", "peer_user_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    peer_user_id = Column(BigInteger, nullable=False)
    client_id = Column(Integer, ForeignKey("clients.id", ondelete="SET NULL"), nullable=True)
    text = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default="pending")  # pending | sending | sent | failed | cancelled
    error = Column(Text, nullable=True)
    telegram_message_id = Column(BigInteger, nullable=True)
    requested_by = Column(String(120), nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    sent_at = Column(DateTime, nullable=True)

    def __repr__(self):
        return f"<OutboundQueue id={self.id} acc={self.account_id} peer={self.peer_user_id} {self.status}>"


# ==================== Архив (soft-delete) ====================


class NeuroChatMessageArchive(Base):
    """
    Архив переписки нейрочата. Сюда переносятся строки из neuro_chat_messages
    при cleanup mode='archive'. Сохраняем оригинальный id (как original_id),
    чтобы restore был идемпотентным.
    """

    __tablename__ = "neuro_chat_messages_archive"
    __table_args__ = (
        Index(
            "ix_neuro_archive_account_peer", "account_id", "peer_user_id", "created_at"
        ),
        Index("ix_neuro_archive_archived_at", "archived_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    original_id = Column(Integer, nullable=False, index=True)
    account_id = Column(Integer, nullable=False)
    peer_user_id = Column(BigInteger, nullable=False)
    role = Column(String(20), nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False)
    archived_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<NeuroChatMessageArchive original_id={self.original_id}>"


class ClientInteractionArchive(Base):
    """
    Архив client_interactions для истории. Используется при cleanup mode='archive'.
    """

    __tablename__ = "client_interactions_archive"
    __table_args__ = (
        Index(
            "ix_client_inter_archive_client_created",
            "client_id",
            "created_at",
        ),
        Index("ix_client_inter_archive_archived_at", "archived_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    original_id = Column(Integer, nullable=False, index=True)
    client_id = Column(Integer, nullable=False)
    account_id = Column(Integer, nullable=True)
    mailing_id = Column(Integer, nullable=True)
    direction = Column(String(8), nullable=False)
    kind = Column(String(64), nullable=False)
    body = Column(Text, nullable=True)
    payload_json = Column(Text, nullable=True)
    telegram_message_id = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, nullable=False)
    archived_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<ClientInteractionArchive original_id={self.original_id}>"


# ==================== Команды для бота из веб-панели ====================


class BotCommand(Base):
    """
    Очередь команд от веб-панели → к боту (внутри одного процесса бота
    воркер `workers/bot_command_consumer.py` поллит таблицу и исполняет
    команды через `worker_manager`).

    command:
      - 'mailing.start'   args: {"mailing_id": int}
      - 'mailing.pause'   args: {"mailing_id": int}
      - 'mailing.stop'    args: {"mailing_id": int}
    Расширение под будущие команды (account.*, neurochat.*, …) — через тот же
    механизм без изменения схемы.
    """

    __tablename__ = "bot_commands"
    __table_args__ = (
        Index("ix_bot_commands_status_created", "status", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    command = Column(String(64), nullable=False)
    args_json = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default="pending")  # pending|done|failed|cancelled
    error = Column(Text, nullable=True)
    requested_by = Column(String(120), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    processed_at = Column(DateTime, nullable=True)

    def __repr__(self):
        return f"<BotCommand id={self.id} {self.command} {self.status}>"


# ==================== Telegram parsing (parser-worker + web panel) ====================


class ParsingTask(Base):
    """
    Задача парсинга Telegram (очередь для parser-worker).

    kind: channels | groups | users
    status: pending | running | completed | failed | cancelled
    mode: max_coverage | active_only (для users; для channels/groups — резерв)
    """

    __tablename__ = "parsing_tasks"
    __table_args__ = (
        Index("ix_parsing_tasks_status_created", "status", "created_at"),
        Index("ix_parsing_tasks_kind_status", "kind", "status"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    kind = Column(String(32), nullable=False)
    status = Column(String(32), nullable=False, default="pending")

    params_json = Column(JSON, nullable=False, default=dict)
    accounts_json = Column(JSON, nullable=False, default=list)

    progress_percent = Column(Integer, nullable=False, default=0)
    current_stage = Column(String(255), nullable=True)
    current_account_id = Column(Integer, ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True)
    current_query = Column(String(512), nullable=True)

    found_count = Column(Integer, nullable=False, default=0)
    filtered_count = Column(Integer, nullable=False, default=0)
    error_count = Column(Integer, nullable=False, default=0)

    depth = Column(Integer, nullable=False, default=1)
    mode = Column(String(32), nullable=False, default="max_coverage")

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    requested_by = Column(String(120), nullable=True)
    last_error = Column(Text, nullable=True)

    def __repr__(self):
        return f"<ParsingTask id={self.id} {self.kind} {self.status}>"


class ParsingTaskLog(Base):
    """Структурированные логи выполнения parsing task."""

    __tablename__ = "parsing_task_logs"
    __table_args__ = (
        Index("ix_parsing_task_logs_task_created", "task_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(Integer, ForeignKey("parsing_tasks.id", ondelete="CASCADE"), nullable=False)
    level = Column(String(16), nullable=False, default="info")  # info | warn | error
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True)
    event = Column(String(64), nullable=False)
    message = Column(Text, nullable=True)
    payload_json = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    task = relationship("ParsingTask", backref="logs", foreign_keys=[task_id])


class ParsedChannel(Base):
    __tablename__ = "parsed_channels"
    __table_args__ = (
        UniqueConstraint("telegram_id", name="uq_parsed_channels_telegram_id"),
        Index("ix_parsed_channels_source_task", "source_task_id"),
        Index("ix_parsed_channels_active", "is_active_7d"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, nullable=False)
    username = Column(String(255), nullable=True)
    title = Column(String(512), nullable=True)
    subscribers = Column(Integer, nullable=True)
    is_public = Column(Boolean, nullable=True)
    has_discussion = Column(Boolean, nullable=True)
    lang = Column(String(16), nullable=True)
    last_post_at = Column(DateTime, nullable=True)
    is_active_7d = Column(Boolean, nullable=True)
    source_task_id = Column(Integer, ForeignKey("parsing_tasks.id", ondelete="SET NULL"), nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<ParsedChannel {self.telegram_id} @{self.username}>"


class ParsedGroup(Base):
    __tablename__ = "parsed_groups"
    __table_args__ = (
        UniqueConstraint("telegram_id", name="uq_parsed_groups_telegram_id"),
        Index("ix_parsed_groups_source_task", "source_task_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, nullable=False)
    username = Column(String(255), nullable=True)
    title = Column(String(512), nullable=True)
    members_count = Column(Integer, nullable=True)
    group_type = Column(String(32), nullable=True)  # public | private
    lang = Column(String(16), nullable=True)
    is_active_7d = Column(Boolean, nullable=True)
    source_task_id = Column(Integer, ForeignKey("parsing_tasks.id", ondelete="SET NULL"), nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<ParsedGroup {self.telegram_id}>"


class ParsedUser(Base):
    __tablename__ = "parsed_users"
    __table_args__ = (
        UniqueConstraint("telegram_id", name="uq_parsed_users_telegram_id"),
        Index("ix_parsed_users_source_task", "source_task_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, nullable=False)
    username = Column(String(255), nullable=True)
    display_name = Column(String(512), nullable=True)
    has_avatar = Column(Boolean, nullable=True)
    last_seen_at = Column(DateTime, nullable=True)
    lang_guess = Column(String(16), nullable=True)
    is_deleted = Column(Boolean, nullable=False, default=False)
    is_suspicious = Column(Boolean, nullable=False, default=False)
    source_task_id = Column(Integer, ForeignKey("parsing_tasks.id", ondelete="SET NULL"), nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<ParsedUser {self.telegram_id}>"


class ParsedUserSource(Base):
    """Связь пользователя с источником (канал/группа) и ролью сбора."""

    __tablename__ = "parsed_user_sources"
    __table_args__ = (
        UniqueConstraint(
            "parsed_user_id",
            "source_entity_id",
            "source_entity_kind",
            "source_kind",
            name="uq_parsed_user_source_edge",
        ),
        Index("ix_parsed_user_sources_user", "parsed_user_id"),
        Index("ix_parsed_user_sources_source", "source_entity_id", "source_entity_kind"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    parsed_user_id = Column(Integer, ForeignKey("parsed_users.id", ondelete="CASCADE"), nullable=False)
    source_entity_id = Column(BigInteger, nullable=False)
    source_entity_kind = Column(String(16), nullable=False)  # channel | group
    source_kind = Column(String(32), nullable=False)  # member | active | commenter
    source_task_id = Column(Integer, ForeignKey("parsing_tasks.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("ParsedUser", backref="sources", foreign_keys=[parsed_user_id])
