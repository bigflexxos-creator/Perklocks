"""Client telemetry — capture runtime errors from the mobile app so
we can see what's crashing in production without waiting for user reports.

Stored in `client_errors` collection. Payload fields:
  • message         — Error.message
  • stack           — first ~4kb of the stack trace
  • url_path        — router path when the error fired
  • component       — component name (from ErrorBoundary)
  • user_email_hash — sha256 first-8 for correlation without PII
  • device          — {os, os_version, model, app_version}
  • data_version    — server DATA_VERSION at time of error (drift signal)
  • extra           — arbitrary JSON blob
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, Request, Header
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field
import os

from auth import get_current_user_from_db, UserPublic

# Local Mongo handle — the shared `db` instance lives in server.py as
# a module-level global, but importing it here would create a circular
# import. Instead we re-create the client here using the same env
# variables. The overhead is negligible (motor pools connections).
_MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")
_DB_NAME   = os.getenv("DB_NAME", "lockscore_db")
_client    = AsyncIOMotorClient(_MONGO_URL)
db         = _client[_DB_NAME]

router = APIRouter(prefix="/api", tags=["telemetry"])


async def _optional_user(
    authorization: Optional[str] = Header(None),
) -> Optional[UserPublic]:
    """Auth is optional for telemetry — we still want error data
    from users who somehow lost their session."""
    if not authorization:
        return None
    try:
        token = authorization.replace("Bearer ", "").strip()
        return await get_current_user_from_db(db, token)
    except Exception:
        return None


class ClientErrorPayload(BaseModel):
    message:      str = Field(..., max_length=1000)
    stack:        Optional[str] = Field(None, max_length=8000)
    url_path:     Optional[str] = None
    component:    Optional[str] = None
    device:       Optional[dict[str, Any]] = None
    data_version: Optional[str] = None
    extra:        Optional[dict[str, Any]] = None


@router.post("/telemetry/error")
async def log_client_error(
    payload: ClientErrorPayload,
    request: Request,
    user: Annotated[Optional[UserPublic], Depends(_optional_user)] = None,
):
    """Idempotent error sink. Never raises — dropped errors are worse
    than a slightly slower endpoint."""
    try:
        doc = {
            "message":         payload.message[:1000],
            "stack":           (payload.stack or "")[:8000],
            "url_path":        payload.url_path,
            "component":       payload.component,
            "device":          payload.device or {},
            "data_version":    payload.data_version,
            "extra":           payload.extra or {},
            "user_email_hash": hashlib.sha256((user.email or "").encode()).hexdigest()[:8]
                                if user else None,
            "ip_last_octet":   (request.client.host.rsplit(".", 1)[-1]
                                if request.client else None),
            "received_at":     datetime.now(timezone.utc).isoformat(),
        }
        await db.client_errors.insert_one(doc)
        return {"ok": True}
    except Exception:
        return {"ok": False}


@router.get("/admin/client-errors/recent")
async def recent_client_errors(
    user: Annotated[Optional[UserPublic], Depends(_optional_user)] = None,
    limit: int = 100,
):
    """Read-only view of the last N client errors. Admin-gated in the
    UI; wide open at API level for now (matches other /admin/* reads)."""
    docs = await db.client_errors.find(
        {},
        {"_id": 0},
    ).sort("received_at", -1).limit(min(limit, 500)).to_list(length=None)
    return {"count": len(docs), "errors": docs}
