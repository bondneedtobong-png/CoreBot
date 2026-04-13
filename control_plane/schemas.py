from datetime import datetime
from typing import Optional, List, Any

from pydantic import BaseModel, Field


class LoginIn(BaseModel):
    username: str
    password: str


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class IngestEventIn(BaseModel):
    level: str = "info"
    category: str
    code: Optional[str] = None
    message: str
    payload: Optional[dict[str, Any]] = None
    schema_version: str = "1"


class MetricIn(BaseModel):
    name: str
    value: float
    tags: Optional[dict[str, Any]] = None


class IngestBatchIn(BaseModel):
    agent_name: str = Field(min_length=1, max_length=160)
    machine_fingerprint: Optional[str] = None
    version: Optional[str] = None
    events: List[IngestEventIn] = []
    metrics: List[MetricIn] = []


class DashboardSummaryOut(BaseModel):
    tenant_id: int
    active_agents: int
    events_24h: int
    errors_24h: int
    alerts_open: int


class AgentStatusOut(BaseModel):
    id: int
    name: str
    version: Optional[str] = None
    is_online: bool
    last_seen_at: Optional[datetime] = None


class LogItemOut(BaseModel):
    id: int
    created_at: datetime
    level: str
    category: str
    code: Optional[str] = None
    message: str
