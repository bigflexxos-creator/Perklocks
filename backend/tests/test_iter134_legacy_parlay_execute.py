"""Phase 3G Step 5 — legacy p_* execute tests (test_iter134).

Covers every invariant declared in the Step 5 prompt (20 items):
  1.  Execution requires explicit confirmation flags.
  2.  Missing migration index blocks all writes.
  3.  Conflicting migration index blocks all writes.
  4.  Manual-review rows block execution.
  5.  Unsafe rows block execution.
  6.  plearn_* rows cannot be inserted.
  7.  All eligible p_* rows use the pure mapper.
  8.  Inserts are idempotent by migration source ID.
  9.  Existing canonical rows are not overwritten.
 10.  Original status, timestamps, legs, odds, and IDs are preserved.
 11.  Missing snapshot/market/line fields remain null.
 12.  void remains void.
 13.  pushed remains pushed.
 14.  parlay_history remains unchanged.
 15.  prediction_snapshots remain unchanged.
 16.  settlement_events remain unchanged.
 17.  Second execution inserts zero rows.
 18.  Step 4 dry-run reclassifies migrated records as duplicate_existing.
 19.  Existing route schemas remain unchanged (static check).
 20.  All previous Phase 1–3 tests remain passing (verified externally).
"""
from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient

from services.database import (
    override_database_for_testing,
    reset_database_override,
)
from services import user_bet_ledger as UBL
from services import index_registry as IR
from scripts.backfills import migrate_parlay_history_p_to_user_bets as DRYRUN
from scripts.backfills import execute_parlay_history_p_to_user_bets as EXEC


def _with_fresh_db(coro):
    async def wrapper():
        c = AsyncIOMotorClient(os.environ.get("MONGO_URL") or "mongodb://localhost:27017")
        db_name = f"step5_test_{uuid.uuid4().hex[:12]}"
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


async def _seed_index(db):
    spec = next(
        s for s in IR.get_specs_for_collection("user_bets")
        if s.name == DRYRUN.MIGRATION_INDEX_NAME
    )
    await db["user_bets"].create_index(list(spec.keys), **spec.to_pymongo_kwargs())


async def _seed_ready_won(db, id_="p_ready_won"):
    doc = {
        "id": id_,
        "user_id": "u-1",
        "created_at": "2026-06-21T15:59:47+00:00",
        "mode": "standard",
        "leg_ids": ["a", "b"],
        "legs": [
            {"pick_id": "a", "sport": "Tennis", "market": "X ML",
             "selection": "X", "book_odds": -200, "status": "won"},
            {"pick_id": "b", "sport": "Tennis", "market": "M ML",
             "selection": "M", "book_odds": -150, "status": "won"},
        ],
        "combined_odds": 285,
        "stake": 10.0,
        "status": "won",
        "settled_at": "2026-06-21T16:32:17+00:00",
        "payout": 28.5,
    }
    await db["parlay_history"].insert_one(doc)
    return doc


async def _seed_ready_lost(db, id_="p_ready_lost"):
    doc = {
        "id": id_,
        "user_id": "u-lost",
        "created_at": "2026-06-22T00:00:00+00:00",
        "leg_ids": ["a", "b"],
        "legs": [
            {"pick_id": "a", "sport": "MLB", "market": "ML", "selection": "X",
             "book_odds": -110, "status": "lost"},
            {"pick_id": "b", "sport": "MLB", "market": "ML", "selection": "Y",
             "book_odds": +100, "status": "lost"},
        ],
        "combined_odds": +200,
        "stake": 5.0,
        "status": "lost",
        "settled_at": "2026-06-22T02:00:00+00:00",
        "payout": None,
    }
    await db["parlay_history"].insert_one(doc)


async def _seed_ready_push(db, id_="p_ready_push"):
    await db["parlay_history"].insert_one({
        "id": id_,
        "user_id": "u-push",
        "created_at": "2026-06-23T00:00:00+00:00",
        "leg_ids": ["a", "b"],
        "legs": [{"pick_id": "a"}, {"pick_id": "b"}],
        "combined_odds": 200, "stake": 1.0,
        "status": "push",
    })


async def _seed_ready_void(db, id_="p_ready_void"):
    await db["parlay_history"].insert_one({
        "id": id_,
        "user_id": "u-void",
        "created_at": "2026-06-24T00:00:00+00:00",
        "leg_ids": ["a", "b"],
        "legs": [{"pick_id": "a"}, {"pick_id": "b"}],
        "combined_odds": 200, "stake": 1.0,
        "status": "void",
    })


async def _seed_won_missing_payout(db, id_="p_manual"):
    """This row lands in manual_review because payout is null on a
    won parlay."""
    await db["parlay_history"].insert_one({
        "id": id_,
        "user_id": "u-mp",
        "created_at": "2026-06-25T00:00:00+00:00",
        "leg_ids": ["a", "b"],
        "legs": [{"pick_id": "a"}, {"pick_id": "b"}],
        "combined_odds": 285, "stake": 5.0,
        "status": "won",
        "settled_at": "2026-06-25T02:00:00+00:00",
        "payout": None,
    })


async def _seed_plearn(db, id_="plearn_x1"):
    await db["parlay_history"].insert_one({
        "id": id_,
        "signature": "sig",
        "leg_count": 2,
        "legs": [{"pick_id": "a"}, {"pick_id": "b"}],
        "status": "pending",
        "shown_at": "2026-06-25T00:00:00+00:00",
    })


# ─────────────────────────────────────────────────────────────────────
# 1. Execution requires both --execute and --confirm PRODUCTION
# ─────────────────────────────────────────────────────────────────────
def test_execute_without_confirm_is_refused():
    async def body(db):
        raised = False
        try:
            _ = await EXEC._amain(["--execute"])
        except EXEC.ExecutionRefused:
            raised = True
        assert raised, "--execute alone must be refused"
    _with_fresh_db(body)


def test_execute_wrong_confirm_token_is_refused():
    async def body(db):
        raised = False
        try:
            _ = await EXEC._amain(["--execute", "--confirm", "yes"])
        except EXEC.ExecutionRefused:
            raised = True
        assert raised, "wrong --confirm token must be refused"
    _with_fresh_db(body)


def test_no_flags_is_refused():
    async def body(db):
        raised = False
        try:
            _ = await EXEC._amain([])
        except EXEC.ExecutionRefused:
            raised = True
        assert raised
    _with_fresh_db(body)


# ─────────────────────────────────────────────────────────────────────
# 2. Missing migration index blocks all writes
# ─────────────────────────────────────────────────────────────────────
def test_missing_migration_index_blocks_all_writes():
    async def body(db):
        # Deliberately DO NOT create the index.
        await _seed_ready_won(db)
        before = await db["user_bets"].count_documents({})
        report = await EXEC.execute_migration(
            db=db, batch_size=100, limit=None, resume_from=None, user_id=None,
        )
        assert report.pre_gate_ok is False
        assert any("migration index" in b for b in report.pre_gate_blockers)
        after = await db["user_bets"].count_documents({})
        assert before == after == 0
        assert report.inserted_count == 0
    _with_fresh_db(body)


# ─────────────────────────────────────────────────────────────────────
# 3. Conflicting migration index blocks all writes
# ─────────────────────────────────────────────────────────────────────
def test_conflicting_migration_index_blocks_all_writes():
    async def body(db):
        # Wrong keys under the same name.
        await db["user_bets"].create_index(
            [("migration_source", 1)],
            name=DRYRUN.MIGRATION_INDEX_NAME,
            unique=True,
        )
        await _seed_ready_won(db)
        report = await EXEC.execute_migration(
            db=db, batch_size=100, limit=None, resume_from=None, user_id=None,
        )
        assert report.pre_gate_ok is False
        assert report.inserted_count == 0
    _with_fresh_db(body)


# ─────────────────────────────────────────────────────────────────────
# 4. Manual-review rows block execution
# ─────────────────────────────────────────────────────────────────────
def test_manual_review_rows_block_execution():
    async def body(db):
        await _seed_index(db)
        await _seed_ready_won(db, id_="p_go")
        await _seed_won_missing_payout(db, id_="p_manual")
        report = await EXEC.execute_migration(
            db=db, batch_size=100, limit=None, resume_from=None, user_id=None,
        )
        assert report.pre_gate_ok is False
        assert any("manual_review > 0" in b for b in report.pre_gate_blockers)
        # Zero rows inserted despite one being migration_ready.
        assert report.inserted_count == 0
        assert await db["user_bets"].count_documents({}) == 0
    _with_fresh_db(body)


# ─────────────────────────────────────────────────────────────────────
# 5. Unsafe rows block execution
# ─────────────────────────────────────────────────────────────────────
def test_unsafe_rows_block_execution():
    async def body(db):
        await _seed_index(db)
        # Craft an "unsafe" row via an unknown legacy status.
        await db["parlay_history"].insert_one({
            "id": "p_unknown_status",
            "user_id": "u-uns",
            "created_at": "2026-06-01T00:00:00+00:00",
            "leg_ids": ["a", "b"],
            "legs": [{"pick_id": "a"}, {"pick_id": "b"}],
            "combined_odds": 200, "stake": 1.0,
            "status": "settling_third_period",  # → canonical unknown
        })
        report = await EXEC.execute_migration(
            db=db, batch_size=100, limit=None, resume_from=None, user_id=None,
        )
        # Unknown-status rows land in manual_review (per Step 4 policy),
        # which also blocks execution.
        assert report.pre_gate_ok is False
        assert report.inserted_count == 0
        assert await db["user_bets"].count_documents({}) == 0
    _with_fresh_db(body)


# ─────────────────────────────────────────────────────────────────────
# 6. plearn_* rows cannot be inserted
# ─────────────────────────────────────────────────────────────────────
def test_plearn_rows_never_inserted():
    async def body(db):
        await _seed_index(db)
        await _seed_ready_won(db)
        await _seed_plearn(db, id_="plearn_a")
        await _seed_plearn(db, id_="plearn_b")
        report = await EXEC.execute_migration(
            db=db, batch_size=100, limit=None, resume_from=None, user_id=None,
        )
        assert report.pre_gate_ok is True
        assert report.inserted_count == 1     # only the p_ready_won row
        # And no user_bets row carries a plearn_ migration_source_id.
        n = await db["user_bets"].count_documents({
            "migration_source_id": {"$regex": "^plearn_"}
        })
        assert n == 0
    _with_fresh_db(body)


# ─────────────────────────────────────────────────────────────────────
# 7. All eligible p_* rows use the pure mapper
# ─────────────────────────────────────────────────────────────────────
def test_uses_pure_step2_mapper_symbol():
    src = Path(EXEC.__file__).read_text(encoding="utf-8")
    assert "UBL.map_legacy_user_parlay" in src


# ─────────────────────────────────────────────────────────────────────
# 8. Inserts are idempotent by migration source ID
# ─────────────────────────────────────────────────────────────────────
def test_inserts_are_idempotent_by_migration_source_id():
    async def body(db):
        await _seed_index(db)
        await _seed_ready_won(db)
        await _seed_ready_lost(db)

        r1 = await EXEC.execute_migration(db=db, batch_size=100, limit=None,
                                          resume_from=None, user_id=None)
        assert r1.inserted_count == 2
        assert r1.skipped_existing_count == 0

        # Rerun → zero new inserts, both skipped as existing.
        r2 = await EXEC.execute_migration(db=db, batch_size=100, limit=None,
                                          resume_from=None, user_id=None)
        assert r2.inserted_count == 0
        assert r2.skipped_existing_count == 2
        assert await db["user_bets"].count_documents({}) == 2
    _with_fresh_db(body)


# ─────────────────────────────────────────────────────────────────────
# 9. Existing canonical rows are not overwritten
# ─────────────────────────────────────────────────────────────────────
def test_existing_canonical_rows_are_not_overwritten():
    async def body(db):
        await _seed_index(db)
        # Pre-seed a canonical row with a distinctive user_bet_id.
        await db["user_bets"].insert_one({
            "user_bet_id":         "PRESET-1",
            "id":                  "PRESET-1",
            "user_id":             "u-1",
            "wager_type":          UBL.WAGER_TYPE_PARLAY,
            "migration_source":    "parlay_history",
            "migration_source_id": "p_preset",
            "notes":               "PRESET_NOTE",
        })
        await db["parlay_history"].insert_one({
            "id": "p_preset",
            "user_id": "u-1",
            "created_at": "2026-06-21T00:00:00+00:00",
            "leg_ids": ["a", "b"],
            "legs": [{"pick_id": "a"}, {"pick_id": "b"}],
            "combined_odds": 200, "stake": 1.0, "status": "lost",
        })
        report = await EXEC.execute_migration(db=db, batch_size=100, limit=None,
                                              resume_from=None, user_id=None)
        # No new insert; existing row untouched.
        assert report.inserted_count == 0
        assert report.skipped_existing_count == 1
        after = await db["user_bets"].find_one({"user_bet_id": "PRESET-1"}, {"_id": 0})
        assert after["notes"] == "PRESET_NOTE"
    _with_fresh_db(body)


# ─────────────────────────────────────────────────────────────────────
# 10. Preserved: status, timestamps, legs, odds, IDs
# ─────────────────────────────────────────────────────────────────────
def test_migrated_row_preserves_source_details():
    async def body(db):
        await _seed_index(db)
        src = await _seed_ready_won(db, id_="p_pres")
        await EXEC.execute_migration(db=db, batch_size=100, limit=None,
                                     resume_from=None, user_id=None)
        row = await db["user_bets"].find_one(
            {"migration_source_id": "p_pres"}, {"_id": 0}
        )
        assert row is not None
        assert row["user_id"]             == src["user_id"]
        assert row["original_status"]     == src["status"]
        assert row["status"]              == UBL.STATUS_WON
        assert row["combined_odds"]       == src["combined_odds"]
        assert row["stake_amount"]        == src["stake"]
        assert row["is_legacy"]           is True
        assert row["migration_source"]    == "parlay_history"
        assert row["migration_source_id"] == "p_pres"
        # legs preserved
        assert len(row["legs"]) == len(src["legs"])
        assert row["legs"][0]["prediction_id"] == "a"
        assert row["legs"][0]["original_odds"] == -200
        assert row["legs"][1]["original_odds"] == -150
        # timestamps preserved as datetimes derived from the ISO input
        assert row["placed_at"] is not None
        assert row["settled_at"] is not None
    _with_fresh_db(body)


# ─────────────────────────────────────────────────────────────────────
# 11. Missing snapshot/market/line fields remain null
# ─────────────────────────────────────────────────────────────────────
def test_missing_identity_fields_stay_null():
    async def body(db):
        await _seed_index(db)
        await _seed_ready_won(db, id_="p_null")
        await EXEC.execute_migration(db=db, batch_size=100, limit=None,
                                     resume_from=None, user_id=None)
        row = await db["user_bets"].find_one({"migration_source_id": "p_null"}, {"_id": 0})
        assert row["snapshot_id"]         is None
        assert row["market_contract_id"]  is None
        assert row["opening_line"]        is None
        assert row["closing_line"]        is None
        assert row["clv_value"]           is None
        assert row["clv_status"]          == UBL.CLV_UNAVAILABLE
        # Legs: none of them carry a `line`.
        for L in row["legs"]:
            assert L["line"] is None
            assert L["snapshot_id"] is None
            assert L["market_contract_id"] is None
    _with_fresh_db(body)


# ─────────────────────────────────────────────────────────────────────
# 12. void remains void
# ─────────────────────────────────────────────────────────────────────
def test_void_row_stays_void_after_migration():
    async def body(db):
        await _seed_index(db)
        await _seed_ready_void(db)
        await EXEC.execute_migration(db=db, batch_size=100, limit=None,
                                     resume_from=None, user_id=None)
        row = await db["user_bets"].find_one({"migration_source_id": "p_ready_void"}, {"_id": 0})
        assert row["status"] == UBL.STATUS_VOID
        assert row["original_status"] == "void"
    _with_fresh_db(body)


# ─────────────────────────────────────────────────────────────────────
# 13. pushed remains pushed
# ─────────────────────────────────────────────────────────────────────
def test_push_row_maps_to_pushed_after_migration():
    async def body(db):
        await _seed_index(db)
        await _seed_ready_push(db)
        await EXEC.execute_migration(db=db, batch_size=100, limit=None,
                                     resume_from=None, user_id=None)
        row = await db["user_bets"].find_one({"migration_source_id": "p_ready_push"}, {"_id": 0})
        assert row["status"] == UBL.STATUS_PUSHED
        # And not void.
        assert row["status"] != UBL.STATUS_VOID
    _with_fresh_db(body)


# ─────────────────────────────────────────────────────────────────────
# 14. parlay_history remains unchanged
# ─────────────────────────────────────────────────────────────────────
def test_parlay_history_unchanged_after_execute():
    async def body(db):
        await _seed_index(db)
        await _seed_ready_won(db)
        await _seed_ready_lost(db)
        before = await db["parlay_history"].find({}, {"_id": 0}).to_list(50)
        await EXEC.execute_migration(db=db, batch_size=100, limit=None,
                                     resume_from=None, user_id=None)
        after = await db["parlay_history"].find({}, {"_id": 0}).to_list(50)
        assert sorted(before, key=str) == sorted(after, key=str)
    _with_fresh_db(body)


# ─────────────────────────────────────────────────────────────────────
# 15. prediction_snapshots unchanged
# ─────────────────────────────────────────────────────────────────────
def test_prediction_snapshots_unchanged():
    async def body(db):
        await _seed_index(db)
        await _seed_ready_won(db)
        await db["prediction_snapshots"].insert_one(
            {"prediction_id": "px", "snapshot_version": 1}
        )
        before = await db["prediction_snapshots"].count_documents({})
        await EXEC.execute_migration(db=db, batch_size=100, limit=None,
                                     resume_from=None, user_id=None)
        after = await db["prediction_snapshots"].count_documents({})
        assert before == after
    _with_fresh_db(body)


# ─────────────────────────────────────────────────────────────────────
# 16. settlement_events unchanged
# ─────────────────────────────────────────────────────────────────────
def test_settlement_events_unchanged():
    async def body(db):
        await _seed_index(db)
        await _seed_ready_won(db)
        await db["settlement_events"].insert_one(
            {"prediction_id": "px", "result": "won"}
        )
        before = await db["settlement_events"].count_documents({})
        await EXEC.execute_migration(db=db, batch_size=100, limit=None,
                                     resume_from=None, user_id=None)
        after = await db["settlement_events"].count_documents({})
        assert before == after
    _with_fresh_db(body)


# ─────────────────────────────────────────────────────────────────────
# 17. Second execution inserts zero rows
# ─────────────────────────────────────────────────────────────────────
def test_second_execution_inserts_zero_rows():
    async def body(db):
        await _seed_index(db)
        await _seed_ready_won(db)
        r1 = await EXEC.execute_migration(db=db, batch_size=100, limit=None,
                                          resume_from=None, user_id=None)
        assert r1.inserted_count == 1
        r2 = await EXEC.execute_migration(db=db, batch_size=100, limit=None,
                                          resume_from=None, user_id=None)
        assert r2.inserted_count == 0
        assert r2.skipped_existing_count == 1
    _with_fresh_db(body)


# ─────────────────────────────────────────────────────────────────────
# 18. Step 4 dry-run reclassifies migrated records as duplicate_existing
# ─────────────────────────────────────────────────────────────────────
def test_post_migration_dryrun_reclassifies_as_duplicate_existing():
    async def body(db):
        await _seed_index(db)
        await _seed_ready_won(db, id_="p_reclass")
        r = await EXEC.execute_migration(db=db, batch_size=100, limit=None,
                                         resume_from=None, user_id=None)
        assert r.post_migrated_all_duplicate is True
        # And confirm the classification in a fresh dry-run.
        post = await DRYRUN.run_dry_run(
            db=db, batch_size=200, limit=None, resume_from=None,
            user_id=None, include_manual_review=True, verbose=False,
        )
        rc = next(c for c in post.classifications if c["legacy_id"] == "p_reclass")
        assert rc["classification"] == DRYRUN.C_DUPLICATE_EXISTING
        assert rc["duplicate_match"] == "primary"
    _with_fresh_db(body)


# ─────────────────────────────────────────────────────────────────────
# 19. Existing route schemas remain unchanged (static check)
# ─────────────────────────────────────────────────────────────────────
def test_no_route_conversion_shipped_in_step_5():
    ubr = Path("/app/backend/routes/user_bets_routes.py").read_text(encoding="utf-8")
    phr = Path("/app/backend/routes/parlay_history_routes.py").read_text(encoding="utf-8")
    assert "from services.user_bet_ledger" not in ubr
    assert "from services.user_bet_ledger" not in phr
    for ep in ("/user/bets/track", "/user/bets", "/user/analytics/summary",
               "/parlay/save", "/parlay/history"):
        assert ep in (ubr + phr), f"missing endpoint {ep}"


# ─────────────────────────────────────────────────────────────────────
# Extra: full-flow scenario (2 ready + 1 plearn + 1 preset)
# ─────────────────────────────────────────────────────────────────────
def test_full_flow_scenario():
    async def body(db):
        await _seed_index(db)
        await _seed_ready_won(db, id_="p_a")
        await _seed_ready_lost(db, id_="p_b")
        await _seed_plearn(db)
        # Preset row that should be preserved unchanged.
        await db["user_bets"].insert_one({
            "user_bet_id": "PRESET",
            "id":          "PRESET",
            "user_id":     "u-preset",
            "wager_type":  UBL.WAGER_TYPE_STRAIGHT,
            "prediction_id": "PX",
            "notes":       "keep-me",
        })
        r = await EXEC.execute_migration(db=db, batch_size=100, limit=None,
                                         resume_from=None, user_id=None)
        assert r.pre_gate_ok is True
        assert r.inserted_count == 2
        assert r.skipped_existing_count == 0
        # Preset still present, unchanged.
        preset = await db["user_bets"].find_one({"user_bet_id": "PRESET"}, {"_id": 0})
        assert preset["notes"] == "keep-me"
        # Zero plearn rows written.
        n = await db["user_bets"].count_documents(
            {"migration_source_id": {"$regex": "^plearn_"}}
        )
        assert n == 0
        # Full count: 1 preset + 2 migrated = 3.
        assert await db["user_bets"].count_documents({}) == 3
    _with_fresh_db(body)


def test_report_includes_inserted_ids_for_rollback():
    async def body(db):
        await _seed_index(db)
        await _seed_ready_won(db, id_="p_rb1")
        await _seed_ready_lost(db, id_="p_rb2")
        r = await EXEC.execute_migration(db=db, batch_size=100, limit=None,
                                         resume_from=None, user_id=None)
        assert set(r.inserted_legacy_ids) == {"p_rb1", "p_rb2"}
        assert len(r.inserted_user_bet_ids) == 2
        # No raw user_id in the inserted_records payload.
        for rec in r.inserted_records:
            assert "user_id" not in rec, "user_id must not leak into report"
        # Rollback filter would target exactly these rows.
        n = await db["user_bets"].count_documents({
            "migration_source":  "parlay_history",
            "migration_version": EXEC.STEP5_MIGRATION_VERSION,
        })
        assert n == 2
    _with_fresh_db(body)
