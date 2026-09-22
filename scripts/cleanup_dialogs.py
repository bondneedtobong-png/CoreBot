"""
Безопасная очистка переписок нейрочата (CLI).

Использование с активированным venv:

  python -m scripts.cleanup_dialogs --older-days 30 --dry-run
  python -m scripts.cleanup_dialogs --classes dead,bl,decline
  python -m scripts.cleanup_dialogs --account 5 --peer 12345678

Удаляются:
  * neuro_chat_messages — собственно переписки нейрочата
  * client_interactions  — связанные события (только по тем же фильтрам)

Не удаляются:
  * accounts, clients, mailings, mailing_logs, прокси, классы клиентов,
    warmup-логи, neuro_action_logs, instance_settings.

Работает батчами (--batch-size, по умолчанию 500), коммитит каждый батч.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from utils.time import utcnow_naive

from sqlalchemy import and_, create_engine, delete, func, select
from sqlalchemy.orm import Session, sessionmaker


def _normalize_url(url: str) -> str:
    if url.startswith("sqlite+aiosqlite"):
        return "sqlite" + url[len("sqlite+aiosqlite") :]
    return url


def _resolve_url() -> str:
    import os

    raw = (
        os.getenv("BOT_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or "sqlite:///data/corebot.db"
    )
    return _normalize_url(raw)


def _open_session() -> Session:
    engine = create_engine(
        _resolve_url(),
        future=True,
        connect_args={"timeout": 30, "check_same_thread": False},
    )
    return sessionmaker(bind=engine, autoflush=False, future=True)()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", type=int, default=None, help="account_id фильтр")
    parser.add_argument("--peer", type=int, default=None, help="peer_user_id фильтр")
    parser.add_argument("--older-days", type=int, default=None, help="удалить переписки старше N дней")
    parser.add_argument(
        "--classes",
        type=str,
        default=None,
        help="клиентские классы через запятую (dead,bl,decline,…)",
    )
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--dry-run", action="store_true", help="только подсчитать, без удаления")
    args = parser.parse_args(argv)

    if not any([args.account, args.peer, args.older_days, args.classes]):
        parser.error("Укажите хотя бы один фильтр (--account/--peer/--older-days/--classes)")

    from database.models import (  # noqa: WPS433
        Client,
        ClientClassCounter,
        ClientInteraction,
        NeuroChatMessage,
    )

    session = _open_session()
    try:
        msg_filters = []
        inter_filters = []

        if args.account is not None:
            msg_filters.append(NeuroChatMessage.account_id == int(args.account))
            inter_filters.append(ClientInteraction.account_id == int(args.account))
        if args.peer is not None:
            msg_filters.append(NeuroChatMessage.peer_user_id == int(args.peer))

        if args.older_days is not None:
            threshold = utcnow_naive() - timedelta(days=int(args.older_days))
            msg_filters.append(NeuroChatMessage.created_at < threshold)
            inter_filters.append(ClientInteraction.created_at < threshold)

        if args.classes:
            normalized = [
                c.strip().lstrip("{").rstrip("}").lower()
                for c in args.classes.split(",")
                if c.strip()
            ]
            if not normalized:
                parser.error("--classes пуст")
            client_ids_q = (
                select(ClientClassCounter.client_id)
                .where(
                    ClientClassCounter.class_key.in_(normalized),
                    ClientClassCounter.count > 0,
                )
                .distinct()
            )
            client_ids = [int(r[0]) for r in session.execute(client_ids_q).all()]
            if not client_ids:
                print("Нет клиентов с такими классами — нечего удалять.")
                return 0
            peer_ids = [
                int(r[0])
                for r in session.execute(
                    select(Client.telegram_user_id).where(
                        Client.id.in_(client_ids),
                        Client.telegram_user_id.is_not(None),
                    )
                ).all()
            ]
            if peer_ids:
                msg_filters.append(NeuroChatMessage.peer_user_id.in_(peer_ids))
            else:
                msg_filters.append(NeuroChatMessage.peer_user_id == -1)
            inter_filters.append(ClientInteraction.client_id.in_(client_ids))

        msg_count_q = select(func.count(NeuroChatMessage.id))
        if msg_filters:
            msg_count_q = msg_count_q.where(and_(*msg_filters))
        total_messages = int(session.execute(msg_count_q).scalar_one() or 0)

        inter_count_q = select(func.count(ClientInteraction.id))
        if inter_filters:
            inter_count_q = inter_count_q.where(and_(*inter_filters))
        total_interactions = int(session.execute(inter_count_q).scalar_one() or 0)

        print(
            f"К удалению: сообщений={total_messages}, событий клиента={total_interactions}"
        )
        if args.dry_run:
            print("Dry-run, ничего не удалено.")
            return 0

        deleted_messages = 0
        while True:
            ids_stmt = select(NeuroChatMessage.id)
            if msg_filters:
                ids_stmt = ids_stmt.where(and_(*msg_filters))
            ids_stmt = ids_stmt.limit(int(args.batch_size))
            ids_chunk = [int(r[0]) for r in session.execute(ids_stmt).all()]
            if not ids_chunk:
                break
            res = session.execute(
                delete(NeuroChatMessage).where(NeuroChatMessage.id.in_(ids_chunk))
            )
            session.commit()
            deleted_messages += int(res.rowcount or 0)
            print(f"  удалено сообщений: {deleted_messages}/{total_messages}")
            if len(ids_chunk) < int(args.batch_size):
                break

        deleted_interactions = 0
        if inter_filters:
            while True:
                ids_stmt = (
                    select(ClientInteraction.id).where(and_(*inter_filters)).limit(
                        int(args.batch_size)
                    )
                )
                ids_chunk = [int(r[0]) for r in session.execute(ids_stmt).all()]
                if not ids_chunk:
                    break
                res = session.execute(
                    delete(ClientInteraction).where(ClientInteraction.id.in_(ids_chunk))
                )
                session.commit()
                deleted_interactions += int(res.rowcount or 0)
                print(
                    f"  удалено client_interactions: {deleted_interactions}/{total_interactions}"
                )
                if len(ids_chunk) < int(args.batch_size):
                    break

        print(
            f"Готово. Удалено сообщений: {deleted_messages}, "
            f"client_interactions: {deleted_interactions}"
        )
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
