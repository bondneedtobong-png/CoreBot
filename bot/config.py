"""
Конфигурация приложения.
Загрузка переменных окружения и настройка параметров.
"""
import os
from typing import Optional
from pathlib import Path
from dotenv import load_dotenv

from utils.logger import log

# Загружаем переменные окружения
load_dotenv()

# Базовая директория проекта
BASE_DIR = Path(__file__).parent.parent

# Telegram API
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Владелец бота
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

# База данных
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite+aiosqlite:///{BASE_DIR}/data/corebot.db")

# Логирование
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Настройки рассылок
DEFAULT_DELAY_BETWEEN_MESSAGES = int(os.getenv("DEFAULT_DELAY_BETWEEN_MESSAGES", "5"))
DEFAULT_DELAY_BETWEEN_ACCOUNTS = int(os.getenv("DEFAULT_DELAY_BETWEEN_ACCOUNTS", "10"))
MAX_RETRIES_ON_FLOOD = int(os.getenv("MAX_RETRIES_ON_FLOOD", "3"))

# Экономия трафика / безопасный режим (anti-ban hardening)
BANDWIDTH_SAVER_MODE = os.getenv("BANDWIDTH_SAVER_MODE", "1").strip().lower() in ("1", "true", "yes", "on")
# Не дёргать расширенные запросы профиля при каждом connect
BANDWIDTH_SKIP_PROFILE_ENRICH = os.getenv("BANDWIDTH_SKIP_PROFILE_ENRICH", "1").strip().lower() in ("1", "true", "yes", "on")
# Не трогать SpamBot автоматически в обычных сценариях
BANDWIDTH_SKIP_SPAMBOT_CHECK = os.getenv("BANDWIDTH_SKIP_SPAMBOT_CHECK", "1").strip().lower() in ("1", "true", "yes", "on")

# Фаза 2: параметры прогрева (каркас)
WARMUP_ENABLED = os.getenv("WARMUP_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
WARMUP_BASE_DELAY_SEC = float(os.getenv("WARMUP_BASE_DELAY_SEC", "45"))
WARMUP_JITTER_SEC = float(os.getenv("WARMUP_JITTER_SEC", "25"))
WARMUP_DAILY_ACTION_LIMIT = int(os.getenv("WARMUP_DAILY_ACTION_LIMIT", "40"))

# OpenRouter / нейрочат
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip()
OPENROUTER_HTTP_REFERER = os.getenv("OPENROUTER_HTTP_REFERER", "").strip()
DEFAULT_NEURO_MODEL = os.getenv("DEFAULT_NEURO_MODEL", "google/gemini-2.0-flash-001:free").strip()
NEURO_FALLBACK_MODELS = [
    x.strip() for x in os.getenv("NEURO_FALLBACK_MODELS", "").split(",") if x.strip()
]
NEURO_MAX_CONCURRENT = int(os.getenv("NEURO_MAX_CONCURRENT", "8"))
NEURO_HISTORY_LIMIT = int(os.getenv("NEURO_HISTORY_LIMIT", "20"))
NEURO_MAX_TOKENS = int(os.getenv("NEURO_MAX_TOKENS", "1024"))
NEURO_OPENROUTER_MAX_RETRIES = int(os.getenv("NEURO_OPENROUTER_MAX_RETRIES", "3"))
NEURO_OPENROUTER_RETRY_BASE_SEC = float(os.getenv("NEURO_OPENROUTER_RETRY_BASE_SEC", "2"))
NEURO_UNAVAILABLE_TEMPLATE = os.getenv(
    "NEURO_UNAVAILABLE_TEMPLATE",
    "Сейчас отвечаю с задержкой из-за нагрузки. Напишите, пожалуйста, чуть позже.",
).strip()
DEFAULT_NEURO_SYSTEM_PROMPT = (
    "Ты ведёшь переписку от лица проекта: естественно, кратко, по делу.\n\n"
    "Служебные метки (латиница, без пробелов внутри скобок): "
    "[SEND_LINK] — только если пользователь явно просит ссылку, инвайт или «как вступить»; "
    "[STOP] — если просит прекратить переписку. "
    "На приветствия, «как дела», общие вопросы отвечай обычным текстом, без этих меток. "
    "Никогда не отправляй пользователю одну только метку без нормального ответа."
)

# Control Plane / distributed-agent telemetry
CP_AGENT_ENABLED = os.getenv("CP_AGENT_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
CP_INGEST_URL = os.getenv("CP_INGEST_URL", "http://127.0.0.1:8081/ingest/batch").strip()
CP_AGENT_TOKEN = os.getenv("CP_AGENT_TOKEN", "").strip()
CP_AGENT_NAME = os.getenv("CP_AGENT_NAME", "corebot-agent").strip()
CP_AGENT_VERSION = os.getenv("CP_AGENT_VERSION", "dev").strip()

# Прокси для Control Bot
CONTROL_BOT_PROXY_TYPE = os.getenv("CONTROL_BOT_PROXY_TYPE", "").strip()
CONTROL_BOT_PROXY_HOST = os.getenv("CONTROL_BOT_PROXY_HOST", "").strip()
CONTROL_BOT_PROXY_PORT = os.getenv("CONTROL_BOT_PROXY_PORT", "").strip()
CONTROL_BOT_PROXY_USERNAME = os.getenv("CONTROL_BOT_PROXY_USERNAME", "").strip()
CONTROL_BOT_PROXY_PASSWORD = os.getenv("CONTROL_BOT_PROXY_PASSWORD", "").strip()


def get_control_bot_proxy() -> Optional[dict]:
    """
    Получение конфигурации прокси для Control Bot.
    
    Returns:
        dict: Конфигурация прокси для aiohttp или None
    """
    if not CONTROL_BOT_PROXY_TYPE or not CONTROL_BOT_PROXY_HOST or not CONTROL_BOT_PROXY_PORT:
        return None
    
    # Формирование URL прокси
    if CONTROL_BOT_PROXY_USERNAME and CONTROL_BOT_PROXY_PASSWORD:
        proxy_url = (
            f"{CONTROL_BOT_PROXY_TYPE}://"
            f"{CONTROL_BOT_PROXY_USERNAME}:{CONTROL_BOT_PROXY_PASSWORD}@"
            f"{CONTROL_BOT_PROXY_HOST}:{CONTROL_BOT_PROXY_PORT}"
        )
    else:
        proxy_url = f"{CONTROL_BOT_PROXY_TYPE}://{CONTROL_BOT_PROXY_HOST}:{CONTROL_BOT_PROXY_PORT}"
    
    log.info(f"Прокси для Control Bot: {CONTROL_BOT_PROXY_HOST}:{CONTROL_BOT_PROXY_PORT} ({CONTROL_BOT_PROXY_TYPE})")
    
    return {
        "url": proxy_url,
        "type": CONTROL_BOT_PROXY_TYPE,
        "host": CONTROL_BOT_PROXY_HOST,
        "port": int(CONTROL_BOT_PROXY_PORT),
    }

# Директории
DATA_DIR = BASE_DIR / "data"
SESSIONS_DIR = DATA_DIR / "sessions"
TDATA_TEMP_DIR = DATA_DIR / "tdata_temp"
FILES_DIR = DATA_DIR / "files"
# Временные файлы для загрузки аватарок через бота (после Upload удаляются)
AVATARS_TEMP_DIR = FILES_DIR / "avatars"
NEURO_DIR = DATA_DIR / "neuro"
NEURO_MAILING_PROMPTS_DIR = NEURO_DIR / "mailings"

# Создаём директории при инициализации
for directory in [
    DATA_DIR,
    SESSIONS_DIR,
    TDATA_TEMP_DIR,
    FILES_DIR,
    AVATARS_TEMP_DIR,
    NEURO_DIR,
    NEURO_MAILING_PROMPTS_DIR,
]:
    directory.mkdir(parents=True, exist_ok=True)


def validate_config() -> bool:
    """
    Проверка корректности конфигурации.
    
    Returns:
        bool: True если все обязательные параметры заданы
    """
    required = {
        "API_ID": API_ID,
        "API_HASH": API_HASH,
        "BOT_TOKEN": BOT_TOKEN,
        "OWNER_ID": OWNER_ID,
    }
    
    missing = [key for key, value in required.items() if not value]
    
    if missing:
        from utils.logger import log
        log.error(f"Отсутствуют обязательные параметры: {', '.join(missing)}")
        return False
    
    return True
