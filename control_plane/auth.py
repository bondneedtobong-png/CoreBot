from datetime import datetime, timedelta
import hashlib

import jwt
from passlib.context import CryptContext

from control_plane.config import (
    CP_JWT_SECRET,
    CP_JWT_ALG,
    CP_ACCESS_TTL_MIN,
    CP_REFRESH_TTL_MIN,
)


pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


def hash_password(raw: str) -> str:
    return pwd_context.hash(raw)


def verify_password(raw: str, password_hash: str) -> bool:
    return pwd_context.verify(raw, password_hash)


def create_access_token(user_id: int, tenant_id: int, role: str) -> str:
    exp = datetime.utcnow() + timedelta(minutes=CP_ACCESS_TTL_MIN)
    payload = {"sub": str(user_id), "tenant_id": tenant_id, "role": role, "type": "access", "exp": exp}
    return jwt.encode(payload, CP_JWT_SECRET, algorithm=CP_JWT_ALG)


def create_refresh_token(user_id: int, tenant_id: int, role: str) -> str:
    exp = datetime.utcnow() + timedelta(minutes=CP_REFRESH_TTL_MIN)
    payload = {"sub": str(user_id), "tenant_id": tenant_id, "role": role, "type": "refresh", "exp": exp}
    return jwt.encode(payload, CP_JWT_SECRET, algorithm=CP_JWT_ALG)


def decode_token(token: str) -> dict:
    return jwt.decode(token, CP_JWT_SECRET, algorithms=[CP_JWT_ALG])


def hash_agent_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
