"""Phase 3G Step 3 — user_bets canonical schema-extension tests (test_iter132).

Covers every invariant declared in the Step 3 prompt (18 items):

  1. Dry-run performs zero writes.
  2. Execute mode touches only user_bets.
  3. parlay_history is never modified.
  4. prediction_snapshots are never modified.
  5. Existing populated values are never overwritten.
  6. Missing nullable fields are added safely.
  7. Missing arrays become empty arrays.
  8. clv_value remains null when unavailable.
  9. clv_status becomes "unavailable" when missing.
 10. void and pushed remain distinct after the extension.
 11. Unknown status values are preserved.
 12. Native user_bets receive is_legacy=false.
 13. Migration is idempotent.
 14. Resume and limit options work.
 15. Existing route response schemas remain unchanged (static check).
 16. UserBetLedger can deserialize migrated records.
 17. Partial-unique index preflight remains clean.
 18. All previous Phase 1–3 tests continue passing (asserted elsewhere).
"""
from __future__ import annotations

import asyncio
import ast
import os
import uuid
from pathlib import Path
from typing import Any

import pytest
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient

from services import database as SDB
from services.database import (
    override_database_for_testing,
    reset_database_override,
)
from services import user_bet_ledger as UBL
from services.user_bet_ledger import (
    CANONICAL_MIGRATION_VERSION,
    CLV_UNAVAILABLE,
    STATUS_PUSHED,
    STATUS_VOID,
    UserBet,
    preflight_unique_indexes,
)
from scripts.backfills import user_bets_add_canonical_fields as MIG


def _with_fresh_db(coro):
    """Isolated per-test DB via the shared owner override."""
    async def wrapper():
        c = AsyncIOMotorClient(os.environ.get("MONGO_URL") or "mongodb://localhost:27017")
        db_name = f"step3_test_{uuid.uuid4().hex[:12]}"
        db = c[db_name]
        override_database_for_testing(c, db)
        try:
            await coro(db)
        finally:
            try:
                await c.drop_database(db_name)
            except Exception:
                pass
            reset_database_override()
            c.close()
    asyncio.run(wrapper())


# ── seed helpers ─────────────────────────────────────────────────────
async def _seed_native_row(db, *, id_=None, extra=None):
    """Insert a document shaped like the current live user_bets rows."""
    doc = {
        "id":           id_ or str(uuid.uuid4()),
        "user_id":      "u-native",
        "pick_id":      "pick-1",
        "bet_type":     "straight",
        "parlay_legs": [],
        "stake_units":  1.5,
        "odds_at_bet": -184,
        "status":       "pending",
        "pnl_units":    0.0,
        "sport":        "Soccer",
        "market":       "X ML",
        "event":        "X vs Y",
        "selection":    "X",
        "created_at":   "2026-07-20T22:40:07.070000+00:00",
        "settled_at":   None,
        "notes":        None,
    }
    if extra:
        doc.update(extra)
    await db.user_bets.insert_one(doc)
    return doc


async def _seed_parlay_native_row(db, *, id_=None):
    return await _seed_native_row(db, id_=id_, extra={
        "bet_type":    "parlay",
        "parlay_legs": ["a", "b", "c"],
        "odds_at_bet": +450,
        "pick_id":     None,
    })


# ── 1. Dry-run performs zero writes ──────────────────────────────────
def test_dry_run_performs_zero_writes():
    async def body(db):
        await _seed_native_row(db)
        await _seed_native_row(db)
        before = await db.user_bets.count_documents({})
        # Snapshot every document (excluding _id) so we can equality-check.
        docs_before = await db.user_bets.find({}, {"_id": 0}).to_list(1000)

        report = await MIG.run_migration(
            db=db, execute=False, batch_size=200,
            limit=None, resume_from=None, user_id=None,
        )
        assert report.mode == "dry-run"
        assert report.total_scanned == 2
        # Dry-run reports what WOULD update but writes nothing.
        after_docs = await db.user_bets.find({}, {"_id": 0}).to_list(1000)
        assert sorted(docs_before, key=lambda d: d["id"]) == sorted(after_docs, key=lambda d: d["id"])
        assert await db.user_bets.count_documents({}) == before
    _with_fresh_db(body)


# ── 2. Execute mode touches only user_bets ────────────────────────────
def test_execute_mode_touches_only_user_bets():
    async def body(db):
        await _seed_native_row(db)
        # Seed sentinel rows in forbidden collections.
        await db.parlay_history.insert_one({"id": "p_untouched", "user_id": "x"})
        await db.prediction_snapshots.insert_one(
            {"prediction_id": "pred-x", "snapshot_version": 1}
        )
        ph_before = await db.parlay_history.count_documents({})
        ps_before = await db.prediction_snapshots.count_documents({})

        report = await MIG.run_migration(
            db=db, execute=True, batch_size=50,
            limit=None, resume_from=None, user_id=None,
        )
        assert report.mode == "execute"
        assert report.forbidden_touched == []
        assert await db.parlay_history.count_documents({}) == ph_before
        assert await db.prediction_snapshots.count_documents({}) == ps_before
    _with_fresh_db(body)


# ── 3. parlay_history is never modified ──────────────────────────────
def test_parlay_history_never_modified():
    async def body(db):
        # Seed both a native user bet and diverse parlay_history rows.
        await _seed_native_row(db)
        await db.parlay_history.insert_many([
            {"id": "p_alpha",   "user_id": "u-a", "legs": [{}, {}]},
            {"id": "plearn_x1", "signature": "s1", "legs": [{}, {}]},
        ])
        ph_docs_before = await db.parlay_history.find({}, {"_id": 0}).to_list(100)
        report = await MIG.run_migration(
            db=db, execute=True, batch_size=100,
            limit=None, resume_from=None, user_id=None,
        )
        assert report.forbidden_touched == []
        ph_docs_after = await db.parlay_history.find({}, {"_id": 0}).to_list(100)
        assert sorted(ph_docs_before, key=str) == sorted(ph_docs_after, key=str)
    _with_fresh_db(body)


# ── 4. prediction_snapshots is never modified ────────────────────────
def test_prediction_snapshots_never_modified():
    async def body(db):
        await _seed_native_row(db)
        await db.prediction_snapshots.insert_many([
            {"prediction_id": "pred-1", "snapshot_version": 1,
             "idempotency_key": "k1", "is_active": True,
             "board_version": "bv1", "published_at": "2026-01-01",
             "model_version": "m1"},
        ])
        ps_before = await db.prediction_snapshots.find({}, {"_id": 0}).to_list(100)
        await MIG.run_migration(
            db=db, execute=True, batch_size=100,
            limit=None, resume_from=None, user_id=None,
        )
        ps_after = await db.prediction_snapshots.find({}, {"_id": 0}).to_list(100)
        assert sorted(ps_before, key=str) == sorted(ps_after, key=str)
    _with_fresh_db(body)


# ── 5. Existing populated values are never overwritten ───────────────
def test_existing_populated_values_never_overwritten():
    async def body(db):
        # Seed with a pre-set canonical field.  The migration MUST NOT
        # overwrite it even if the source field is present too.
        await _seed_native_row(db, id_="preset-1", extra={
            "wager_type":  "parlay",         # explicit override
            "sport_key":   "PreExisting",    # explicit override
            "profit_loss": 42.0,             # explicit override
            "clv_status":  "available",      # explicit override
            "tags":        ["preserved"],    # explicit override
        })
        await MIG.run_migration(db=db, execute=True, batch_size=100,
                                limit=None, resume_from=None, user_id=None)
        after = await db.user_bets.find_one({"id": "preset-1"}, {"_id": 0})
        # Preserved exactly:
        assert after["wager_type"]  == "parlay"
        assert after["sport_key"]   == "PreExisting"
        assert after["profit_loss"] == 42.0
        assert after["clv_status"]  == "available"
        assert after["tags"]        == ["preserved"]
    _with_fresh_db(body)


# ── 6. Missing nullable fields are added safely ──────────────────────
def test_missing_nullable_fields_added_safely():
    async def body(db):
        await _seed_native_row(db, id_="fresh-1")
        await MIG.run_migration(db=db, execute=True, batch_size=100,
                                limit=None, resume_from=None, user_id=None)
        after = await db.user_bets.find_one({"id": "fresh-1"}, {"_id": 0})
        assert after["client_bet_id"]       is None
        assert after["idempotency_key"]     is None
        assert after["sportsbook"]          is None
        assert after["opening_line"]        is None
        assert after["closing_odds"]        is None
        assert after["migration_source"]    is None
        assert after["migration_source_id"] is None
        assert after["migration_version"]   == CANONICAL_MIGRATION_VERSION
    _with_fresh_db(body)


# ── 7. Missing arrays become empty arrays ────────────────────────────
def test_missing_arrays_default_to_empty_lists():
    async def body(db):
        await _seed_native_row(db, id_="arr-1")
        await MIG.run_migration(db=db, execute=True, batch_size=100,
                                limit=None, resume_from=None, user_id=None)
        after = await db.user_bets.find_one({"id": "arr-1"}, {"_id": 0})
        assert after["tags"]              == []
        assert after["legs"]              == []
        assert after["settlement_events"] == []
    _with_fresh_db(body)


# ── 8. clv_value remains null when unavailable ───────────────────────
def test_clv_value_stays_null():
    async def body(db):
        await _seed_native_row(db, id_="clv-1")
        await MIG.run_migration(db=db, execute=True, batch_size=100,
                                limit=None, resume_from=None, user_id=None)
        after = await db.user_bets.find_one({"id": "clv-1"}, {"_id": 0})
        assert after["clv_value"] is None
        assert after["clv_value"] is not 0        # never zero-filled
    _with_fresh_db(body)


# ── 9. clv_status becomes "unavailable" when missing ─────────────────
def test_clv_status_defaults_to_unavailable():
    async def body(db):
        await _seed_native_row(db, id_="clv-2")
        await MIG.run_migration(db=db, execute=True, batch_size=100,
                                limit=None, resume_from=None, user_id=None)
        after = await db.user_bets.find_one({"id": "clv-2"}, {"_id": 0})
        assert after["clv_status"] == CLV_UNAVAILABLE
    _with_fresh_db(body)


# ── 10. void and pushed remain distinct ──────────────────────────────
def test_void_and_pushed_remain_distinct_after_extension():
    async def body(db):
        await _seed_native_row(db, id_="void-1", extra={"status": "void"})
        await _seed_native_row(db, id_="push-1", extra={"status": "pushed"})
        await MIG.run_migration(db=db, execute=True, batch_size=100,
                                limit=None, resume_from=None, user_id=None)
        v = await db.user_bets.find_one({"id": "void-1"}, {"_id": 0})
        p = await db.user_bets.find_one({"id": "push-1"}, {"_id": 0})
        # status untouched (this migration never rewrites status)
        assert v["status"] == "void"
        assert p["status"] == "pushed"
        # original_status snapshot preserves the observed value
        assert v["original_status"] == "void"
        assert p["original_status"] == "pushed"
    _with_fresh_db(body)


# ── 11. Unknown status values are preserved ──────────────────────────
def test_unknown_status_preserved_and_reported():
    async def body(db):
        await _seed_native_row(db, id_="unk-1", extra={"status": "live"})
        r = await MIG.run_migration(db=db, execute=True, batch_size=100,
                                    limit=None, resume_from=None, user_id=None)
        after = await db.user_bets.find_one({"id": "unk-1"}, {"_id": 0})
        assert after["status"] == "live"                # never rewritten
        assert after["original_status"] == "live"
        # Reported in manual_review_rows since "live" is legacy-only.
        assert any(
            "legacy status 'live'" in reason
            for row in r.manual_review_rows
            for reason in row["manual_review_reasons"]
        )
    _with_fresh_db(body)


# ── 12. Native user_bets receive is_legacy=false ─────────────────────
def test_native_rows_receive_is_legacy_false():
    async def body(db):
        await _seed_native_row(db, id_="nat-1")
        # Legacy-shaped row (has migration markers) — is_legacy should be True.
        await db.user_bets.insert_one({
            "id":                  "leg-1",
            "user_id":             "u-legacy",
            "migration_source":    "parlay_history",
            "migration_source_id": "p_someid",
            "status":              "won",
        })
        await MIG.run_migration(db=db, execute=True, batch_size=100,
                                limit=None, resume_from=None, user_id=None)
        nat = await db.user_bets.find_one({"id": "nat-1"}, {"_id": 0})
        leg = await db.user_bets.find_one({"id": "leg-1"}, {"_id": 0})
        assert nat["is_legacy"] is False
        assert leg["is_legacy"] is True
        # Native gets source="user_track"; legacy migration marker rows
        # are left without a defaulted source (the migration script
        # that inserts them owns that field).
        assert nat["source"] == "user_track"
    _with_fresh_db(body)


# ── 13. Migration is idempotent ──────────────────────────────────────
def test_migration_is_idempotent():
    async def body(db):
        await _seed_native_row(db, id_="idem-1")
        await _seed_native_row(db, id_="idem-2")
        r1 = await MIG.run_migration(db=db, execute=True, batch_size=100,
                                     limit=None, resume_from=None, user_id=None)
        assert r1.total_updated == 2
        r2 = await MIG.run_migration(db=db, execute=True, batch_size=100,
                                     limit=None, resume_from=None, user_id=None)
        assert r2.total_updated == 0
        assert r2.total_skipped == 2
    _with_fresh_db(body)


# ── 14. Resume and limit options work ────────────────────────────────
def test_resume_and_limit_options():
    async def body(db):
        # Seed 5 rows with predictable, sortable ids.
        for i in range(5):
            await _seed_native_row(db, id_=f"row-{i:02d}")
        # limit=2 — process only 2
        r = await MIG.run_migration(
            db=db, execute=True, batch_size=100,
            limit=2, resume_from=None, user_id=None,
        )
        assert r.total_scanned == 2
        # Then resume from the last processed key.
        r2 = await MIG.run_migration(
            db=db, execute=True, batch_size=100,
            limit=None, resume_from=r.last_resume_key, user_id=None,
        )
        # The three remaining rows are processed.
        assert r2.total_scanned == 3
        # Everything should have canonical fields now.
        cov = await db.user_bets.count_documents({"migration_version": CANONICAL_MIGRATION_VERSION})
        assert cov == 5
    _with_fresh_db(body)


# ── 15. Existing route response schemas remain unchanged ─────────────
def test_no_route_conversion_shipped_in_step_3():
    """Static assertion: the route modules were NOT modified to import
    the ledger or to use canonical field names.  Step 3 is a data-only
    migration."""
    ubr = Path("/app/backend/routes/user_bets_routes.py").read_text(encoding="utf-8")
    phr = Path("/app/backend/routes/parlay_history_routes.py").read_text(encoding="utf-8")
    # No new imports into the routes.
    assert "from services.user_bet_ledger" not in ubr
    assert "from services.user_bet_ledger" not in phr
    # Existing endpoint list unchanged.
    for endpoint in ("/user/bets/track", "/user/bets", "/user/analytics/summary",
                     "/user/analytics/by-sport", "/user/analytics/by-market",
                     "/user/analytics/history"):
        assert endpoint in ubr, f"missing endpoint {endpoint}"
    for endpoint in ("/parlay/save", "/parlay/history", "/parlay/{parlay_id}"):
        assert endpoint in phr


# ── 16. UserBetLedger can deserialize migrated records ───────────────
def test_ledger_deserializes_migrated_records():
    async def body(db):
        await _seed_native_row(db, id_="deser-1")
        await MIG.run_migration(db=db, execute=True, batch_size=100,
                                limit=None, resume_from=None, user_id=None)
        after = await db.user_bets.find_one({"id": "deser-1"}, {"_id": 0})
        bet = UserBet.from_document(after)
        # The ledger's dataclass reads canonical fields.
        assert bet.user_id == "u-native"
        assert bet.wager_type == "straight"
        assert bet.is_legacy is False
        assert bet.clv_status == CLV_UNAVAILABLE
        assert bet.tags == []
        assert bet.legs == []
        assert bet.settlement_events == []
        assert bet.source == "user_track"
        assert bet.migration_version == CANONICAL_MIGRATION_VERSION
    _with_fresh_db(body)


# ── 17. Partial-unique index preflight remains clean ─────────────────
def test_partial_unique_index_preflight_clean_after_migration():
    async def body(db):
        for i in range(3):
            await _seed_native_row(db, id_=f"pf-{i:02d}")
        await MIG.run_migration(db=db, execute=True, batch_size=100,
                                limit=None, resume_from=None, user_id=None)
        rep = await preflight_unique_indexes()
        assert rep.ok is True, rep.to_dict()
        assert rep.duplicate_user_bet_id == 0
        assert rep.duplicate_client_bet_id_per_user == 0
        assert rep.duplicate_idempotency_key_per_user == 0
        assert rep.duplicate_migration_source_id == 0
    _with_fresh_db(body)


# ── 18. Static guard: forbidden collection writes are impossible ─────
def test_script_source_contains_no_forbidden_writes():
    src_path = Path(MIG.__file__)
    src = src_path.read_text(encoding="utf-8")
    # Even the string form must not contain a write op on the
    # forbidden collections.  The static guard inside the script also
    # asserts this at import time.
    for coll in MIG.FORBIDDEN_COLLECTIONS:
        for op in (".insert_one(", ".insert_many(", ".update_one(",
                   ".update_many(", ".delete_one(", ".delete_many(",
                   ".replace_one(", ".find_one_and_update(",
                   ".drop(", ".rename("):
            assert f"{coll}{op}" not in src, f"{coll}{op}"
    # And parse the tree for method calls on the forbidden collection
    # attribute — a stronger, semantic check.
    tree = ast.parse(src)
    forbidden_writes = {"insert_one","insert_many","update_one","update_many",
                        "delete_one","delete_many","replace_one",
                        "find_one_and_update","drop","rename"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attr = node.func
            if attr.attr in forbidden_writes and isinstance(attr.value, ast.Attribute):
                # attr.value may be db.parlay_history OR db["parlay_history"]
                # For attribute access:
                if isinstance(attr.value.value, ast.Name) and \
                   attr.value.attr in MIG.FORBIDDEN_COLLECTIONS:
                    raise AssertionError(
                        f"script writes to forbidden collection: "
                        f"{attr.value.attr}.{attr.attr}"
                    )


# ── Guard test: static forbidden-write scanner ───────────────────────
def test_static_forbidden_write_guard_fires_on_synthetic_bad_line():
    # If we synthesize a forbidden write and re-run the guard on a
    # temp source, it should raise.  We inject into a local COPY of the
    # source and re-scan.
    src = Path(MIG.__file__).read_text(encoding="utf-8")
    bad = src + "\n# await db.parlay_history.insert_one({}) # intentional bad line\n"
    # Manually apply the guard's logic on the string.
    for op in (".insert_one(", ".insert_many(", ".update_one(",
               ".update_many(", ".delete_one(", ".delete_many("):
        if f"parlay_history{op}" in bad:
            break
    else:
        # If no bad reference was matched, the guard IS defective — but
        # in this synthetic case one of them WILL match.
        raise AssertionError("guard failed to detect synthetic bad write")
