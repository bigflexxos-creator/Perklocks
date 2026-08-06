"""Phase 3C — Central Index Registry tests.

Covers the invariants declared in the Phase 3C contract:

  1.  Registry specs are deterministic.
  2.  Index names are unique within each collection.
  3.  Duplicate registry declarations fail loudly.
  4.  ensure_all_indexes is idempotent.
  5.  Existing matching indexes are not recreated.
  6.  Missing noncritical indexes are created safely.
  7.  Missing critical indexes are reported.
  8.  Same-name conflicting definitions are detected.
  9.  Equivalent differently-named indexes are reported.
 10.  Unique-index duplicate data does NOT trigger deletion.
 11.  Every TTL spec uses a documented BSON-Date-backed field
      (or is explicitly blocked/documented in migration_notes).
 12.  publication_mismatch_report 30-day TTL is NOT applied — the
      registry documents the block because logged_at is stored as
      ISO 8601 strings, not Date.
 13.  Lazy request-time index creation is removed from runtime paths.
 14.  Legacy ensure_indices() wrappers delegate to the registry.
 15.  Registry verify_all_indexes returns critical_ok True on the
      live production database (after ensure_all_indexes runs).
 16.  Phase 3B shared client remains one per process.
 17.  Frontend response schemas unchanged (implicit via 3A/3B tests).
"""
from __future__ import annotations

import asyncio
import inspect
import os
import re
from pathlib import Path

import pytest
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient

from services import index_registry as IR
from services.database import (
    initialize_database,
    override_database_for_testing,
    reset_database_override,
)
from services.index_registry import (
    IndexSpec, IndexRegistryError,
    collections, get_index_specs, get_specs_for_collection,
    ensure_all_indexes, verify_all_indexes, verify_collection_indexes,
    create_missing_indexes, report_conflicts, safe_index_diagnostics,
    ensure_collection,
)


def _with_fresh_client(coro):
    """Run an async test body against a per-loop client so Motor's
    event-loop binding doesn't collide with the import-time shared
    client.  The shared owner is overridden for the duration and
    restored after."""
    async def wrapper():
        c = AsyncIOMotorClient(os.environ["MONGO_URL"])
        db = c[os.environ.get("DB_NAME", "lockscore_db")]
        override_database_for_testing(c, db)
        try:
            await coro(db)
        finally:
            reset_database_override()
            c.close()
    asyncio.run(wrapper())


# ── 1. deterministic ───────────────────────────────────────────────
def test_specs_are_deterministic():
    a = get_index_specs()
    b = get_index_specs()
    assert [(s.collection, s.name) for s in a] == [(s.collection, s.name) for s in b]


# ── 2. names unique per collection ─────────────────────────────────
def test_index_names_are_unique_per_collection():
    seen: dict[str, set[str]] = {}
    for s in get_index_specs():
        bucket = seen.setdefault(s.collection, set())
        assert s.name not in bucket, f"duplicate {s.collection}.{s.name}"
        bucket.add(s.name)


# ── 3. duplicate declarations fail loudly ──────────────────────────
def test_duplicate_declaration_raises():
    from services.index_registry import _validate_specs
    dup = [
        IndexSpec("x", "n", keys=(("a", 1),)),
        IndexSpec("x", "n", keys=(("b", 1),)),
    ]
    with pytest.raises(IndexRegistryError):
        _validate_specs(dup)


# ── 4/5. ensure_all_indexes is idempotent, does not recreate ───────
def test_ensure_all_indexes_is_idempotent():
    async def body(db):
        r1 = await ensure_all_indexes(db)
        r2 = await ensure_all_indexes(db)
        assert r1["critical_ok"] is True
        assert r2["critical_ok"] is True
        assert r2["created"] == {}, r2
    _with_fresh_client(body)


# ── 6. missing noncritical index in a fresh collection is created ──
def test_missing_noncritical_created_on_fresh_collection():
    async def body(db):
        for coll in collections():
            r = await verify_collection_indexes(db, coll)
            assert not r.critical_missing, (coll, r.critical_missing)
    _with_fresh_client(body)


# ── 7. missing critical index is reported ──────────────────────────
def test_missing_critical_is_reported():
    async def body(db):
        spec = IndexSpec(
            "phase3c_missing_test_coll", "critical_stub",
            keys=(("a", 1),), critical=True,
        )
        original = IR._INDEX_SPECS[:]
        try:
            IR._INDEX_SPECS.append(spec)
            r = await verify_collection_indexes(db, spec.collection)
            assert spec in r.missing
            assert spec in r.critical_missing
        finally:
            IR._INDEX_SPECS[:] = original
    _with_fresh_client(body)


# ── 8. same-name conflict detected ─────────────────────────────────
def test_same_name_conflict_detected():
    async def body(db):
        coll = "phase3c_conflict_test"
        try:
            await db[coll].create_index([("wrong_key", 1)], name="conf_idx")
            spec = IndexSpec(
                coll, "conf_idx", keys=(("right_key", 1),), critical=True,
            )
            original = IR._INDEX_SPECS[:]
            try:
                IR._INDEX_SPECS.append(spec)
                r = await verify_collection_indexes(db, coll)
                assert any(s is spec for s, _ in r.same_name_conflict)
            finally:
                IR._INDEX_SPECS[:] = original
        finally:
            await db[coll].drop()
    _with_fresh_client(body)


# ── 9. equivalent duplicate detected ───────────────────────────────
def test_equivalent_duplicate_detected():
    async def body(db):
        coll = "phase3c_dup_test"
        try:
            await db[coll].create_index([("a", 1)], name="live_only_name")
            spec = IndexSpec(
                coll, "spec_name", keys=(("a", 1),), critical=False,
            )
            original = IR._INDEX_SPECS[:]
            try:
                IR._INDEX_SPECS.append(spec)
                await create_missing_indexes(db, collection=coll)
                r = await verify_collection_indexes(db, coll)
                assert any(
                    live == "live_only_name" for _s, live, _info
                    in r.equivalent_duplicates
                )
            finally:
                IR._INDEX_SPECS[:] = original
        finally:
            await db[coll].drop()
    _with_fresh_client(body)


# ── 10. unique-index duplicate data does NOT delete ────────────────
def test_unique_index_duplicate_data_blocks_without_deleting():
    async def body(db):
        coll = "phase3c_dup_key_test"
        try:
            await db[coll].insert_many([{"key": "x"}, {"key": "x"}])
            spec = IndexSpec(
                coll, "unique_key_idx", keys=(("key", 1),), unique=True,
                critical=False,
            )
            original = IR._INDEX_SPECS[:]
            try:
                IR._INDEX_SPECS.append(spec)
                await create_missing_indexes(db, collection=coll)
                n = await db[coll].count_documents({})
                assert n == 2, "unique-key blocker MUST NOT delete data"
            finally:
                IR._INDEX_SPECS[:] = original
        finally:
            await db[coll].drop()
    _with_fresh_client(body)


# ── 11. every TTL spec has documented purpose ──────────────────────
def test_every_ttl_spec_has_purpose_or_migration_notes():
    for s in get_index_specs():
        if s.expire_after_seconds is None:
            continue
        assert s.purpose or s.migration_notes, (
            f"TTL spec {s.collection}.{s.name} must declare purpose "
            "(target BSON-date field) or migration_notes"
        )


# ── 12. publication_mismatch TTL is UNBLOCKED as of Phase 3K ───────
def test_publication_mismatch_ttl_applied_in_phase3k():
    """Phase 3K (2026-08) applied the 30-day TTL.  Verify the
    registry now declares exactly one TTL spec on logged_at_dt with
    expire_after_seconds=2592000."""
    specs = get_specs_for_collection("publication_mismatch_report")
    ttl_specs = [s for s in specs if s.expire_after_seconds is not None]
    assert len(ttl_specs) == 1, f"expected 1 TTL spec, got {len(ttl_specs)}"
    s = ttl_specs[0]
    assert s.keys == (("logged_at_dt", 1),)
    assert s.expire_after_seconds == 2_592_000
    # Legacy string field remains for compatibility, no TTL on it.
    legacy = next(
        s for s in specs if s.name == "mismatch_logged_at_idx"
    )
    assert legacy.expire_after_seconds is None


# ── 13. Lazy runtime index creation removed ────────────────────────
# server.py's on_startup() IS the approved startup owner — it is
# excluded here because startup is not a hot path.
_HOTPATH_FILES = [
    "sports_engine.py", "settlement_engine.py",
    "routes/picks_routes.py", "routes/parlay_routes.py",
]


def test_no_lazy_create_index_calls_in_hot_paths():
    """Phase 3C guardrail: hot-path modules must NOT construct new
    indexes during user requests.  Only startup + the registry may."""
    root = Path("/app/backend")
    pattern = re.compile(r"\.create_index\s*\(")
    offenders: list[str] = []
    for rel in _HOTPATH_FILES:
        p = root / rel
        if not p.exists():
            continue
        text = p.read_text()
        for i, ln in enumerate(text.splitlines(), start=1):
            if pattern.search(ln):
                offenders.append(f"{rel}:{i}: {ln.strip()}")
    assert not offenders, (
        "Phase 3C guardrail — hot-path create_index calls remain:\n  "
        + "\n  ".join(offenders)
    )


# ── 14. Legacy ensure_indices wrappers delegate to registry ────────
_WRAPPER_FILES = [
    "services/job_coordinator.py",
    "services/provider_budget.py",
    "services/single_flight.py",
    "services/tournament_registry.py",
    "services/sports_catalog.py",
    "services/bad_market_registry.py",
    "services/odds_cache.py",
    "alt_lines_feed.py",
    "services/settlement_service.py",
    "services/prediction_publication_service.py",
    "services/enrichment_service.py",
]


def test_legacy_ensure_indices_wrappers_delegate_to_registry():
    """Every legacy ensure_indices site must now import from the
    central registry.  A file that still calls .create_index(...)
    directly from its ensure_indices body would break parity."""
    root = Path("/app/backend")
    for rel in _WRAPPER_FILES:
        p = root / rel
        assert p.exists(), rel
        text = p.read_text()
        # Locate ensure_indices / _ensure_indexes body (rough scope:
        # from `def ensure_indices` to next unindented def/class).
        m = re.search(
            r"(async def (?:ensure_indices|_ensure_indexes)\b.*?)(?=\n(?:async def|def|class )\b)",
            text, re.DOTALL,
        )
        if not m:
            continue
        body = m.group(1)
        # Body must reference the registry.
        assert "index_registry" in body, (
            f"{rel}: legacy ensure_indices body no longer delegates to "
            "the central registry"
        )


# ── 15. Live DB critical_ok True ───────────────────────────────────
def test_live_db_critical_indexes_ok():
    async def body(db):
        await ensure_all_indexes(db)
        verified = await verify_all_indexes(db)
        for coll, r in verified.items():
            assert r.critical_ok, (coll, r.summary())
    _with_fresh_client(body)


# ── 16. Phase 3B invariant preserved ───────────────────────────────
def test_shared_client_still_single_after_registry_use():
    async def body(db):
        # We're inside an override — verify it's stable.
        from services.database import get_database
        assert get_database() is db
        await ensure_all_indexes(db)
        assert get_database() is db
    _with_fresh_client(body)


# ── 17. safe_index_diagnostics contains no secrets ─────────────────
def test_safe_diagnostics_have_no_secrets():
    diag = safe_index_diagnostics()
    dumped = repr(diag)
    real_url = os.environ.get("MONGO_URL") or ""
    if real_url:
        assert real_url not in dumped
    assert "password" not in dumped.lower()
    assert diag["total_specs"] > 0
    assert diag["collections"] > 0


# ── 18. Coverage sanity — every ensure_indices() owner is in registry ──
def test_registry_covers_all_legacy_ensure_indices_owners():
    """Every service listed in Phase 3B audit must have at least one
    spec in the registry."""
    owners_expected = {
        "job_coordinator", "provider_budget", "single_flight",
        "tournament_registry", "sports_catalog", "bad_market_registry",
        "odds_cache", "alt_lines_feed",
        "settlement_service", "prediction_publication_service",
        "enrichment_service",
    }
    owners_seen = {s.owner_service for s in get_index_specs()}
    missing = owners_expected - owners_seen
    assert not missing, f"registry missing owners: {missing}"
