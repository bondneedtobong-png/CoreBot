"""
Скрипт миграции БД - добавление новых полей в таблицу mailings.
Запустить: python migrate_mailings.py
"""
import asyncio
import sys
from pathlib import Path

# Добавляем корень проекта в path
sys.path.insert(0, str(Path(__file__).parent))

from sqlalchemy import text
from database.repository import db
from utils.logger import log, setup_logger


async def migrate():
    """Выполнение миграции."""
    setup_logger("INFO")
    
    log.info("🔄 Начало миграции таблицы mailings...")
    
    try:
        await db.connect()
        
        async with db.engine.begin() as conn:
            # Проверяем, существует ли таблица
            result = await conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='mailings'"
            ))
            if not result.fetchone():
                log.error("❌ Таблица mailings не найдена!")
                return False
            
            # Список новых колонок для добавления
            columns_to_add = [
                ("use_typing", "BOOLEAN DEFAULT 1"),
                ("typing_delay", "REAL DEFAULT 3.0"),
                ("smart_delay", "BOOLEAN DEFAULT 0"),
                ("messages_per_batch", "INTEGER DEFAULT 1"),
                ("batch_delay", "REAL DEFAULT 45.0"),
                ("daily_limit", "INTEGER DEFAULT 20"),
                ("updated_at", "DATETIME"),
            ]
            
            # Проверяем и добавляем колонки
            for col_name, col_def in columns_to_add:
                try:
                    # Проверяем, существует ли колонка
                    result = await conn.execute(text(
                        "PRAGMA table_info(mailings)"
                    ))
                    existing_cols = [row[1] for row in result.fetchall()]
                    
                    if col_name not in existing_cols:
                        log.info(f"➕ Добавление колонки: {col_name}")
                        await conn.execute(text(
                            f"ALTER TABLE mailings ADD COLUMN {col_name} {col_def}"
                        ))
                        log.info(f"✅ Добавлено: {col_name}")
                    else:
                        log.info(f"✓ Уже существует: {col_name}")
                        
                except Exception as e:
                    log.error(f"❌ Ошибка при добавлении {col_name}: {e}")
            
            log.info("✅ Миграция завершена успешно!")
            return True
            
    except Exception as e:
        log.error(f"❌ Ошибка миграции: {e}")
        import traceback
        log.error(traceback.format_exc())
        return False
        
    finally:
        await db.disconnect()


if __name__ == "__main__":
    success = asyncio.run(migrate())
    sys.exit(0 if success else 1)
