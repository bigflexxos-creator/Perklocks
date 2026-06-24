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
client = AsyncIOMotorClient(_mongo_url)
db = client[os.environ.get("DB_NAME") or "perkslocks_production"]

# ── auth dependency ───────────────────────────────────────────────────
# Imported here (rather than in server.py) so route modules don't have
# to depend on server.py for `current_user`.
from auth import (  # noqa: E402
    UserPublic,
    get_current_user_from_db,
    oauth2_scheme,
)


async def current_user(
    token: Annotated[Optional[str], Depends(oauth2_scheme)],
) -> UserPublic:
    return await get_current_user_from_db(db, token)


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
