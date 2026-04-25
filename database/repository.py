"""
Инициализация базы данных и сессии SQLAlchemy.
Автоматическая миграция при запуске.
"""
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import text, event

from bot.config import DATABASE_URL
from database.models import Base
from utils.logger import log


class Database:
    """
    Класс для управления подключением к базе данных.
    """

    def __init__(self, url: str):
        self.url = url
        self.engine = None
        self.async_session_maker = None
        # Блокировка для предотвращения конкурентных записей в SQLite
        self._lock = asyncio.Lock()

    async def connect(self):
        """Создание подключения к БД и выполнение миграций."""
        self.engine = create_async_engine(
            self.url,
            echo=False,
            future=True,
            # Настройки для SQLite — предотвращают "database is locked"
            connect_args={
                "timeout": 30,       # Ждём до 30 сек перед блокировкой
                "check_same_thread": False,
            },
            pool_pre_ping=True,     # Проверка подключения перед использованием
        )

        # Включаем WAL mode и busy_timeout для SQLite
        @event.listens_for(self.engine.sync_engine, "connect")
        def set_sqlite_pragma(dbapi_conn, connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")  # 30 секунд
            cursor.execute("PRAGMA wal_autocheckpoint=1000")
            cursor.close()

        self.async_session_maker = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

        # Создание таблиц
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        # Выполнение миграций
        await self._run_migrations()

    async def _run_migrations(self):
        """
        Автоматическая миграция таблицы accounts.
        Конвертирует старые русские значения в новые английские.
        """
        try:
            async with self.engine.begin() as conn:
                # Проверяем, существует ли таблица accounts
                result = await conn.execute(text(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='accounts'"
                ))
                if not result.fetchone():
                    return  # Таблицы нет, миграция не нужна

                # Проверяем, существует ли колонка membership
                result = await conn.execute(text("PRAGMA table_info(accounts)"))
                columns = {row[1]: row[2] for row in result.fetchall()}

                if 'membership' not in columns:
                    log.info("➕ Добавление колонки membership в таблицу accounts")
                    await conn.execute(text(
                        "ALTER TABLE accounts ADD COLUMN membership VARCHAR(20) DEFAULT 'READY'"
                    ))
                    log.info("✅ Колонка membership добавлена")

                # Конвертация старых русских значений в новые английские
                log.info("🔄 Конвертация старых значений membership...")

                # Обновляем русские значения на английские
                await conn.execute(text(
                    "UPDATE accounts SET membership = 'READY' WHERE membership = 'готов' OR membership IS NULL"
                ))
                await conn.execute(text(
                    "UPDATE accounts SET membership = 'WARMUP' WHERE membership = 'прогрев'"
                ))
                await conn.execute(text(
                    "UPDATE accounts SET membership = 'TEST' WHERE membership = 'тест'"
                ))

                log.info("✅ Миграция membership завершена")

                # --- Миграция: добавление колонки tags ---
                if 'tags' not in columns:
                    log.info("➕ Добавление колонки tags в таблицу accounts")
                    await conn.execute(text(
                        "ALTER TABLE accounts ADD COLUMN tags VARCHAR(500) DEFAULT ''"
                    ))
                    log.info("✅ Колонка tags добавлена")

                # --- account warmup columns ---
                a_info = await conn.execute(text("PRAGMA table_info(accounts)"))
                acols = {row[1] for row in a_info.fetchall()}
                warmup_columns = {
                    "warmup_enabled": "ALTER TABLE accounts ADD COLUMN warmup_enabled BOOLEAN DEFAULT 0",
                    "warmup_profile": "ALTER TABLE accounts ADD COLUMN warmup_profile VARCHAR(50) DEFAULT 'safe'",
                    "warmup_actions_today": "ALTER TABLE accounts ADD COLUMN warmup_actions_today INTEGER DEFAULT 0",
                    "warmup_last_action_at": "ALTER TABLE accounts ADD COLUMN warmup_last_action_at DATETIME",
                    "warmup_next_run_at": "ALTER TABLE accounts ADD COLUMN warmup_next_run_at DATETIME",
                    "warmup_paused_until": "ALTER TABLE accounts ADD COLUMN warmup_paused_until DATETIME",
                    "warmup_pause_reason": "ALTER TABLE accounts ADD COLUMN warmup_pause_reason VARCHAR(255)",
                }
                for col, sql in warmup_columns.items():
                    if col not in acols:
                        log.info(f"➕ accounts: {col}")
                        await conn.execute(text(sql))

                a_info2 = await conn.execute(text("PRAGMA table_info(accounts)"))
                acols2 = {row[1] for row in a_info2.fetchall()}
                if "list_label" not in acols2:
                    log.info("➕ accounts: list_label")
                    await conn.execute(
                        text("ALTER TABLE accounts ADD COLUMN list_label VARCHAR(64)")
                    )
                if "ai_mode" not in acols2:
                    log.info("➕ accounts: ai_mode (AI_ACTIVE | MANUAL)")
                    await conn.execute(
                        text(
                            "ALTER TABLE accounts ADD COLUMN ai_mode VARCHAR(20) "
                            "NOT NULL DEFAULT 'AI_ACTIVE'"
                        )
                    )

                # --- Миграция: добавление таблицы groups ---
                groups_exists = await conn.execute(text(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='groups'"
                ))
                if not groups_exists.fetchone():
                    log.info("➕ Создание таблицы groups")
                    await conn.execute(text("""
                        CREATE TABLE groups (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            name VARCHAR(100) UNIQUE NOT NULL,
                            created_at DATETIME DEFAULT (datetime('now'))
                        )
                    """))
                    log.info("✅ Таблица groups создана")

                # --- Миграция: добавление таблицы account_groups ---
                ag_exists = await conn.execute(text(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='account_groups'"
                ))
                if not ag_exists.fetchone():
                    log.info("➕ Создание таблицы account_groups")
                    await conn.execute(text("""
                        CREATE TABLE account_groups (
                            account_id INTEGER NOT NULL REFERENCES accounts(id),
                            group_id INTEGER NOT NULL REFERENCES groups(id),
                            PRIMARY KEY (account_id, group_id)
                        )
                    """))
                    log.info("✅ Таблица account_groups создана")

                # --- warmup profiles table ---
                wp_exists = await conn.execute(text(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='warmup_profiles'"
                ))
                if not wp_exists.fetchone():
                    log.info("➕ Создание таблицы warmup_profiles")
                    await conn.execute(text("""
                        CREATE TABLE warmup_profiles (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            name VARCHAR(50) UNIQUE NOT NULL,
                            base_delay_sec FLOAT DEFAULT 45.0,
                            jitter_sec FLOAT DEFAULT 25.0,
                            daily_action_limit INTEGER DEFAULT 40,
                            target_chats_text TEXT DEFAULT '',
                            enabled BOOLEAN DEFAULT 1,
                            created_at DATETIME DEFAULT (datetime('now')),
                            updated_at DATETIME DEFAULT (datetime('now'))
                        )
                    """))
                    await conn.execute(text(
                        "INSERT INTO warmup_profiles(name, base_delay_sec, jitter_sec, daily_action_limit, enabled) "
                        "VALUES ('safe', 45.0, 25.0, 40, 1)"
                    ))
                    log.info("✅ Таблица warmup_profiles создана")
                else:
                    wp_info = await conn.execute(text("PRAGMA table_info(warmup_profiles)"))
                    wp_cols = {row[1] for row in wp_info.fetchall()}
                    if "target_chats_text" not in wp_cols:
                        log.info("➕ warmup_profiles: target_chats_text")
                        await conn.execute(text(
                            "ALTER TABLE warmup_profiles ADD COLUMN target_chats_text TEXT DEFAULT ''"
                        ))

                # --- warmup logs table ---
                wl_exists = await conn.execute(text(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='warmup_logs'"
                ))
                if not wl_exists.fetchone():
                    log.info("➕ Создание таблицы warmup_logs")
                    await conn.execute(text("""
                        CREATE TABLE warmup_logs (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            account_id INTEGER NOT NULL REFERENCES accounts(id),
                            action VARCHAR(64) NOT NULL,
                            status VARCHAR(20) NOT NULL DEFAULT 'ok',
                            details TEXT,
                            created_at DATETIME DEFAULT (datetime('now'))
                        )
                    """))
                    await conn.execute(text(
                        "CREATE INDEX IF NOT EXISTS ix_warmup_logs_account_created "
                        "ON warmup_logs(account_id, created_at)"
                    ))
                    log.info("✅ Таблица warmup_logs создана")

                # --- proxy groups / pools ---
                pg_exists = await conn.execute(text(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='proxy_groups'"
                ))
                if not pg_exists.fetchone():
                    log.info("➕ Создание таблицы proxy_groups")
                    await conn.execute(text("""
                        CREATE TABLE proxy_groups (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            name VARCHAR(100) UNIQUE NOT NULL,
                            rr_cursor INTEGER DEFAULT 0,
                            created_at DATETIME DEFAULT (datetime('now'))
                        )
                    """))
                    log.info("✅ Таблица proxy_groups создана")

                p_exists = await conn.execute(text(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='proxies'"
                ))
                if p_exists.fetchone():
                    p_info = await conn.execute(text("PRAGMA table_info(proxies)"))
                    pcols = {row[1] for row in p_info.fetchall()}
                    if "group_id" not in pcols:
                        log.info("➕ proxies: group_id")
                        await conn.execute(text(
                            "ALTER TABLE proxies ADD COLUMN group_id INTEGER"
                        ))

                # --- Миграция: колонки mailings (группа, варианты текста, нейрочат) ---
                m_exists = await conn.execute(text(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='mailings'"
                ))
                if m_exists.fetchone():
                    m_info = await conn.execute(text("PRAGMA table_info(mailings)"))
                    mcols = {row[1] for row in m_info.fetchall()}
                    if "target_group_id" not in mcols:
                        log.info("➕ mailings: target_group_id")
                        await conn.execute(text(
                            "ALTER TABLE mailings ADD COLUMN target_group_id INTEGER"
                        ))
                    if "message_variants_json" not in mcols:
                        log.info("➕ mailings: message_variants_json")
                        await conn.execute(text(
                            "ALTER TABLE mailings ADD COLUMN message_variants_json TEXT DEFAULT '[]'"
                        ))
                    if "variant_mode" not in mcols:
                        log.info("➕ mailings: variant_mode")
                        await conn.execute(text(
                            "ALTER TABLE mailings ADD COLUMN variant_mode VARCHAR(20) DEFAULT 'random'"
                        ))
                    if "neurochat_enabled" not in mcols:
                        log.info("➕ mailings: neurochat_enabled")
                        await conn.execute(text(
                            "ALTER TABLE mailings ADD COLUMN neurochat_enabled BOOLEAN DEFAULT 0"
                        ))
                    if "neuro_model" not in mcols:
                        log.info("➕ mailings: neuro_model")
                        await conn.execute(text(
                            "ALTER TABLE mailings ADD COLUMN neuro_model VARCHAR(255)"
                        ))
                    if "auto_stop_hours" not in mcols:
                        log.info("➕ mailings: auto_stop_hours")
                        await conn.execute(text(
                            "ALTER TABLE mailings ADD COLUMN auto_stop_hours FLOAT"
                        ))
                    if "community_link" not in mcols:
                        log.info("➕ mailings: community_link")
                        await conn.execute(text(
                            "ALTER TABLE mailings ADD COLUMN community_link VARCHAR(1024)"
                        ))
                    if "first_phase_per_account" not in mcols:
                        log.info("➕ mailings: first_phase_per_account")
                        await conn.execute(text(
                            "ALTER TABLE mailings ADD COLUMN first_phase_per_account INTEGER DEFAULT 0"
                        ))
                    if "auto_neuro_after_first_phase" not in mcols:
                        log.info("➕ mailings: auto_neuro_after_first_phase")
                        await conn.execute(text(
                            "ALTER TABLE mailings ADD COLUMN auto_neuro_after_first_phase BOOLEAN DEFAULT 1"
                        ))
                    if "neuro_sampling_json" not in mcols:
                        log.info("➕ mailings: neuro_sampling_json")
                        await conn.execute(text(
                            "ALTER TABLE mailings ADD COLUMN neuro_sampling_json TEXT DEFAULT '{}'"
                        ))
                    if "audience_filter_json" not in mcols:
                        log.info("➕ mailings: audience_filter_json")
                        await conn.execute(text(
                            "ALTER TABLE mailings ADD COLUMN audience_filter_json TEXT"
                        ))
                    if "audience_mode" not in mcols:
                        log.info("➕ mailings: audience_mode")
                        await conn.execute(text(
                            "ALTER TABLE mailings ADD COLUMN audience_mode VARCHAR(20) DEFAULT 'classes'"
                        ))
                    if "max_recipients" not in mcols:
                        log.info("➕ mailings: max_recipients")
                        await conn.execute(text(
                            "ALTER TABLE mailings ADD COLUMN max_recipients INTEGER"
                        ))
                    if "mailing_cooldown_hours" not in mcols:
                        log.info("➕ mailings: mailing_cooldown_hours")
                        await conn.execute(text(
                            "ALTER TABLE mailings ADD COLUMN mailing_cooldown_hours FLOAT DEFAULT 12.0"
                        ))

                # --- instance_settings (ключ OpenRouter и др.) ---
                ins_exists = await conn.execute(text(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='instance_settings'"
                ))
                if not ins_exists.fetchone():
                    log.info("➕ Создание таблицы instance_settings")
                    await conn.execute(text("""
                        CREATE TABLE instance_settings (
                            id INTEGER PRIMARY KEY,
                            openrouter_key_ciphertext TEXT,
                            mailing_base_utc_offset INTEGER,
                            neurochat_enabled BOOLEAN
                        )
                    """))
                    await conn.execute(text(
                        "INSERT INTO instance_settings (id) VALUES (1)"
                    ))
                    log.info("✅ Таблица instance_settings создана")
                else:
                    ins_info = await conn.execute(text("PRAGMA table_info(instance_settings)"))
                    iscols = {row[1] for row in ins_info.fetchall()}
                    if "mailing_base_utc_offset" not in iscols:
                        log.info("➕ instance_settings: mailing_base_utc_offset")
                        await conn.execute(text(
                            "ALTER TABLE instance_settings ADD COLUMN mailing_base_utc_offset INTEGER"
                        ))
                    if "neurochat_enabled" not in iscols:
                        log.info("➕ instance_settings: neurochat_enabled")
                        await conn.execute(text(
                            "ALTER TABLE instance_settings ADD COLUMN neurochat_enabled BOOLEAN"
                        ))

                # --- clients: telegram_user_id ---
                c_exists = await conn.execute(text(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='clients'"
                ))
                if c_exists.fetchone():
                    c_info = await conn.execute(text("PRAGMA table_info(clients)"))
                    ccols = {row[1] for row in c_info.fetchall()}
                    if "telegram_user_id" not in ccols:
                        log.info("➕ clients: telegram_user_id")
                        await conn.execute(text(
                            "ALTER TABLE clients ADD COLUMN telegram_user_id INTEGER"
                        ))

                # --- neuro action logs table ---
                nal_exists = await conn.execute(text(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='neuro_action_logs'"
                ))
                if not nal_exists.fetchone():
                    log.info("➕ Создание таблицы neuro_action_logs")
                    await conn.execute(text("""
                        CREATE TABLE neuro_action_logs (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            mailing_id INTEGER NOT NULL REFERENCES mailings(id),
                            account_id INTEGER NOT NULL REFERENCES accounts(id),
                            client_id INTEGER NOT NULL REFERENCES clients(id),
                            action VARCHAR(50) NOT NULL,
                            created_at DATETIME DEFAULT (datetime('now'))
                        )
                    """))
                    log.info("✅ Таблица neuro_action_logs создана")

                # --- neuro stop list table ---
                nsl_exists = await conn.execute(text(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='neuro_stop_list'"
                ))
                if not nsl_exists.fetchone():
                    log.info("➕ Создание таблицы neuro_stop_list")
                    await conn.execute(text("""
                        CREATE TABLE neuro_stop_list (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            mailing_id INTEGER NOT NULL REFERENCES mailings(id),
                            account_id INTEGER NOT NULL REFERENCES accounts(id),
                            client_id INTEGER NOT NULL REFERENCES clients(id),
                            created_at DATETIME DEFAULT (datetime('now'))
                        )
                    """))
                    await conn.execute(text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS ix_neuro_stop_account_client "
                        "ON neuro_stop_list(account_id, client_id)"
                    ))
                    log.info("✅ Таблица neuro_stop_list создана")

                # --- outbound queue (manual sends from web-panel) ---
                ob_exists = await conn.execute(text(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='outbound_queue'"
                ))
                if not ob_exists.fetchone():
                    log.info("➕ Создание таблицы outbound_queue")
                    await conn.execute(text("""
                        CREATE TABLE outbound_queue (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                            peer_user_id BIGINT NOT NULL,
                            client_id INTEGER REFERENCES clients(id) ON DELETE SET NULL,
                            text TEXT NOT NULL,
                            status VARCHAR(20) NOT NULL DEFAULT 'pending',
                            error TEXT,
                            telegram_message_id BIGINT,
                            requested_by VARCHAR(120),
                            created_at DATETIME DEFAULT (datetime('now')),
                            sent_at DATETIME
                        )
                    """))
                    await conn.execute(text(
                        "CREATE INDEX IF NOT EXISTS ix_outbound_queue_status_created "
                        "ON outbound_queue(status, created_at)"
                    ))
                    await conn.execute(text(
                        "CREATE INDEX IF NOT EXISTS ix_outbound_queue_account_peer "
                        "ON outbound_queue(account_id, peer_user_id)"
                    ))
                    log.info("✅ Таблица outbound_queue создана")

                # --- client alive windows table ---
                caw_exists = await conn.execute(text(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='client_alive_windows'"
                ))
                if not caw_exists.fetchone():
                    log.info("➕ Создание таблицы client_alive_windows")
                    await conn.execute(text("""
                        CREATE TABLE client_alive_windows (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            mailing_id INTEGER NOT NULL REFERENCES mailings(id),
                            account_id INTEGER NOT NULL REFERENCES accounts(id),
                            client_id INTEGER NOT NULL REFERENCES clients(id),
                            window_key INTEGER NOT NULL,
                            created_at DATETIME DEFAULT (datetime('now'))
                        )
                    """))
                    await conn.execute(text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS uq_client_alive_window "
                        "ON client_alive_windows(mailing_id, account_id, client_id, window_key)"
                    ))
                    await conn.execute(text(
                        "CREATE INDEX IF NOT EXISTS ix_client_alive_windows_client "
                        "ON client_alive_windows(client_id)"
                    ))
                    log.info("✅ Таблица client_alive_windows создана")

                log.info("✅ Все миграции завершены")

        except Exception as e:
            log.error(f"⚠️ Ошибка при выполнении миграции: {e}")
            # Не прерываем работу, продолжаем запуск

    async def disconnect(self):
        """Закрытие подключения."""
        if self.engine:
            await self.engine.dispose()

    async def get_session(self) -> AsyncSession:
        """Получение сессии для работы с БД."""
        async with self.async_session_maker() as session:
            yield session

    async def locked_execute(self, coro):
        """
        Выполнение корутины с блокировкой для предотвращения
        конкурентных записей в SQLite.

        Используйте для операций записи (INSERT, UPDATE, DELETE).
        """
        async with self._lock:
            return await coro


# Глобальный экземпляр
db = Database(DATABASE_URL)
