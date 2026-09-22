import asyncio
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from control_plane.auth import hash_password
from control_plane.config import CP_BOOTSTRAP_ADMIN_USERNAME, CP_BOOTSTRAP_ADMIN_PASSWORD
from control_plane.database import Base, engine, SessionLocal
from control_plane.models import Tenant, User
from control_plane.routes.auth import router as auth_router
from control_plane.routes.ingest import router as ingest_router
from control_plane.routes.dashboard import router as dashboard_router
from control_plane.routes.admin import router as admin_router
from control_plane.routes.business import router as business_router
from control_plane.routes.stream import router as stream_router
from control_plane.health import router as health_router
from control_plane.business.dashboard import router as biz_dashboard_router
from control_plane.business.archive import router as biz_archive_router
from control_plane.business.mailings import router as biz_mailings_router
from control_plane.business.clients import router as biz_clients_router
from control_plane.business.instance import router as biz_instance_router
from control_plane.business.groups import router as biz_groups_router
from control_plane.business.proxies import router as biz_proxies_router
from control_plane.business.parsing import router as biz_parsing_router
from control_plane.business.tdata_routes import router as biz_tdata_router
from database.repository import db as bot_db
from utils.logger import log
from workers.parser.task_runner import run_forever as run_parser_forever


load_dotenv()


def is_parser_embedded_enabled() -> bool:
    """PARSER_EMBEDDED=0/false/off/no отключает встроенный parser-loop."""
    return str(os.getenv("PARSER_EMBEDDED", "1")).strip().lower() not in {
        "0",
        "false",
        "off",
        "no",
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Единый lifespan: bootstrap, embedded parser-loop и аккуратный shutdown.

    Встроенный parser-loop запускается вместе с веб-панелью/Control Plane.
    Отключение при необходимости: PARSER_EMBEDDED=0.
    """
    app.state.parser_task = None
    parser_task = None
    # Task 04: production fail-fast до bootstrap/БД/парсера.
    # Local-режим: no-op, поведение старта не меняется.
    if (os.getenv("COREBOT_ENV", "local") or "local").strip().lower() == "production":
        from tools.validate_config import require_valid_production_config

        require_valid_production_config("production")
    try:
        bootstrap_defaults()
        if not is_parser_embedded_enabled():
            log.info("Embedded parser is disabled (PARSER_EMBEDDED=0)")
        else:
            # Нужен async-движок corebot.db для workers/parser/*
            await bot_db.connect()
            parser_task = asyncio.create_task(run_parser_forever(), name="embedded-parser-loop")
            log.info("Embedded parser started with Control Plane")
        app.state.parser_task = parser_task
        yield
    finally:
        if parser_task is not None:
            parser_task.cancel()
            with suppress(asyncio.CancelledError):
                await parser_task
        with suppress(Exception):
            await bot_db.disconnect()


app = FastAPI(title="CoreBot Control Plane", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(ingest_router)
app.include_router(dashboard_router)
app.include_router(admin_router)
app.include_router(business_router)
app.include_router(stream_router)
app.include_router(health_router)
app.include_router(biz_dashboard_router)
app.include_router(biz_archive_router)
app.include_router(biz_mailings_router)
app.include_router(biz_clients_router)
app.include_router(biz_instance_router)
app.include_router(biz_groups_router)
app.include_router(biz_proxies_router)
app.include_router(biz_parsing_router)
app.include_router(biz_tdata_router)

web_dir = Path(__file__).parent.parent / "web-panel"
if web_dir.exists():
    app.mount("/panel", StaticFiles(directory=str(web_dir), html=True), name="panel")


def bootstrap_defaults() -> None:
    Base.metadata.create_all(bind=engine)
    db: Session = SessionLocal()
    try:
        tenant = db.query(Tenant).filter(Tenant.name == "default").first()
        if not tenant:
            tenant = Tenant(name="default")
            db.add(tenant)
            db.commit()
            db.refresh(tenant)
        admin = db.query(User).filter(User.username == CP_BOOTSTRAP_ADMIN_USERNAME).first()
        if not admin:
            db.add(
                User(
                    tenant_id=tenant.id,
                    username=CP_BOOTSTRAP_ADMIN_USERNAME,
                    password_hash=hash_password(CP_BOOTSTRAP_ADMIN_PASSWORD),
                    role="super_admin",
                    is_active=True,
                )
            )
            db.commit()
    finally:
        db.close()
