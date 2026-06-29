"""Shared backend dependencies.

Centralizes the Mongo connection, logger, and auth dependency so route
modules under `backend/routes/` can import them without circular
references back into `server.py`.

Anything that BOTH `server.py` and a route module needs — and that is
stable / side-effect-light — belongs here. Keep this module thin.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Annotated, Optional

from dotenv import load_dotenv
from fastapi import Depends
from motor.motor_asyncio import AsyncIOMotorClient

# ── env loading ───────────────────────────────────────────────────────
# Load .env BEFORE we read MONGO_URL. Mirrors the call in server.py;
# load_dotenv is idempotent so calling twice is safe.
ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

# ── logger ────────────────────────────────────────────────────────────
# Match server.py's basicConfig so importing this module before server.py
# still produces formatted logs. server.py also calls basicConfig — the
# second call is a no-op (logging only honors the first).
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("lockscore")

# ── Mongo ─────────────────────────────────────────────────────────────
# Production-safe env loading with sane fallbacks so deployment doesn't
# crash if env vars aren't set on the production environment.
_mongo_url = os.environ.get("MONGO_URL") or "mongodb://localhost:27017"

# ── CONNECTION POOL HARDENING (2026-06-28) ────────────────────────────
# Emergent support flagged that the deployed MONGO_URL had
# `?maxPoolSize=5` appended, while this backend runs 20+ concurrent
# sports engines (MLB Intel, Soccer pipeline, Player Intelligence,
# Settlement loop, Validator, Brain, etc.). With a 5-connection ceiling
# they starve each other → operations queue → /api/picks/today times
# out on Cloudflare → user sees "picks fail to load/save".
#
# We pass explicit pool + timeout kwargs to the Motor client. PyMongo
# rule: KWARGS take precedence over URI query params, so even if the
# secret still has `maxPoolSize=5`, the values below win.
#
# Tuning rationale:
#   • maxPoolSize=20  — one connection per concurrent engine, +headroom
#   • minPoolSize=2   — keep at least 2 warm so the first request after
#                       idle doesn't pay full TCP+TLS handshake latency
#   • serverSelectionTimeoutMS=10000 — bail in 10s instead of the 30s
#                       default; matches our outer 85s middleware budget
#   • connectTimeoutMS=10000         — same as above for new sockets
#   • socketTimeoutMS=60000          — heavy aggregations (632-pick
#                       validator) take 20-40s; 60s gives margin
#   • waitQueueTimeoutMS=15000       — if all 20 sockets are busy,
#                       FAIL FAST instead of holding the request open
#                       indefinitely (the silent-timeout symptom)
#   • retryWrites=True               — auto-retry one transient write
client = AsyncIOMotorClient(
    _mongo_url,
    maxPoolSize=int(os.environ.get("MONGO_MAX_POOL_SIZE", "20")),
    minPoolSize=int(os.environ.get("MONGO_MIN_POOL_SIZE", "2")),
    serverSelectionTimeoutMS=int(os.environ.get("MONGO_SERVER_SEL_TIMEOUT_MS", "10000")),
    connectTimeoutMS=int(os.environ.get("MONGO_CONNECT_TIMEOUT_MS", "10000")),
    socketTimeoutMS=int(os.environ.get("MONGO_SOCKET_TIMEOUT_MS", "60000")),
    waitQueueTimeoutMS=int(os.environ.get("MONGO_WAIT_QUEUE_TIMEOUT_MS", "15000")),
    retryWrites=True,
    appname="perklocks-api",
)
db = client[os.environ.get("DB_NAME") or "perkslocks_production"]

# ── auth dependency ───────────────────────────────────────────────────
# Imported here (rather than in server.py) so route modules don't have
# to depend on server.py for `current_user`.
from auth import (  # noqa: E402
    UserPublic,
    get_current_user_from_db,
    oauth2_scheme,
    require_admin_user,
)


async def current_user(
    token: Annotated[Optional[str], Depends(oauth2_scheme)],
) -> UserPublic:
    return await get_current_user_from_db(db, token)


async def current_admin(
    token: Annotated[Optional[str], Depends(oauth2_scheme)],
) -> UserPublic:
    """RBAC gate — 403s on non-admin. Use for every /api/admin/* route
    that mutates state, triggers paid third-party calls, or exposes
    operator-only data (SEC-003, fixed 2026-06-25)."""
    return await require_admin_user(db, token)


# ── small shared utilities ────────────────────────────────────────────
def today_str() -> str:
    """UTC YYYY-MM-DD — the canonical `pick_date` value used across
    every collection. Imported by route modules to avoid a circular
    dependency back into server.py for this trivial helper."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def strip_mongo(doc: dict) -> dict:
    """Drop the internal `_id` field from a Mongo document before
    returning it via the API. Mirrors the legacy `_strip_mongo`
    helper from server.py — exported under both names for back-compat
    while server.py is being decomposed."""
    if doc and "_id" in doc:
        doc = {k: v for k, v in doc.items() if k != "_id"}
    return doc


# Back-compat alias so existing code that already imports
# `_strip_mongo` from server.py keeps working after route extraction.
_strip_mongo = strip_mongo
