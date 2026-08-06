"""database — Phase 3B shared MongoDB lifecycle owner.

Single controlled owner of the runtime Mongo client and database
handle for the FastAPI application.  All runtime services and route
modules that need a Mongo handle must go through this module (either
directly or via the ``deps`` compatibility re-export).

Design goals (Phase 3B contract)
────────────────────────────────
* Exactly one runtime ``AsyncIOMotorClient`` per process.
* Idempotent :func:`initialize_database` — repeated calls with the
  same effective settings are no-ops.  Repeated calls with
  *incompatible* settings raise clearly.
* Test injection: tests may call :func:`override_database_for_testing`
  to install a per-test client + database, and later
  :func:`reset_database_override` to restore the production instance.
* No secrets in :func:`safe_database_diagnostics`.
* No client creation at module import time from *other* modules —
  this module owns the eager init.  Compatibility eagerly initialises
  on first import so that legacy code that does ``from deps import
  db`` at module top-level keeps working during the transition.
* Standalone CLIs (training, scripts) MAY create their own explicit
  client — they do NOT have to go through this module.

Environment variables
─────────────────────
* ``MONGO_URL`` (required in production)
* ``DB_NAME``   (required in production; default ``perkslocks_production`` in dev)
* ``ENVIRONMENT`` (production/preview/development/test)
* ``MONGO_MAX_POOL_SIZE`` (default 20)
* ``MONGO_MIN_POOL_SIZE`` (default 2)
* ``MONGO_SERVER_SEL_TIMEOUT_MS`` (default 10_000)
* ``MONGO_CONNECT_TIMEOUT_MS`` (default 10_000)
* ``MONGO_SOCKET_TIMEOUT_MS`` (default 60_000)
* ``MONGO_WAIT_QUEUE_TIMEOUT_MS`` (default 15_000)

Test guardrail — the Phase 3B repository guardrail scans for direct
``AsyncIOMotorClient(...)`` / ``MongoClient(...)`` construction in
runtime service modules and fails if new ones appear outside the
approved allow-list (this file, tests, scripts, and standalone CLIs).
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

logger = logging.getLogger("lockscore.database")


# ── Pool defaults ────────────────────────────────────────────────────
# Kept identical to the previous deps.py values so this refactor is
# behaviour-preserving.  They are also documented as the Phase 3B
# canonical pool sizing.
_DEFAULT_POOL_KWARGS: dict[str, int | bool | str] = {
    "maxPoolSize":               20,
    "minPoolSize":               2,
    "serverSelectionTimeoutMS": 10_000,
    "connectTimeoutMS":         10_000,
    "socketTimeoutMS":          60_000,
    "waitQueueTimeoutMS":       15_000,
    "retryWrites":              True,
    "appname":                  "perklocks-api",
}


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key) or default)
    except (TypeError, ValueError):
        return default


def _resolve_pool_kwargs() -> dict[str, Any]:
    kw = dict(_DEFAULT_POOL_KWARGS)
    kw["maxPoolSize"]              = _env_int("MONGO_MAX_POOL_SIZE",       kw["maxPoolSize"])
    kw["minPoolSize"]              = _env_int("MONGO_MIN_POOL_SIZE",       kw["minPoolSize"])
    kw["serverSelectionTimeoutMS"] = _env_int("MONGO_SERVER_SEL_TIMEOUT_MS", kw["serverSelectionTimeoutMS"])
    kw["connectTimeoutMS"]         = _env_int("MONGO_CONNECT_TIMEOUT_MS",  kw["connectTimeoutMS"])
    kw["socketTimeoutMS"]          = _env_int("MONGO_SOCKET_TIMEOUT_MS",   kw["socketTimeoutMS"])
    kw["waitQueueTimeoutMS"]       = _env_int("MONGO_WAIT_QUEUE_TIMEOUT_MS", kw["waitQueueTimeoutMS"])
    return kw


# ── Errors ───────────────────────────────────────────────────────────
class DatabaseError(RuntimeError):
    pass


class DatabaseNotInitialized(DatabaseError):
    pass


class DatabaseAlreadyInitialized(DatabaseError):
    """Raised when initialize_database() is called a second time with
    incompatible settings (different MONGO_URL or DB_NAME)."""


# ── State container ──────────────────────────────────────────────────
@dataclass
class _State:
    client:       Optional[AsyncIOMotorClient]           = None
    database:     Optional[AsyncIOMotorDatabase]         = None
    mongo_url:    Optional[str]                          = None
    db_name:      Optional[str]                          = None
    pool_kwargs:  dict[str, Any]                         = field(default_factory=dict)
    is_override:  bool                                   = False
    saved:        Optional["_State"]                     = None


_state = _State()
_lock  = threading.Lock()


# ── Public helpers ───────────────────────────────────────────────────
def _resolve_env_config() -> tuple[str, str]:
    """Resolve MONGO_URL and DB_NAME from environment, applying safe
    non-production defaults.  Production enforcement lives in
    :class:`services.settings.AppSettings`."""
    mongo_url = (os.environ.get("MONGO_URL") or "").strip()
    db_name   = (os.environ.get("DB_NAME") or "").strip()
    if not mongo_url:
        mongo_url = "mongodb://localhost:27017"
    if not db_name:
        db_name = "perkslocks_production"
    return mongo_url, db_name


def initialize_database(
    mongo_url:  Optional[str] = None,
    db_name:    Optional[str] = None,
    pool_kwargs: Optional[dict[str, Any]] = None,
) -> AsyncIOMotorDatabase:
    """Eagerly initialise the shared client/database.

    Idempotent when called with matching parameters.  Raises
    :class:`DatabaseAlreadyInitialized` if a subsequent call would
    silently swap out the client for a different destination.

    Override-mode (see :func:`override_database_for_testing`) is not
    disturbed by production initialisation attempts.
    """
    with _lock:
        # If a test override is active, leave it alone.
        if _state.is_override and _state.database is not None:
            return _state.database

        if mongo_url is None or db_name is None:
            env_url, env_db = _resolve_env_config()
            mongo_url = mongo_url or env_url
            db_name   = db_name   or env_db

        pk = _resolve_pool_kwargs()
        if pool_kwargs:
            pk.update(pool_kwargs)

        # Already initialised — check compatibility.
        if _state.client is not None:
            if _state.mongo_url != mongo_url or _state.db_name != db_name:
                raise DatabaseAlreadyInitialized(
                    "database already initialised with "
                    f"({_state.mongo_url!r}, {_state.db_name!r}); refused "
                    f"re-init with ({mongo_url!r}, {db_name!r})"
                )
            return _state.database   # type: ignore[return-value]

        # Fresh init.
        client = AsyncIOMotorClient(mongo_url, **pk)
        database = client[db_name]
        _state.client       = client
        _state.database     = database
        _state.mongo_url    = mongo_url
        _state.db_name      = db_name
        _state.pool_kwargs  = pk
        logger.info(
            "shared Mongo client initialised (db=%s, maxPool=%s, minPool=%s)",
            db_name, pk["maxPoolSize"], pk["minPoolSize"],
        )
        return database


def get_client() -> AsyncIOMotorClient:
    if _state.client is None:
        # Fall back to auto-init so pre-startup imports (deps.py
        # module-level) do not crash.  Production-grade validation
        # happens in AppSettings; this function stays low-level.
        initialize_database()
    return _state.client   # type: ignore[return-value]


def get_database() -> AsyncIOMotorDatabase:
    if _state.database is None:
        initialize_database()
    return _state.database   # type: ignore[return-value]


def is_initialized() -> bool:
    return _state.client is not None and _state.database is not None


async def close_database() -> None:
    """Close the shared client exactly once.  Repeated calls are safe."""
    with _lock:
        if _state.is_override:
            # Never close a test-injected client from here.
            return
        c = _state.client
        _state.client   = None
        _state.database = None
        _state.mongo_url = None
        _state.db_name   = None
    if c is not None:
        try:
            c.close()
            logger.info("shared Mongo client closed")
        except Exception as e:                          # pragma: no cover
            logger.warning("shared Mongo close raised: %s", e)


async def ping_database(timeout_ms: int = 3000) -> bool:
    """Ping the shared cluster.  Returns True on success, False on
    failure.  Never raises."""
    try:
        c = get_client()
        # Motor ping honours serverSelectionTimeoutMS via admin cmd.
        await c.admin.command("ping", maxTimeMS=timeout_ms)
        return True
    except Exception as e:
        logger.warning("Mongo ping failed: %s", e)
        return False


def safe_database_diagnostics() -> dict[str, Any]:
    """Diagnostics safe for admin surfaces.  Never includes the raw
    connection string, credentials, or database contents."""
    mu = _state.mongo_url or ""
    return {
        "initialized":         is_initialized(),
        "override_active":     _state.is_override,
        "mongo_url_present":   bool(mu),
        "mongo_url_is_local":  ("localhost" in mu.lower() or "127.0.0.1" in mu.lower()),
        "mongo_url_length":    len(mu),
        "db_name":             _state.db_name,  # not secret
        "pool_kwargs":         dict(_state.pool_kwargs),
    }


# ── Test injection ────────────────────────────────────────────────────
def override_database_for_testing(
    client:   AsyncIOMotorClient,
    database: AsyncIOMotorDatabase,
) -> None:
    """Install a test-supplied client + database.  The previous state
    (if any) is saved and restored by :func:`reset_database_override`.
    Multiple overrides stack — the most recent reset restores the
    immediately-prior state."""
    with _lock:
        prior = _State(
            client=_state.client,
            database=_state.database,
            mongo_url=_state.mongo_url,
            db_name=_state.db_name,
            pool_kwargs=dict(_state.pool_kwargs),
            is_override=_state.is_override,
            saved=_state.saved,
        )
        _state.client       = client
        _state.database     = database
        _state.mongo_url    = "test://override"
        _state.db_name      = database.name
        _state.pool_kwargs  = {}
        _state.is_override  = True
        _state.saved        = prior


def reset_database_override() -> None:
    """Undo the most recent override.  Safe to call when no override
    is active (no-op)."""
    with _lock:
        prior = _state.saved
        if prior is None:
            # No override to unwind.
            _state.is_override = False
            return
        _state.client       = prior.client
        _state.database     = prior.database
        _state.mongo_url    = prior.mongo_url
        _state.db_name      = prior.db_name
        _state.pool_kwargs  = dict(prior.pool_kwargs)
        _state.is_override  = prior.is_override
        _state.saved        = prior.saved


# ── Sync client (for the two legacy synchronous callsites) ───────────
# elite_players.py and sport_adapters/nba.py use short-lived pymongo
# MongoClient instances.  They can't share the async client but they
# CAN share URL/DB resolution and a single reusable pool.
_sync_client = None
_sync_lock   = threading.Lock()


def get_sync_client():
    """Return a lazily-constructed shared pymongo MongoClient.  Used
    only by the two synchronous callsites (elite_players,
    sport_adapters.nba).  Async callers must use :func:`get_client`."""
    global _sync_client
    if _sync_client is None:
        with _sync_lock:
            if _sync_client is None:
                from pymongo import MongoClient
                mongo_url, _ = _resolve_env_config()
                _sync_client = MongoClient(
                    mongo_url,
                    maxPoolSize=5,
                    serverSelectionTimeoutMS=2000,
                    connectTimeoutMS=5000,
                    appname="perklocks-sync",
                )
    return _sync_client


def get_sync_database():
    """Return the shared pymongo database handle."""
    _, name = _resolve_env_config()
    return get_sync_client()[name]


def _reset_sync_client_for_testing() -> None:  # pragma: no cover
    """Test helper — close and drop the sync client so a subsequent
    call reinitialises against fresh env."""
    global _sync_client
    with _sync_lock:
        c = _sync_client
        _sync_client = None
    if c is not None:
        try:
            c.close()
        except Exception:
            pass


__all__ = [
    "DatabaseError",
    "DatabaseNotInitialized",
    "DatabaseAlreadyInitialized",
    "initialize_database",
    "get_client",
    "get_database",
    "is_initialized",
    "close_database",
    "ping_database",
    "safe_database_diagnostics",
    "override_database_for_testing",
    "reset_database_override",
    "get_sync_client",
    "get_sync_database",
]
