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
# Phase 3B (2026-08): the shared Mongo client + database now live in
# services/database.py.  This module keeps `client` and `db` as
# compatibility re-exports so the ~30+ existing `from deps import db`
# and `from deps import client, db` callsites continue to work
# unchanged.  Pool configuration and env resolution moved to
# services/database.py.
from services.database import (  # noqa: E402
    initialize_database,
    get_client,
    get_database,
)

# Eagerly initialise on first import so legacy module-level uses of
# `deps.db` and `deps.client` see a live handle.  The FastAPI lifespan
# ALSO calls initialize_database() at startup — that call is idempotent.
initialize_database()


def __getattr__(name):
    """Module-level lookup so ``deps.db`` and ``deps.client`` ALWAYS
    resolve to the shared owner's current instances.  This survives
    ``close_database()`` + ``initialize_database()`` cycles used in
    tests and re-entrant startup scenarios."""
    if name == "db":
        return get_database()
    if name == "client":
        return get_client()
    raise AttributeError(f"module 'deps' has no attribute {name!r}")

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
    return await get_current_user_from_db(get_database(), token)


async def current_admin(
    token: Annotated[Optional[str], Depends(oauth2_scheme)],
) -> UserPublic:
    """RBAC gate — 403s on non-admin. Use for every /api/admin/* route
    that mutates state, triggers paid third-party calls, or exposes
    operator-only data (SEC-003, fixed 2026-06-25)."""
    return await require_admin_user(get_database(), token)


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
