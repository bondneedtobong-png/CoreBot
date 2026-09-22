from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from control_plane.config import CP_DATABASE_URL
from database.sqlite_pragmas import register_sqlite_pragmas


# URL не меняем: только PRAGMA/retry (требование tools/validate_config.py).
engine = create_engine(
    CP_DATABASE_URL,
    future=True,
    connect_args={"timeout": 30, "check_same_thread": False}
    if CP_DATABASE_URL.startswith("sqlite")
    else {},
    pool_pre_ping=True,
)
if CP_DATABASE_URL.startswith("sqlite"):
    # Единые SQLite PRAGMA для всех engine (см. database/sqlite_pragmas.py).
    register_sqlite_pragmas(engine)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, future=True)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
