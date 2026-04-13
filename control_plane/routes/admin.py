import secrets

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from control_plane.auth import hash_password, hash_agent_token
from control_plane.database import get_db
from control_plane.deps import require_admin, require_super_admin
from control_plane.models import Tenant, User, Agent, AgentToken, AuditLog
from control_plane.tasks import cleanup_retention, make_backup


router = APIRouter(prefix="/admin", tags=["admin"])


def _safe_audit(db: Session, **kwargs) -> None:
    try:
        db.add(AuditLog(**kwargs))
        db.commit()
    except Exception:
        db.rollback()


@router.post("/tenants")
def create_tenant(name: str, db: Session = Depends(get_db), user=Depends(require_super_admin)):
    existing = db.query(Tenant).filter(Tenant.name == name).first()
    if existing:
        raise HTTPException(status_code=400, detail="Tenant already exists")
    row = Tenant(name=name)
    db.add(row)
    db.commit()
    db.refresh(row)
    _safe_audit(
        db,
        actor_user_id=user.id,
        tenant_id=row.id,
        action="create_tenant",
        target_type="tenant",
        target_id=str(row.id),
    )
    return {"id": row.id, "name": row.name}


@router.post("/users")
def create_user(
    tenant_id: int,
    username: str,
    password: str,
    role: str = "tenant_admin",
    db: Session = Depends(get_db),
    actor=Depends(require_admin),
):
    if actor.role != "super_admin" and actor.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="Cross-tenant denied")
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(status_code=400, detail="Username exists")
    row = User(
        tenant_id=tenant_id,
        username=username,
        password_hash=hash_password(password),
        role=role,
        is_active=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    _safe_audit(
        db,
        actor_user_id=actor.id,
        tenant_id=tenant_id,
        action="create_user",
        target_type="user",
        target_id=str(row.id),
    )
    return {"id": row.id, "username": row.username, "role": row.role}


@router.post("/agents")
def create_agent(
    tenant_id: int,
    name: str,
    db: Session = Depends(get_db),
    actor=Depends(require_admin),
):
    if actor.role != "super_admin" and actor.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="Cross-tenant denied")
    row = Agent(tenant_id=tenant_id, name=name, is_online=False)
    db.add(row)
    db.commit()
    db.refresh(row)
    _safe_audit(
        db,
        actor_user_id=actor.id,
        tenant_id=tenant_id,
        action="create_agent",
        target_type="agent",
        target_id=str(row.id),
    )
    return {"id": row.id, "name": row.name}


@router.post("/agents/{agent_id}/rotate-token")
def rotate_agent_token(agent_id: int, db: Session = Depends(get_db), actor=Depends(require_admin)):
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if actor.role != "super_admin" and actor.tenant_id != agent.tenant_id:
        raise HTTPException(status_code=403, detail="Cross-tenant denied")
    for t in db.query(AgentToken).filter(AgentToken.agent_id == agent.id, AgentToken.is_active == True).all():
        t.is_active = False
    raw = secrets.token_urlsafe(32)
    db.add(AgentToken(agent_id=agent.id, token_hash=hash_agent_token(raw), is_active=True))
    db.commit()
    _safe_audit(
        db,
        actor_user_id=actor.id,
        tenant_id=agent.tenant_id,
        action="rotate_agent_token",
        target_type="agent",
        target_id=str(agent.id),
    )
    return {"agent_id": agent.id, "agent_token": raw}


@router.post("/maintenance/cleanup")
def run_cleanup(db: Session = Depends(get_db), actor=Depends(require_super_admin)):
    stats = cleanup_retention(db)
    _safe_audit(db, actor_user_id=actor.id, tenant_id=None, action="cleanup_retention", details=str(stats))
    return {"ok": True, **stats}


@router.post("/maintenance/backup")
def run_backup(db: Session = Depends(get_db), actor=Depends(require_super_admin)):
    path = make_backup()
    _safe_audit(db, actor_user_id=actor.id, tenant_id=None, action="backup_db", details=path)
    return {"ok": bool(path), "path": path}
