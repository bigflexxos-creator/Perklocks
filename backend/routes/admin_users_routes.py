"""Admin User Management routes.

Gated behind `role == "admin"`. Provides a paginated user list,
per-user detail, role/status mutation, and platform-wide overview
metrics so the operator has a real dashboard, not just an API.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from auth import UserPublic, require_admin_user, oauth2_scheme
from deps import db, current_user

router = APIRouter(prefix="/api/admin", tags=["admin-users"])


async def current_admin(
    token: Annotated[Optional[str], Depends(oauth2_scheme)],
) -> UserPublic:
    return await require_admin_user(db, token)


def get_db():
    return db


class RoleUpdate(BaseModel):
    role: str   # "admin" | "user"


class StatusUpdate(BaseModel):
    status: str  # "active" | "suspended"


def _serialize_user(doc: dict) -> dict:
    return {
        "id":         doc.get("id"),
        "email":      doc.get("email"),
        "name":       doc.get("name"),
        "created_at": doc.get("created_at"),
        "role":       doc.get("role") or "user",
        "status":     doc.get("status") or "active",
        "last_login_at": doc.get("last_login_at"),
    }


# ───────────────────────── Overview ─────────────────────────
@router.get("/overview")
async def admin_overview(
    user: Annotated[UserPublic, Depends(current_admin)]
):
    """Headline platform metrics for the admin dashboard."""
    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(days=1)
    week_ago = now - timedelta(days=7)
    # User counts
    total = await db.users.count_documents({})
    suspended = await db.users.count_documents({"status": "suspended"})
    admins = await db.users.count_documents({"role": "admin"})
    new_24h = await db.users.count_documents({"created_at": {"$gte": day_ago.isoformat()}})
    new_7d = await db.users.count_documents({"created_at": {"$gte": week_ago.isoformat()}})
    active_24h = await db.users.count_documents(
        {"last_login_at": {"$gte": day_ago.isoformat()}}
    )
    # Activity counts
    parlays_total = await db.parlay_history.count_documents({})
    parlays_24h = await db.parlay_history.count_documents(
        {"created_at": {"$gte": day_ago}}
    )
    picks_today = await db.picks.count_documents(
        {"pick_date": now.strftime("%Y-%m-%d")}
    )
    return {
        "users": {
            "total":     total,
            "admins":    admins,
            "suspended": suspended,
            "new_24h":   new_24h,
            "new_7d":    new_7d,
            "active_24h": active_24h,
        },
        "activity": {
            "parlays_total": parlays_total,
            "parlays_24h":   parlays_24h,
            "picks_today":   picks_today,
        },
        "generated_at": now.isoformat(),
    }


# ───────────────────────── Users list ─────────────────────────
@router.get("/users")
async def admin_list_users(
    user: Annotated[UserPublic, Depends(current_admin)],
    q: Optional[str] = Query(None, description="Search email/name (case-insensitive)"),
    role: Optional[str] = None,
    user_status: Optional[str] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
):
    """Paginated user list with optional search and filters."""
    query: dict = {}
    if q:
        import re
        rx = re.escape(q)
        query["$or"] = [
            {"email": {"$regex": rx, "$options": "i"}},
            {"name":  {"$regex": rx, "$options": "i"}},
        ]
    if role:
        query["role"] = role
    if user_status:
        query["status"] = user_status

    total = await db.users.count_documents(query)
    skip = (page - 1) * page_size
    cursor = db.users.find(query, {"hashed_password": 0, "_id": 0}) \
                     .sort("created_at", -1) \
                     .skip(skip).limit(page_size)
    rows = [_serialize_user(d) async for d in cursor]
    return {
        "users":     rows,
        "total":     total,
        "page":      page,
        "page_size": page_size,
        "pages":     (total + page_size - 1) // page_size if page_size else 1,
    }


# ───────────────────────── User detail ─────────────────────────
@router.get("/users/{user_id}")
async def admin_user_detail(
    user_id: str,
    user: Annotated[UserPublic, Depends(current_admin)]
):
    doc = await db.users.find_one({"id": user_id}, {"hashed_password": 0, "_id": 0})
    if not doc:
        raise HTTPException(404, "User not found")
    parlays_n = await db.parlay_history.count_documents({"user_id": user_id})
    parlays_won = await db.parlay_history.count_documents(
        {"user_id": user_id, "status": "won"}
    )
    parlays_lost = await db.parlay_history.count_documents(
        {"user_id": user_id, "status": "lost"}
    )
    recent_parlays = []
    async for p in db.parlay_history.find(
        {"user_id": user_id}, {"_id": 0},
    ).sort("created_at", -1).limit(10):
        recent_parlays.append({
            "id":         p.get("id"),
            "status":     p.get("status"),
            "legs_n":     len(p.get("legs") or []),
            "stake":      p.get("stake"),
            "payout":     p.get("payout"),
            "created_at": str(p.get("created_at") or ""),
        })
    return {
        "user":           _serialize_user(doc),
        "parlays_total":  parlays_n,
        "parlays_won":    parlays_won,
        "parlays_lost":   parlays_lost,
        "recent_parlays": recent_parlays,
    }


# ───────────────────────── Mutations ─────────────────────────
@router.post("/users/{user_id}/role")
async def admin_set_role(
    user_id: str,
    body: RoleUpdate,
    user: Annotated[UserPublic, Depends(current_admin)]
):
    new_role = (body.role or "").lower()
    if new_role not in ("admin", "user"):
        raise HTTPException(400, "role must be 'admin' or 'user'")
    # Block self-demotion if it would leave zero admins.
    if user_id == user.id and new_role != "admin":
        admins = await db.users.count_documents({"role": "admin"})
        if admins <= 1:
            raise HTTPException(409, "Cannot demote the last admin")
    r = await db.users.update_one({"id": user_id}, {"$set": {"role": new_role}})
    if r.matched_count == 0:
        raise HTTPException(404, "User not found")
    return {"ok": True, "role": new_role}


@router.post("/users/{user_id}/status")
async def admin_set_status(
    user_id: str,
    body: StatusUpdate,
    user: Annotated[UserPublic, Depends(current_admin)]
):
    new_status = (body.status or "").lower()
    if new_status not in ("active", "suspended"):
        raise HTTPException(400, "status must be 'active' or 'suspended'")
    if user_id == user.id and new_status == "suspended":
        raise HTTPException(409, "Cannot suspend your own account")
    r = await db.users.update_one({"id": user_id}, {"$set": {"status": new_status}})
    if r.matched_count == 0:
        raise HTTPException(404, "User not found")
    return {"ok": True, "status": new_status}


@router.get("/top-api-users")
async def admin_top_api_users(
    user: Annotated[UserPublic, Depends(current_admin)],
    limit: int = Query(25, ge=1, le=200),
):
    """Top users ranked by API call count (heaviest consumers)."""
    rows = []
    cursor = db.user_activity.find({}, {"_id": 0}).sort("api_calls", -1).limit(limit)
    async for r in cursor:
        u = await db.users.find_one(
            {"id": r.get("user_id")}, {"_id": 0, "email": 1, "name": 1, "role": 1, "status": 1},
        )
        rows.append({
            "user_id":    r.get("user_id"),
            "email":      (u or {}).get("email"),
            "name":       (u or {}).get("name"),
            "role":       (u or {}).get("role") or "user",
            "status":     (u or {}).get("status") or "active",
            "api_calls":  r.get("api_calls") or 0,
            "last_call_at": r.get("last_call_at"),
            "endpoints_top": r.get("endpoints_top") or [],
        })
    return {"top": rows}
async def admin_delete_user(
    user_id: str,
    user: Annotated[UserPublic, Depends(current_admin)]
):
    if user_id == user.id:
        raise HTTPException(409, "Cannot delete your own account")
    r = await db.users.delete_one({"id": user_id})
    if r.deleted_count == 0:
        raise HTTPException(404, "User not found")
    # Cascade — drop their parlay history but leave global pick records.
    await db.parlay_history.delete_many({"user_id": user_id})
    return {"ok": True}
