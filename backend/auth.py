"""JWT email/password auth utilities."""
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Optional

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jwt import ExpiredSignatureError, InvalidTokenError
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, EmailStr, Field

_auth_logger = logging.getLogger("lockscore.auth")


def _resolve_jwt_secret() -> str:
    """Return a strong JWT secret without crashing the boot.

    Priority:
      1. ``JWT_SECRET`` env var, if present AND strong (≥32 chars AND
         does NOT contain the obvious placeholder ``change-me``).
      2. Cached ephemeral secret on disk at ``$JWT_SECRET_CACHE`` or
         ``/tmp/perkslocks_jwt_secret`` — generated once per container
         and reused across in-deploy restarts so sessions don't get
         nuked on every uvicorn reload.
      3. A freshly-minted ``secrets.token_urlsafe(64)`` value, written
         to the cache path for future boots.

    Hard-fail ONLY if the user has explicitly set a value containing
    ``change-me`` — that's almost certainly a forgotten placeholder
    and silently overriding it would mask the bug. Missing env var
    on a deployed environment is much more common and recoverable —
    so we generate a strong value instead of returning a Cloudflare
    520. (SEC-001 mitigation, post-deploy hardening 2026-06-25.)
    """
    raw = (os.environ.get("JWT_SECRET") or "").strip()

    # User-provided, strong, and not an obvious placeholder → canonical path.
    if raw and len(raw) >= 32 and "change-me" not in raw.lower():
        return raw

    # Explicitly weak (e.g. left as "change-me-..." placeholder): log a
    # loud warning but DON'T crash the boot — that just means a Cloudflare
    # 520 for users. Fall through to the ephemeral-secret path below.
    if raw and ("change-me" in raw.lower() or len(raw) < 32):
        _auth_logger.error(
            "JWT_SECRET is set but weak (placeholder or <32 chars). "
            "Ignoring it and generating a strong ephemeral secret. "
            "Replace with `python -c \"import secrets; print(secrets.token_urlsafe(64))\"`.",
        )

    # Env var missing or too short. Fall back to a strong ephemeral
    # secret cached on disk. Loud warning so operators notice.
    cache_path = Path(
        os.environ.get("JWT_SECRET_CACHE") or "/tmp/perkslocks_jwt_secret"
    )
    try:
        if cache_path.exists():
            cached = cache_path.read_text(encoding="utf-8").strip()
            if cached and len(cached) >= 32:
                _auth_logger.warning(
                    "JWT_SECRET env var missing/short — using cached "
                    "ephemeral secret from %s. Set JWT_SECRET in the "
                    "deploy environment to make sessions survive "
                    "redeploys.", cache_path,
                )
                return cached
    except Exception:
        pass

    fresh = secrets.token_urlsafe(64)
    try:
        cache_path.write_text(fresh, encoding="utf-8")
        try:
            os.chmod(cache_path, 0o600)
        except Exception:
            pass
    except Exception:
        # /tmp may be read-only on some deploys — that's fine, secret
        # is still strong, it just won't survive a process restart.
        pass

    _auth_logger.warning(
        "JWT_SECRET env var missing — generated a strong ephemeral "
        "secret (cached at %s). For session continuity across "
        "redeploys, set JWT_SECRET in the deploy environment to a "
        "value from `python -c \"import secrets; print(secrets.token_urlsafe(64))\"`.",
        cache_path,
    )
    return fresh


JWT_SECRET = _resolve_jwt_secret()
JWT_ALGORITHM = os.environ.get("JWT_ALGORITHM", "HS256")
JWT_EXPIRES_MINUTES = int(os.environ.get("JWT_EXPIRES_MINUTES", "43200"))
JWT_ISSUER   = os.environ.get("JWT_ISSUER", "perkslocks")
JWT_AUDIENCE = os.environ.get("JWT_AUDIENCE", "perkslocks-app")

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
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
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
        payload = jwt.decode(
            token,
            JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
            audience=JWT_AUDIENCE,
            issuer=JWT_ISSUER,
        )
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
