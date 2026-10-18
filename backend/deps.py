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


# ─────────────────────────────────────────────────────────────────────
# P0-A Launch-Blocker Fix (2026-06 Master Surgical Fix Build)
# =====================================================================
# ROOT CAUSE PROVEN by tests/test_login_stale_client_repro.py:
#   `from deps import db, client` captures the result of
#   `get_database()` / `get_client()` into the IMPORTING module's
#   globals ONCE at import time.  If `close_database()` later runs
#   (shutdown fallback, admin reset, test harness, idle-pool reset)
#   the authority's `_state.client` is nulled and the importer still
#   holds a reference to the CLOSED motor client.  The next DB
#   operation raises:
#       pymongo.errors.InvalidOperation: Cannot use MongoClient after close
#   which surfaces as `POST /api/auth/login → 500` and only clears
#   on a supervisor restart.  The module-level `__getattr__` above
#   does NOT rescue this case because `from deps import db` resolves
#   the attribute exactly once at import; it never fires again.
#
# FIX (surgical, zero-callsite change):
#   Export `db` and `client` as *lazy proxies*.  Every attribute /
#   subscript access re-resolves through the shared authority
#   (`get_database()` / `get_client()`) which already auto-reinits
#   when `_state.client` is `None`.  Importers like
#   `from deps import db` still get a module-level binding, but the
#   binding is a proxy *object* whose behaviour defers to the LIVE
#   handle on every call.  No new MongoClient per request, no pool
#   leak, no supervisor restart needed, no auth weakening.
# ─────────────────────────────────────────────────────────────────────


class _LazyDatabaseProxy:
    """Resolve the shared database handle on every access.

    Behaves like an :class:`AsyncIOMotorDatabase` for the handful of
    patterns the codebase uses: attribute access (``db.users``),
    item access (``db["users"]``), ``repr``, truthiness, and
    ``isinstance`` is NOT supported (callers don't need it).
    """

    __slots__ = ()

    def _resolve(self):
        return get_database()

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return getattr(self._resolve(), name)

    def __getitem__(self, name):
        return self._resolve()[name]

    def __repr__(self) -> str:                      # pragma: no cover
        try:
            return f"<LazyDBProxy -> {self._resolve()!r}>"
        except Exception:
            return "<LazyDBProxy (unresolved)>"

    def __bool__(self) -> bool:                     # pragma: no cover
        try:
            return self._resolve() is not None
        except Exception:
            return False


class _LazyClientProxy:
    """Resolve the shared motor client on every access."""

    __slots__ = ()

    def _resolve(self):
        return get_client()

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return getattr(self._resolve(), name)

    def __getitem__(self, name):
        return self._resolve()[name]

    def __repr__(self) -> str:                      # pragma: no cover
        try:
            return f"<LazyClientProxy -> {self._resolve()!r}>"
        except Exception:
            return "<LazyClientProxy (unresolved)>"

    def __bool__(self) -> bool:                     # pragma: no cover
        try:
            return self._resolve() is not None
        except Exception:
            return False


# Public module symbols — importers get these proxies, not raw handles.
db = _LazyDatabaseProxy()
client = _LazyClientProxy()


def __getattr__(name):
    """Fallback resolver kept for anyone touching
    ``deps.db`` / ``deps.client`` dynamically (e.g. monkeypatching
    in tests).  The proxies above cover 100% of static ``from deps
    import db`` callsites; this is defence-in-depth."""
    if name == "db":
        return db
    if name == "client":
        return client
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
