"""
Настройка логирования с использованием loguru.
"""
import sys
from pathlib import Path
from loguru import logger

# Создаём директорию для логов
LOGS_DIR = Path(__file__).parent.parent / "logs"
LOGS_DIR.mkdir(exist_ok=True)

LOG_FILE = LOGS_DIR / "corebot.log"
ERROR_LOG_FILE = LOGS_DIR / "error.log"


def setup_logger(level: str = "INFO") -> None:
    """
    Настройка логгера.
    
    Args:
        level: Уровень логирования (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    """
    # Удаляем стандартный обработчик
    logger.remove()
    
    # Консольный вывод с цветом
    logger.add(
        sys.stderr,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
               "<level>{level: <8}</level> | "
               "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
               "<level>{message}</level>",
        level=level,
        colorize=True,
    )
    
    # Файл с полным логом (ротация по размеру и времени)
    logger.add(
        LOG_FILE,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} | {message}",
        level="DEBUG",
        rotation="10 MB",
        retention="7 days",
        compression="zip",
        enqueue=True,  # Асинхронная запись
    )
    
    # Отдельный файл только для ошибок
    logger.add(
        ERROR_LOG_FILE,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} | {message}",
        level="ERROR",
        rotation="5 MB",
        retention="30 days",
        compression="zip",
        enqueue=True,
    )
    
    logger.info("Логгер инициализирован")


# Создаём экземпляр логгера для импорта
log = logger
