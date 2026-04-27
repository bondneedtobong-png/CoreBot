"""
Точка входа parser-worker: Telethon + очередь parsing_tasks.

Запуск:
  python -m workers.parser_worker

Переменные окружения: как у бота (API_ID, API_HASH, DATABASE_URL).
Опционально: PARSER_POLL_SEC (по умолчанию 2), PARSER_MAX_PARALLEL (резерв, сейчас 1).
"""
from __future__ import annotations

import asyncio

from dotenv import load_dotenv

load_dotenv()

from database.repository import db
from utils.logger import log
from workers.parser.task_runner import run_forever


async def _main() -> None:
    await db.connect()
    log.info("parser-worker: БД подключена, старт цикла задач")
    await run_forever()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
