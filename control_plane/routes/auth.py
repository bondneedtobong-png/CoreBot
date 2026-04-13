from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from control_plane.auth import verify_password, create_access_token, create_refresh_token
from control_plane.database import get_db
from control_plane.models import User
from control_plane.schemas import LoginIn, TokenOut


router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenOut)
def login(payload: LoginIn, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == payload.username, User.is_active == True).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    return TokenOut(
        access_token=create_access_token(user.id, user.tenant_id, user.role),
        refresh_token=create_refresh_token(user.id, user.tenant_id, user.role),
    )
