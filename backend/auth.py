"""JWT email/password auth utilities."""
import os
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jwt import ExpiredSignatureError, InvalidTokenError
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, EmailStr, Field

# JWT secret with production fallback so deployment doesn't crash if the env
# var isn't set. The env var is still preferred — this is a safety net.
JWT_SECRET = os.environ.get("JWT_SECRET") or "perkslocks_prod_jwt_secret_a8h3kdj29sl1nf03kp5_change_via_env_for_better_security"
JWT_ALGORITHM = os.environ.get("JWT_ALGORITHM", "HS256")
JWT_EXPIRES_MINUTES = int(os.environ.get("JWT_EXPIRES_MINUTES", "43200"))

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6)
    name: Optional[str] = None


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class UserPublic(BaseModel):
    id: str
    email: EmailStr
    name: Optional[str] = None
    created_at: Optional[str] = None
    role: Optional[str] = "user"           # "user" | "admin"
    status: Optional[str] = "active"       # "active" | "suspended"


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserPublic


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def create_access_token(subject: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=JWT_EXPIRES_MINUTES)).timestamp()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


async def get_current_user_from_db(
    db: AsyncIOMotorDatabase, token: Optional[str]
) -> UserPublic:
    credentials_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not token:
        raise credentials_exc
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except InvalidTokenError:
        raise credentials_exc

    user_id = payload.get("sub")
    if not user_id:
        raise credentials_exc

    doc = await db["users"].find_one({"id": user_id}, {"_id": 0, "hashed_password": 0})
    if not doc:
        raise credentials_exc
    # Reject suspended accounts at auth time so every endpoint is protected.
    if (doc.get("status") or "active") == "suspended":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account suspended. Contact support.",
        )
    # Default role/status so legacy users (created before this field
    # was added) still validate.
    doc.setdefault("role", "user")
    doc.setdefault("status", "active")
    return UserPublic(**doc)


async def require_admin_user(
    db: AsyncIOMotorDatabase, token: Optional[str]
) -> UserPublic:
    """Dependency: same as `get_current_user_from_db` but 403s on non-admin."""
    user = await get_current_user_from_db(db, token)
    if (user.role or "user") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required",
        )
    return user
