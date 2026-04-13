from pathlib import Path

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


app = FastAPI(title="CoreBot Control Plane", version="0.1.0")
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

web_dir = Path(__file__).parent.parent / "web-panel"
if web_dir.exists():
    app.mount("/panel", StaticFiles(directory=str(web_dir), html=True), name="panel")


@app.get("/health")
def health():
    return {"ok": True}


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


bootstrap_defaults()
