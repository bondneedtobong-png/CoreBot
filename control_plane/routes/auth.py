from datetime import timedelta

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from control_plane.auth import (
    decode_token,
    verify_password,
    create_access_token,
    create_refresh_token,
)
from control_plane.database import get_db
from control_plane.config import CP_JWT_ALG, CP_JWT_SECRET
from control_plane.deps import get_current_user
from control_plane.models import User
from control_plane.schemas import LoginIn, TokenOut, UserMeOut
from utils.time import utcnow_naive


router = APIRouter(prefix="/auth", tags=["auth"])
STREAM_COOKIE_NAME = "corebot_stream"
STREAM_COOKIE_MAX_AGE = 15 * 60


class RefreshIn(BaseModel):
    refresh_token: str = Field(min_length=10, max_length=4096)


@router.post("/login", response_model=TokenOut)
def login(payload: LoginIn, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == payload.username, User.is_active == True).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    return TokenOut(
        access_token=create_access_token(user.id, user.tenant_id, user.role),
        refresh_token=create_refresh_token(user.id, user.tenant_id, user.role),
    )


@router.post("/refresh", response_model=TokenOut)
def refresh(payload: RefreshIn, db: Session = Depends(get_db)):
    """Обмен refresh→access. Раньше refresh создавался, но нигде не проверялся."""
    try:
        data = decode_token(payload.refresh_token)
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    if data.get("type") != "refresh":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Wrong token type")
    try:
        uid = int(data.get("sub"))
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    user = db.query(User).filter(User.id == uid, User.is_active == True).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return TokenOut(
        access_token=create_access_token(user.id, user.tenant_id, user.role),
        refresh_token=create_refresh_token(user.id, user.tenant_id, user.role),
    )


@router.get("/me", response_model=UserMeOut)
def me(user: User = Depends(get_current_user)):
    return UserMeOut(
        id=int(user.id),
        username=user.username,
        role=user.role,
        tenant_id=int(user.tenant_id),
    )


@router.post("/stream-session", status_code=status.HTTP_204_NO_CONTENT)
def create_stream_session(
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
):
    token = jwt.encode(
        {
            "sub": str(user.id),
            "tenant_id": user.tenant_id,
            "role": user.role,
            "type": "access",
            "exp": utcnow_naive() + timedelta(seconds=STREAM_COOKIE_MAX_AGE),
        },
        CP_JWT_SECRET,
        algorithm=CP_JWT_ALG,
    )
    response.set_cookie(
        STREAM_COOKIE_NAME,
        token,
        max_age=STREAM_COOKIE_MAX_AGE,
        path="/business",
        secure=request.url.scheme == "https",
        httponly=True,
        samesite="strict",
    )
    response.headers["Cache-Control"] = "no-store"


@router.delete("/stream-session", status_code=status.HTTP_204_NO_CONTENT)
def delete_stream_session(request: Request, response: Response):
    response.delete_cookie(
        STREAM_COOKIE_NAME,
        path="/business",
        secure=request.url.scheme == "https",
        httponly=True,
        samesite="strict",
    )
    response.headers["Cache-Control"] = "no-store"
