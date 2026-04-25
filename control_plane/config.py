import os


CP_DATABASE_URL = os.getenv("CP_DATABASE_URL", "sqlite:///data/control_plane.db")
CP_JWT_SECRET = os.getenv("CP_JWT_SECRET", "change-me-in-production")
CP_JWT_ALG = "HS256"
CP_ACCESS_TTL_MIN = int(os.getenv("CP_ACCESS_TTL_MIN", "60"))
CP_REFRESH_TTL_MIN = int(os.getenv("CP_REFRESH_TTL_MIN", str(60 * 24 * 14)))

CP_BOOTSTRAP_ADMIN_USERNAME = os.getenv("CP_BOOTSTRAP_ADMIN_USERNAME", "admin")
CP_BOOTSTRAP_ADMIN_PASSWORD = os.getenv("CP_BOOTSTRAP_ADMIN_PASSWORD", "admin123")

CP_INGEST_SUPPRESSION_SEC = int(os.getenv("CP_INGEST_SUPPRESSION_SEC", "300"))
CP_RETENTION_DAYS = int(os.getenv("CP_RETENTION_DAYS", "30"))

CP_TELEGRAM_BOT_TOKEN = os.getenv("CP_TELEGRAM_BOT_TOKEN", "").strip()
CP_TELEGRAM_ALERT_CHAT_ID = os.getenv("CP_TELEGRAM_ALERT_CHAT_ID", "").strip()

# === Бизнес-данные бота (corebot.db) ===
# Sync-URL до основной базы бота. По умолчанию — относительный путь рядом с
# репозиторием. На VPS обычно /opt/corebot/app/data/corebot.db.
# Если в .env используется DATABASE_URL вида sqlite+aiosqlite:///..., здесь
# нужен sync-вариант: sqlite:///...
BOT_DATABASE_URL = os.getenv("BOT_DATABASE_URL", "sqlite:///data/corebot.db")
# Лимит сообщений за один SSE-tick (от больших всплесков отгораживаемся).
CP_BUSINESS_STREAM_BATCH = int(os.getenv("CP_BUSINESS_STREAM_BATCH", "200"))
# Период опроса БД для SSE-стрима (секунды).
CP_BUSINESS_STREAM_INTERVAL = float(os.getenv("CP_BUSINESS_STREAM_INTERVAL", "1.5"))
