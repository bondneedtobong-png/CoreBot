"""
Точка входа приложения.
Запуск Control Bot и инициализация всех компонентов.
"""
import asyncio
import os
import sys

from loguru import logger

from bot.config import validate_config, LOG_LEVEL, OWNER_ID
from utils.background_tasks import background_tasks
from utils.logger import setup_logger
from utils.telemetry import telemetry_emitter


async def main():
    """Основная функция запуска."""
    # Настройка логгера
    setup_logger(LOG_LEVEL)
    
    log = logger
    
    log.info("=" * 50)
    log.info("CoreBot - Telegram Mass Mailer")
    log.info("=" * 50)
    
    # Валидация конфигурации
    if not validate_config():
        log.error("Конфигурация некорректна. Проверьте .env файл")
        log.error("Заполните API_ID, API_HASH, BOT_TOKEN и OWNER_ID")
        sys.exit(1)
    
    # Проверка OWNER_ID
    if OWNER_ID == 0:
        log.error("OWNER_ID не установлен. Укажите ваш Telegram ID в .env")
        log.error("Узнать ID можно через бота @userinfobot")
        sys.exit(1)
    
    log.info(f"Владелец бота: {OWNER_ID}")

    # Task 04: production fail-fast до подключения БД/парсера.
    # Local-режим: no-op, поведение старта не меняется.
    if (os.getenv("COREBOT_ENV", "local") or "local").strip().lower() == "production":
        from tools.validate_config import require_valid_production_config

        try:
            require_valid_production_config("production")
        except RuntimeError as exc:
            log.error(str(exc))
            log.error("Проверьте .env: python -m tools.validate_config --mode production")
            sys.exit(2)

    # Инициализация базы данных
    from database.repository import db
    try:
        await db.connect()
        log.info("База данных подключена")
    except Exception as e:
        log.error(f"Ошибка подключения к БД: {e}")
        sys.exit(1)
    
    # Запуск бота
    from bot.main import run_bot
    from workers.warmup import warmup_runner
    from workers.outbound_consumer import outbound_consumer
    from workers.bot_command_consumer import bot_command_consumer
    from control_plane.services.heartbeat import BotHeartbeat
    log.info("Запуск Control Bot...")

    bot_heartbeat = BotHeartbeat()

    try:
        await telemetry_emitter.start()
        warmup_runner.start()
        outbound_consumer.start()
        bot_command_consumer.start()
        bot_heartbeat.start()
        await run_bot()
    except KeyboardInterrupt:
        log.info("Получен сигнал остановки")
    except Exception as e:
        log.error(f"Критическая ошибка бота: {e}")
    finally:
        try:
            from workers.manager import worker_manager
            if worker_manager.is_running:
                log.info("Запрошена остановка активной рассылки перед завершением")
                worker_manager.stop_mailing()
                # Даём циклу рассылки корректно завершить текущую итерацию.
                await asyncio.sleep(1.0)
            await background_tasks.shutdown()
            await worker_manager.disconnect_all()
        except Exception as e:
            log.warning(f"Ошибка graceful shutdown WorkerManager: {e}")
        await telemetry_emitter.stop()
        await warmup_runner.stop()
        try:
            await bot_heartbeat.stop()
        except Exception as e:
            log.warning(f"Ошибка остановки BotHeartbeat: {e}")
        try:
            await outbound_consumer.stop()
        except Exception as e:
            log.warning(f"Ошибка остановки OutboundConsumer: {e}")
        try:
            await bot_command_consumer.stop()
        except Exception as e:
            log.warning(f"Ошибка остановки BotCommandConsumer: {e}")
        # Закрытие подключения к БД
        await db.disconnect()
        log.info("Приложение остановлено")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
