from utils.time import utcnow_naive

from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    DateTime,
    ForeignKey,
    Text,
    BigInteger,
    Float,
    Index,
)
from sqlalchemy.orm import relationship

from control_plane.database import Base


class Tenant(Base):
    __tablename__ = "cp_tenants"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(120), unique=True, nullable=False)
    created_at = Column(DateTime, default=utcnow_naive)


class User(Base):
    __tablename__ = "cp_users"
    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey("cp_tenants.id"), nullable=False)
    username = Column(String(120), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(40), default="tenant_viewer")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow_naive)

    tenant = relationship("Tenant")


class Agent(Base):
    __tablename__ = "cp_agents"
    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey("cp_tenants.id"), nullable=False)
    name = Column(String(160), nullable=False)
    machine_fingerprint = Column(String(255), nullable=True)
    version = Column(String(64), nullable=True)
    last_seen_at = Column(DateTime, nullable=True)
    is_online = Column(Boolean, default=False)
    created_at = Column(DateTime, default=utcnow_naive)

    tenant = relationship("Tenant")

    __table_args__ = (Index("ix_cp_agents_tenant_name", "tenant_id", "name"),)


class AgentToken(Base):
    __tablename__ = "cp_agent_tokens"
    id = Column(Integer, primary_key=True, autoincrement=True)
    agent_id = Column(Integer, ForeignKey("cp_agents.id"), nullable=False)
    token_hash = Column(String(255), nullable=False, unique=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow_naive)
    rotated_at = Column(DateTime, nullable=True)

    agent = relationship("Agent")


class IngestEvent(Base):
    __tablename__ = "cp_ingest_events"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey("cp_tenants.id"), nullable=False)
    agent_id = Column(Integer, ForeignKey("cp_agents.id"), nullable=False)
    level = Column(String(20), default="info")
    category = Column(String(80), nullable=False)
    code = Column(String(120), nullable=True)
    message = Column(Text, nullable=False)
    payload_json = Column(Text, nullable=True)
    schema_version = Column(String(32), default="1")
    created_at = Column(DateTime, default=utcnow_naive)

    __table_args__ = (
        Index("ix_cp_ingest_tenant_created", "tenant_id", "created_at"),
        Index("ix_cp_ingest_agent_created", "agent_id", "created_at"),
    )


class MetricPoint(Base):
    __tablename__ = "cp_metric_points"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey("cp_tenants.id"), nullable=False)
    agent_id = Column(Integer, ForeignKey("cp_agents.id"), nullable=False)
    name = Column(String(80), nullable=False)
    value = Column(Float, nullable=False)
    tags_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow_naive)

    __table_args__ = (Index("ix_cp_metric_tenant_name_created", "tenant_id", "name", "created_at"),)


class Alert(Base):
    __tablename__ = "cp_alerts"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey("cp_tenants.id"), nullable=False)
    agent_id = Column(Integer, ForeignKey("cp_agents.id"), nullable=True)
    kind = Column(String(80), nullable=False)
    fingerprint = Column(String(255), nullable=False)
    severity = Column(String(20), default="warning")
    title = Column(String(255), nullable=False)
    details = Column(Text, nullable=True)
    status = Column(String(20), default="open")
    count = Column(Integer, default=1)
    last_triggered_at = Column(DateTime, default=utcnow_naive)
    created_at = Column(DateTime, default=utcnow_naive)

    __table_args__ = (Index("ix_cp_alert_tenant_fingerprint", "tenant_id", "fingerprint"),)


class AuditLog(Base):
    __tablename__ = "cp_audit_logs"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    actor_user_id = Column(Integer, ForeignKey("cp_users.id"), nullable=True)
    tenant_id = Column(Integer, ForeignKey("cp_tenants.id"), nullable=True)
    action = Column(String(100), nullable=False)
    target_type = Column(String(80), nullable=True)
    target_id = Column(String(80), nullable=True)
    details = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow_naive)
