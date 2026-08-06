"""Phase 3B — Shared Mongo Client Consolidation tests.

Verifies the invariants declared in the Phase 3B contract:

  1. Repeated initialize_database() returns the SAME client instance.
  2. Reinitialisation with a different destination raises clearly.
  3. deps.db and services.database.get_database() are the SAME object.
  4. Importing runtime service modules does NOT open a new client.
  5. Production env with missing MONGO_URL raises via settings.
  6. Production env with missing DB_NAME raises via settings.
  7. Production env with localhost MONGO_URL raises via settings.
  8. override_database_for_testing() installs a test handle.
  9. reset_database_override() restores the prior handle.
 10. close_database() closes the client; repeated close is safe.
 11. safe_database_diagnostics() contains no secret values.
 12. Runtime client guardrail — no new module-level
     AsyncIOMotorClient/MongoClient calls appear in runtime service
     modules outside the approved allow-list.
 13. Ping helper succeeds against the running Mongo instance.
 14. Runtime service modules that previously created their own client
     now route through the shared owner.
 15. Compat: deps.client remains a valid attribute.
"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import pytest
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient

from services import database as SDB
from services.database import (
    AsyncIOMotorClient as _RE_MOTOR_ALIAS,  # noqa: F401 (import-side check)
    DatabaseAlreadyInitialized,
    close_database,
    get_client,
    get_database,
    initialize_database,
    is_initialized,
    override_database_for_testing,
    ping_database,
    reset_database_override,
    safe_database_diagnostics,
)


# ── 1. idempotent init ─────────────────────────────────────────────
def test_initialize_database_is_idempotent():
    a = initialize_database()
    b = initialize_database()
    assert a is b
    assert get_database() is a
    assert is_initialized() is True


# ── 2. conflicting init raises ─────────────────────────────────────
def test_initialize_with_different_target_raises():
    initialize_database()   # ensure we're initialised
    with pytest.raises(DatabaseAlreadyInitialized):
        initialize_database(
            mongo_url="mongodb://elsewhere:27017",
            db_name="something_else",
        )


# ── 3. deps.db identity ────────────────────────────────────────────
def test_deps_db_is_the_shared_database():
    import deps
    assert deps.db is get_database()
    assert deps.client is get_client()


# ── 4. importing runtime services does not add a new client ────────
def test_runtime_service_imports_do_not_create_new_clients(monkeypatch):
    # Count constructions by monkey-patching the class.  Any construction
    # after the shared owner is initialised must NOT happen at import
    # time for these modules.
    initialize_database()
    created = {"n": 0}
    real_init = AsyncIOMotorClient.__init__

    def counting_init(self, *a, **k):
        created["n"] += 1
        return real_init(self, *a, **k)

    monkeypatch.setattr(AsyncIOMotorClient, "__init__", counting_init)

    # Re-import runtime modules (they may already be imported; the
    # counter proves no NEW client is constructed).
    import importlib
    for name in (
        "routes.telemetry_routes",
        "services.game_context",
        "services.mlb_team_k_intel",
        "services.odds_cache",
        "elite_players",
        "sport_adapters.nba",
    ):
        try:
            mod = importlib.import_module(name)
            importlib.reload(mod)
        except Exception:
            # Some modules may fail to reload due to inter-module state.
            # The invariant we care about is client construction count.
            pass
    assert created["n"] == 0, (
        f"runtime modules unexpectedly created {created['n']} Motor client(s) at "
        "import time — Phase 3B forbids this"
    )


# ── 5/6/7. Production validation (delegated to AppSettings) ────────
def test_production_missing_mongo_url_rejected(monkeypatch):
    from services.settings import AppSettings, SettingsError
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("MONGO_URL", raising=False)
    monkeypatch.setenv("DB_NAME", "prod")
    monkeypatch.setenv("JWT_SECRET", "x" * 40)
    with pytest.raises(SettingsError):
        AppSettings.load()


def test_production_missing_db_name_rejected(monkeypatch):
    from services.settings import AppSettings, SettingsError
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("MONGO_URL", "mongodb://prod-cluster:27017")
    monkeypatch.delenv("DB_NAME", raising=False)
    monkeypatch.setenv("JWT_SECRET", "x" * 40)
    with pytest.raises(SettingsError):
        AppSettings.load()


def test_production_localhost_mongo_rejected(monkeypatch):
    from services.settings import AppSettings, SettingsError
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("MONGO_URL", "mongodb://localhost:27017")
    monkeypatch.setenv("DB_NAME", "prod")
    monkeypatch.setenv("JWT_SECRET", "x" * 40)
    with pytest.raises(SettingsError):
        AppSettings.load()


# ── 8/9. Test override + reset ─────────────────────────────────────
def test_test_override_and_reset_restores_prior_state():
    initialize_database()
    original = get_database()
    # Install override with a fresh test client.
    test_client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    test_db     = test_client["phase3b_override_db"]
    override_database_for_testing(test_client, test_db)
    try:
        assert get_database() is test_db
        assert get_client()   is test_client
        diag = safe_database_diagnostics()
        assert diag["override_active"] is True
        assert diag["db_name"] == "phase3b_override_db"
    finally:
        reset_database_override()
        test_client.close()

    assert get_database() is original
    diag = safe_database_diagnostics()
    assert diag["override_active"] is False


def test_reset_without_override_is_safe():
    # Should not raise even if no override is active.
    reset_database_override()
    reset_database_override()


# ── 10. close_database is idempotent + safe ────────────────────────
def test_close_database_is_idempotent():
    async def run():
        initialize_database()
        assert is_initialized() is True
        await close_database()
        assert is_initialized() is False
        # Second close is a no-op.
        await close_database()
        # Reinitialise for downstream tests.
        initialize_database()
    asyncio.run(run())


# ── 11. safe diagnostics never leaks secrets ───────────────────────
def test_safe_diagnostics_do_not_leak_secrets(monkeypatch):
    initialize_database()
    diag = safe_database_diagnostics()
    dumped = repr(diag)
    # The real MONGO_URL should not appear in diagnostics output.
    real = os.environ.get("MONGO_URL") or ""
    if real:
        assert real not in dumped
    assert "password" not in dumped.lower()
    assert diag.get("mongo_url_present") is True


# ── 12. Guardrail — no illicit runtime clients ─────────────────────
_ALLOWED_RUNTIME_CLIENT_FILES = {
    # Approved owner:
    "services/database.py",
    # Test-loop compatibility fallback (documented in Phase 3B audit):
    "services/odds_cache.py",
}
_RUNTIME_SCAN_DIRS = ("services", "routes", "brain", "sport_adapters")
_RUNTIME_SCAN_FILES = (
    "server.py", "sports_engine.py", "elite_players.py", "deps.py",
    "sportdb_client.py", "auth.py", "settlement_engine.py",
    "player_db/client.py",
)


def _scan_runtime_files() -> list[Path]:
    root = Path("/app/backend")
    found: list[Path] = []
    for sub in _RUNTIME_SCAN_DIRS:
        found.extend((root / sub).rglob("*.py"))
    for f in _RUNTIME_SCAN_FILES:
        p = root / f
        if p.exists():
            found.append(p)
    return found


def test_no_new_runtime_motor_clients_outside_allowlist():
    """Phase 3B repository guardrail — no runtime service module may
    create its own AsyncIOMotorClient(...) call outside the approved
    allow-list (services/database.py and the test-loop fallback in
    odds_cache.py)."""
    pattern = re.compile(r"\bAsyncIOMotorClient\s*\(")
    offenders: list[str] = []
    for path in _scan_runtime_files():
        rel = str(path.relative_to("/app/backend"))
        if rel in _ALLOWED_RUNTIME_CLIENT_FILES:
            continue
        try:
            text = path.read_text()
        except Exception:
            continue
        # Ignore CLI blocks — lines inside a function named _main are
        # explicitly allowed (they are standalone entry points).
        # We use a simple heuristic: if the line lives inside the
        # `_main` function OR after an `if __name__ == "__main__"`
        # guard, we skip it.
        in_cli = False
        for ln in text.splitlines():
            s = ln.strip()
            if s.startswith("async def _main") or s.startswith("def _main"):
                in_cli = True
            elif s.startswith('if __name__'):
                in_cli = True
            if pattern.search(ln) and not in_cli:
                offenders.append(f"{rel}: {s}")
    assert not offenders, (
        "Phase 3B guardrail failed — direct AsyncIOMotorClient calls in "
        "runtime modules outside the allow-list:\n  " + "\n  ".join(offenders)
    )


def test_no_new_runtime_pymongo_clients_outside_allowlist():
    """Same guardrail for the sync pymongo MongoClient."""
    pattern = re.compile(r"(?<!Async)\bMongoClient\s*\(")
    offenders: list[str] = []
    for path in _scan_runtime_files():
        rel = str(path.relative_to("/app/backend"))
        if rel in _ALLOWED_RUNTIME_CLIENT_FILES:
            continue
        try:
            text = path.read_text()
        except Exception:
            continue
        in_cli = False
        for ln in text.splitlines():
            s = ln.strip()
            if s.startswith("async def _main") or s.startswith("def _main"):
                in_cli = True
            elif s.startswith('if __name__'):
                in_cli = True
            if pattern.search(ln) and not in_cli:
                offenders.append(f"{rel}: {s}")
    assert not offenders, (
        "Phase 3B guardrail failed — direct MongoClient calls in "
        "runtime modules outside the allow-list:\n  " + "\n  ".join(offenders)
    )


# ── 13. Ping helper ────────────────────────────────────────────────
def test_ping_database_succeeds():
    async def run():
        initialize_database()
        ok = await ping_database()
        assert ok is True
    asyncio.run(run())


# ── 14. Migrated services route through shared owner ───────────────
def test_game_context_uses_shared_owner():
    initialize_database()
    from services.game_context import _get_db as gc_get
    assert gc_get() is get_database()


def test_mlb_team_k_intel_uses_shared_owner():
    initialize_database()
    from services.mlb_team_k_intel import _get_db as km_get
    # Reset the module-level lazy cache so we see the shared owner.
    import services.mlb_team_k_intel as km
    km._LAZY_DB = None
    assert km_get() is get_database()


# ── 15. deps compat ────────────────────────────────────────────────
def test_deps_client_and_db_are_valid_after_refactor():
    import deps
    assert deps.client is get_client()
    assert deps.db is get_database()
    # And the auth dependency still resolves.
    assert callable(deps.current_user)
    assert callable(deps.current_admin)


# ── Bonus. Ping failure surfaces cleanly ───────────────────────────
def test_ping_failure_is_reported_false(monkeypatch):
    async def _fail(*a, **k):
        raise RuntimeError("simulated ping failure")
    # Patch the admin.command coroutine.
    class _FakeAdmin:
        async def command(self, *a, **k):
            raise RuntimeError("simulated ping failure")
    class _FakeClient:
        admin = _FakeAdmin()
    monkeypatch.setattr(SDB, "get_client", lambda: _FakeClient())
    async def run():
        assert await ping_database() is False
    asyncio.run(run())
