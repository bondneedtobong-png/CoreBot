import asyncio
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from starlette.types import Scope

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
from control_plane.version import router as version_router
from control_plane.business.dashboard import router as biz_dashboard_router
from control_plane.business.archive import router as biz_archive_router
from control_plane.business.mailings import router as biz_mailings_router
from control_plane.business.clients import router as biz_clients_router
from control_plane.business.instance import router as biz_instance_router
from control_plane.business.groups import router as biz_groups_router
from control_plane.business.proxies import router as biz_proxies_router
from control_plane.business.parsing import router as biz_parsing_router
from control_plane.business.tdata_routes import router as biz_tdata_router
from control_plane.business.links import router as biz_links_router
from control_plane.business.tdata_check_routes import router as biz_tdata_check_router
from control_plane.business.account_safety import router as biz_account_safety_router
from control_plane.business.engagement import router as biz_engagement_router
from control_plane.business.warmup_report import router as biz_warmup_report_router
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
        # The panel may be started before the bot during a pinned release.
        # Migrate the shared business schema before serving its routes in both
        # parser modes; this reuses the existing bot async engine.
        await bot_db.connect()
        if not is_parser_embedded_enabled():
            log.info("Embedded parser is disabled (PARSER_EMBEDDED=0)")
        else:
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


def _cors_origins() -> list[str]:
    """CORS-origins из env CP_CORS_ORIGINS (csv). По умолчанию только loopback.

    allow_credentials=True несовместим с allow_origins=["*"] (браузеры
    режут такой ответ + credentialed wildcard — дыра). Если задан "*",
    credentials принудительно выключаем.
    """
    raw = (os.getenv("CP_CORS_ORIGINS", "") or "").strip()
    if not raw:
        return ["http://127.0.0.1:8081", "http://localhost:8081"]
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return parts or ["http://127.0.0.1:8081"]


_CORS_ORIGINS = _cors_origins()
_CORS_ALLOW_CREDENTIALS = "*" not in _CORS_ORIGINS

app = FastAPI(title="CoreBot Control Plane", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=_CORS_ALLOW_CREDENTIALS,
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
app.include_router(version_router)
app.include_router(biz_dashboard_router)
app.include_router(biz_archive_router)
app.include_router(biz_mailings_router)
app.include_router(biz_clients_router)
app.include_router(biz_instance_router)
app.include_router(biz_groups_router)
app.include_router(biz_proxies_router)
app.include_router(biz_links_router)
app.include_router(biz_parsing_router)
app.include_router(biz_tdata_router)
app.include_router(biz_tdata_check_router)
app.include_router(biz_account_safety_router)
app.include_router(biz_engagement_router)
app.include_router(biz_warmup_report_router)

web_dir = Path(__file__).parent.parent / "web-panel"


class NoStoreStaticFiles(StaticFiles):
    """Serve the local web panel without HTTP caching.

    The panel is a local admin UI edited in place; heuristic browser caching
    of index.html/main.js caused stale (pre-fix) bundles to render after
    updates. ``no-store`` forces revalidation on every load.
    """

    async def get_response(self, path: str, scope: Scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store"
        return response


if web_dir.exists():
    app.mount("/panel", NoStoreStaticFiles(directory=str(web_dir), html=True), name="panel")


@app.get("/", include_in_schema=False)
def public_home():
    return FileResponse(web_dir / "landing.html", headers={"Cache-Control": "no-store"})


def bootstrap_defaults() -> None:
    Base.metadata.create_all(bind=engine)
    db: Session = SessionLocal()
    try:
        tenant = db.query(Tenant).filter(Tenant.name == "default").first()
        if not tenant:
            tenant = Tenant(name="default")
            db.add(tenant)
            try:
                db.commit()
            except Exception:
                db.rollback()
                raise
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
            try:
                db.commit()
            except Exception:
                db.rollback()
                raise
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise
    finally:
        db.close()
