"""Phase 3G Step 4 — legacy p_* dry-run backfill tests (test_iter133).

Covers every invariant declared in the Step 4 prompt (19 items):

  1. Dry-run is the default.
  2. --execute is rejected in Step 4.
  3. Dry-run performs zero writes.
  4. plearn_* rows are always excluded.
  5. Rows without user_id are excluded.
  6. Eligible p_* rows use the pure Step 2 mapper.
  7. All expected classification paths are deterministic.
  8. void remains void.
  9. push maps to pushed.
 10. payout=null is not treated as zero.
 11. won + payout=null becomes manual_review (deterministic calc deferred).
 12. Existing migration_source_id match → duplicate_existing (primary).
 13. Low-confidence match → manual_review (never auto duplicate).
 14. Missing migration index is reported.
 15. Conflicting migration index is reported.
 16. No prediction_snapshots are changed.
 17. No settlement_events are changed.
 18. Existing Phase 1–3 tests remain passing (verified externally).
 19. Frontend response schemas unchanged (static check on routes).
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone
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
from scripts.backfills import migrate_parlay_history_p_to_user_bets as MIG


def _with_fresh_db(coro):
    async def wrapper():
        c = AsyncIOMotorClient(os.environ.get("MONGO_URL") or "mongodb://localhost:27017")
        db_name = f"step4_test_{uuid.uuid4().hex[:12]}"
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
    """Create the Step 5 migration_source unique-partial index on the
    test DB so preflight succeeds."""
    spec = next(
        s for s in IR.get_specs_for_collection("user_bets")
        if s.name == MIG.MIGRATION_INDEX_NAME
    )
    await db["user_bets"].create_index(
        list(spec.keys),
        **spec.to_pymongo_kwargs(),
    )


async def _seed_legacy_p_won(db, id_="p_won1"):
    doc = {
        "id": id_,
        "user_id": "u-legacy-1",
        "created_at": "2026-06-21T15:59:47.898347+00:00",
        "mode": "standard",
        "leg_ids": ["a", "b"],
        "legs": [
            {"pick_id": "a", "sport": "Tennis", "event": "X vs Y",
             "market": "X ML", "selection": "X", "book_odds": -200,
             "status": "won"},
            {"pick_id": "b", "sport": "Tennis", "event": "M vs N",
             "market": "M ML", "selection": "M", "book_odds": -150,
             "status": "won"},
        ],
        "combined_odds": 285,
        "stake": 10.0,
        "status": "won",
        "settled_at": "2026-06-21T16:32:17.591862+00:00",
        "payout": 28.5,
    }
    await db["parlay_history"].insert_one(doc)
    return doc


async def _seed_legacy_p_won_missing_payout(db, id_="p_won_null"):
    doc = {
        "id": id_,
        "user_id": "u-legacy-2",
        "created_at": "2026-06-22T00:00:00+00:00",
        "leg_ids": ["a", "b"],
        "legs": [{"pick_id": "a", "book_odds": -200},
                 {"pick_id": "b", "book_odds": -150}],
        "combined_odds": 285,
        "stake": 5.0,
        "status": "won",
        "settled_at": "2026-06-22T02:00:00+00:00",
        "payout": None,   # ← the gap case
    }
    await db["parlay_history"].insert_one(doc)
    return doc


async def _seed_legacy_p_push(db, id_="p_push1"):
    doc = {
        "id": id_,
        "user_id": "u-push",
        "created_at": "2026-06-23T00:00:00+00:00",
        "leg_ids": ["a", "b"],
        "legs": [{"pick_id": "a"}, {"pick_id": "b"}],
        "combined_odds": 200,
        "stake": 1.0,
        "status": "push",
    }
    await db["parlay_history"].insert_one(doc)
    return doc


async def _seed_legacy_p_void(db, id_="p_void1"):
    doc = {
        "id": id_,
        "user_id": "u-void",
        "created_at": "2026-06-24T00:00:00+00:00",
        "leg_ids": ["a", "b"],
        "legs": [{"pick_id": "a"}, {"pick_id": "b"}],
        "combined_odds": 200,
        "stake": 1.0,
        "status": "void",
    }
    await db["parlay_history"].insert_one(doc)
    return doc


async def _seed_plearn(db, id_="plearn_x1"):
    doc = {
        "id": id_,
        "signature": "sig1",
        "leg_count": 2,
        "legs": [{"pick_id": "a"}, {"pick_id": "b"}],
        "status": "pending",
        "shown_at": "2026-06-25T00:00:00+00:00",
    }
    await db["parlay_history"].insert_one(doc)


async def _seed_no_user_row(db, id_="p_nouser"):
    doc = {
        "id": id_,
        "leg_ids": ["a", "b"],
        "legs": [{"pick_id": "a"}, {"pick_id": "b"}],
        "status": "live",
        "combined_odds": 200,
        "stake": 1.0,
        # NO user_id
    }
    await db["parlay_history"].insert_one(doc)


# ── 1. Dry-run is the default ────────────────────────────────────────
def test_dry_run_is_default():
    args = MIG._parse_args([])
    assert args.dry_run is True
    assert args.execute is False


# ── 2. --execute is rejected in Step 4 ───────────────────────────────
def test_execute_flag_is_rejected():
    async def body(db):
        await _seed_index(db)
        raised = False
        try:
            _ = await MIG._amain(["--execute"])
        except MIG.ExecuteRefused as e:
            raised = True
            assert "HARD-DISABLED" in str(e)
        assert raised, "--execute must raise ExecuteRefused"
    _with_fresh_db(body)


# ── 3. Dry-run performs zero writes ──────────────────────────────────
def test_dry_run_performs_zero_writes():
    async def body(db):
        await _seed_index(db)
        await _seed_legacy_p_won(db)
        await _seed_legacy_p_push(db)
        await _seed_legacy_p_void(db)
        await _seed_plearn(db)

        before = await MIG._collection_counts(db)
        report = await MIG.run_dry_run(
            db=db, batch_size=200, limit=None, resume_from=None,
            user_id=None, include_manual_review=True, verbose=False,
        )
        after = await MIG._collection_counts(db)
        assert before == after
        assert report.zero_write_verified is True
        assert report.forbidden_mutations == []
    _with_fresh_db(body)


# ── 4. plearn_* rows are always excluded ─────────────────────────────
def test_plearn_rows_always_excluded():
    async def body(db):
        await _seed_index(db)
        await _seed_plearn(db, id_="plearn_a")
        await _seed_plearn(db, id_="plearn_b")
        report = await MIG.run_dry_run(
            db=db, batch_size=200, limit=None, resume_from=None,
            user_id=None, include_manual_review=True, verbose=False,
        )
        assert report.excluded_plearn == 2
        assert report.eligible_p_star == 0
        assert report.counts_by_classification[MIG.C_EXCLUDED_LEARNING] == 2
        # No plearn row appears in the classifications list.
        for rc in report.classifications:
            assert not rc["legacy_id"].startswith("plearn_")
    _with_fresh_db(body)


# ── 5. Rows without user_id are excluded ─────────────────────────────
def test_rows_without_user_id_excluded():
    async def body(db):
        await _seed_index(db)
        await _seed_no_user_row(db)
        report = await MIG.run_dry_run(
            db=db, batch_size=200, limit=None, resume_from=None,
            user_id=None, include_manual_review=True, verbose=False,
        )
        assert report.counts_by_classification[MIG.C_EXCLUDED_MISSING_USER] == 1
    _with_fresh_db(body)


# ── 6. Eligible p_* rows use the pure Step 2 mapper (contract check) ──
def test_uses_pure_step2_mapper_symbol():
    # Static: the analyse_row function imports and calls
    # UBL.map_legacy_user_parlay.  Direct source assertion.
    src = Path(MIG.__file__).read_text(encoding="utf-8")
    assert "UBL.map_legacy_user_parlay" in src, \
        "Step 4 script must call the pure Step 2 mapper by name"


# ── 7. Deterministic classification paths ────────────────────────────
def test_all_classifications_are_deterministic():
    async def body(db):
        await _seed_index(db)
        await _seed_legacy_p_won(db, id_="p_det1")
        await _seed_legacy_p_push(db, id_="p_det2")
        # Run twice; classifications must be identical.
        r1 = await MIG.run_dry_run(db=db, batch_size=200, limit=None,
                                    resume_from=None, user_id=None,
                                    include_manual_review=True, verbose=False)
        r2 = await MIG.run_dry_run(db=db, batch_size=200, limit=None,
                                    resume_from=None, user_id=None,
                                    include_manual_review=True, verbose=False)
        assert r1.counts_by_classification == r2.counts_by_classification
        # Sort by legacy_id for stable compare.
        def key(x): return x["legacy_id"]
        c1 = sorted([{"legacy_id": c["legacy_id"], "classification": c["classification"]}
                     for c in r1.classifications], key=key)
        c2 = sorted([{"legacy_id": c["legacy_id"], "classification": c["classification"]}
                     for c in r2.classifications], key=key)
        assert c1 == c2
    _with_fresh_db(body)


# ── 8. void remains void ─────────────────────────────────────────────
def test_void_remains_void_in_dry_run():
    async def body(db):
        await _seed_index(db)
        await _seed_legacy_p_void(db)
        report = await MIG.run_dry_run(db=db, batch_size=200, limit=None,
                                        resume_from=None, user_id=None,
                                        include_manual_review=True, verbose=False)
        rc = report.classifications[0]
        assert rc["original_status"] == "void"
        assert rc["canonical_status"] == UBL.STATUS_VOID
    _with_fresh_db(body)


# ── 9. push maps to pushed ───────────────────────────────────────────
def test_push_maps_to_pushed_in_dry_run():
    async def body(db):
        await _seed_index(db)
        await _seed_legacy_p_push(db)
        report = await MIG.run_dry_run(db=db, batch_size=200, limit=None,
                                        resume_from=None, user_id=None,
                                        include_manual_review=True, verbose=False)
        rc = report.classifications[0]
        assert rc["original_status"] == "push"
        assert rc["canonical_status"] == UBL.STATUS_PUSHED
        # And explicitly NOT void.
        assert rc["canonical_status"] != UBL.STATUS_VOID
    _with_fresh_db(body)


# ── 10 + 11. payout=null on won → manual_review ──────────────────────
def test_won_payout_null_becomes_manual_review():
    async def body(db):
        await _seed_index(db)
        await _seed_legacy_p_won_missing_payout(db)
        report = await MIG.run_dry_run(db=db, batch_size=200, limit=None,
                                        resume_from=None, user_id=None,
                                        include_manual_review=True, verbose=False)
        rc = report.classifications[0]
        assert rc["classification"] == MIG.C_MANUAL_REVIEW
        # payout not treated as zero — payout_coverage remains False.
        assert rc["payout_coverage"] is False
        # The Step 2 mapper *will* compute a profit_loss via the
        # American-odds formula, but Step 4 policy is still
        # manual_review until the operator approves that fallback.
        # The warning explains this — the row is not migration_ready.
        assert any("American-odds formula" in w for w in rc["warnings"])
    _with_fresh_db(body)


def test_payout_null_is_never_treated_as_zero():
    async def body(db):
        await _seed_index(db)
        await _seed_legacy_p_won_missing_payout(db, id_="p_pn1")
        report = await MIG.run_dry_run(db=db, batch_size=200, limit=None,
                                        resume_from=None, user_id=None,
                                        include_manual_review=True, verbose=False)
        assert report.payout_gaps["won_missing_payout"] == 1
    _with_fresh_db(body)


# ── 12. duplicate_existing via primary index ─────────────────────────
def test_primary_migration_source_id_match_becomes_duplicate_existing():
    async def body(db):
        await _seed_index(db)
        await _seed_legacy_p_won(db, id_="p_dup1")
        # Pre-seed the target with a canonical row already carrying
        # migration_source_id=p_dup1.
        await db["user_bets"].insert_one({
            "id":                  "existing-1",
            "user_bet_id":         "existing-1",
            "user_id":             "u-legacy-1",
            "wager_type":          UBL.WAGER_TYPE_PARLAY,
            "migration_source":    "parlay_history",
            "migration_source_id": "p_dup1",
        })
        report = await MIG.run_dry_run(db=db, batch_size=200, limit=None,
                                        resume_from=None, user_id=None,
                                        include_manual_review=True, verbose=False)
        rc = next(c for c in report.classifications if c["legacy_id"] == "p_dup1")
        assert rc["classification"] == MIG.C_DUPLICATE_EXISTING
        assert rc["duplicate_match"] == "primary"
    _with_fresh_db(body)


# ── 13. Low-confidence match becomes manual_review ───────────────────
def test_low_confidence_secondary_match_not_auto_duplicate():
    async def body(db):
        await _seed_index(db)
        # Seed the legacy row.
        await _seed_legacy_p_won(db, id_="p_lc1")
        # Insert a user_bets row that shares ONLY the total-odds signal —
        # different user, no matching leg_ids, no placed_at.  Must NOT
        # be classified as a duplicate.  (This tests the guardrail
        # against "same-total-odds" false-positive duping.)
        await db["user_bets"].insert_one({
            "id":            "unrelated-1",
            "user_bet_id":   "unrelated-1",
            "user_id":       "someone-else",
            "wager_type":    UBL.WAGER_TYPE_PARLAY,
            "combined_odds": 285,        # same total as p_lc1
            "parlay_legs":   ["x", "y"], # different legs
        })
        report = await MIG.run_dry_run(db=db, batch_size=200, limit=None,
                                        resume_from=None, user_id=None,
                                        include_manual_review=True, verbose=False)
        rc = next(c for c in report.classifications if c["legacy_id"] == "p_lc1")
        # Cross-user total-odds collision must NOT flip to duplicate.
        assert rc["classification"] != MIG.C_DUPLICATE_EXISTING
        assert rc["duplicate_match"] is None
    _with_fresh_db(body)


# ── 14. Missing migration index is reported ──────────────────────────
def test_missing_migration_index_reported():
    async def body(db):
        # Do NOT create the index — leave it missing on the fresh DB.
        report = await MIG.run_dry_run(db=db, batch_size=200, limit=None,
                                        resume_from=None, user_id=None,
                                        include_manual_review=True, verbose=False)
        assert report.index_preflight["ok"] is False
        assert report.index_preflight["present"] is False
        assert "missing" in (report.index_preflight["conflict_note"] or "")
        # And a future --execute would be blocked (the field on the
        # DryRunReport documents that Step 4 blocks execution).
        assert report.production_execute_blocked is True
    _with_fresh_db(body)


# ── 15. Conflicting migration index is reported ──────────────────────
def test_conflicting_migration_index_reported():
    async def body(db):
        # Create an index with the SAME name but WRONG keys.
        await db["user_bets"].create_index(
            [("migration_source", 1)],
            name=MIG.MIGRATION_INDEX_NAME,
            unique=True,
        )
        report = await MIG.run_dry_run(db=db, batch_size=200, limit=None,
                                        resume_from=None, user_id=None,
                                        include_manual_review=True, verbose=False)
        assert report.index_preflight["ok"] is False
        assert report.index_preflight["present"] is True
        assert "keys mismatch" in (report.index_preflight["conflict_note"] or "") \
            or "partial filter" in (report.index_preflight["conflict_note"] or "")
    _with_fresh_db(body)


# ── 16. No prediction_snapshots are changed ──────────────────────────
def test_prediction_snapshots_never_changed():
    async def body(db):
        await _seed_index(db)
        await _seed_legacy_p_won(db)
        await db["prediction_snapshots"].insert_one(
            {"prediction_id": "p1", "snapshot_version": 1}
        )
        before = await db["prediction_snapshots"].count_documents({})
        _ = await MIG.run_dry_run(db=db, batch_size=200, limit=None,
                                   resume_from=None, user_id=None,
                                   include_manual_review=True, verbose=False)
        after = await db["prediction_snapshots"].count_documents({})
        assert before == after
    _with_fresh_db(body)


# ── 17. No settlement_events are changed ─────────────────────────────
def test_settlement_events_never_changed():
    async def body(db):
        await _seed_index(db)
        await _seed_legacy_p_won(db)
        await db["settlement_events"].insert_one(
            {"prediction_id": "p1", "result": "won", "source": "test"}
        )
        before = await db["settlement_events"].count_documents({})
        _ = await MIG.run_dry_run(db=db, batch_size=200, limit=None,
                                   resume_from=None, user_id=None,
                                   include_manual_review=True, verbose=False)
        after = await db["settlement_events"].count_documents({})
        assert before == after
    _with_fresh_db(body)


# ── 19. Frontend response schemas unchanged (static route check) ─────
def test_no_route_conversion_shipped_in_step_4():
    ubr = Path("/app/backend/routes/user_bets_routes.py").read_text(encoding="utf-8")
    phr = Path("/app/backend/routes/parlay_history_routes.py").read_text(encoding="utf-8")
    assert "from services.user_bet_ledger" not in ubr
    assert "from services.user_bet_ledger" not in phr
    for ep in ("/user/bets/track", "/user/bets", "/user/analytics/summary",
               "/parlay/save", "/parlay/history"):
        assert ep in (ubr + phr), f"missing endpoint {ep}"


# ── Extra: full multi-row scenario ───────────────────────────────────
def test_full_multi_row_scenario_matches_expected_classifications():
    async def body(db):
        await _seed_index(db)
        # 1 migration_ready
        await _seed_legacy_p_won(db, id_="p_mig1")
        # 1 manual_review (won + payout null)
        await _seed_legacy_p_won_missing_payout(db, id_="p_mr1")
        # 1 migration_ready (push)
        await _seed_legacy_p_push(db, id_="p_mig2")
        # 1 excluded_learning
        await _seed_plearn(db)
        # 1 excluded_missing_user
        await _seed_no_user_row(db)
        # 1 duplicate_existing (primary)
        await _seed_legacy_p_void(db, id_="p_dup2")
        await db["user_bets"].insert_one({
            "id": "existing-2", "user_bet_id": "existing-2",
            "user_id": "u-void", "wager_type": UBL.WAGER_TYPE_PARLAY,
            "migration_source": "parlay_history", "migration_source_id": "p_dup2",
        })

        r = await MIG.run_dry_run(db=db, batch_size=200, limit=None,
                                    resume_from=None, user_id=None,
                                    include_manual_review=True, verbose=False)

        # 2 migration_ready, 1 manual_review, 1 duplicate_existing,
        # 1 excluded_learning, 1 excluded_missing_user.
        assert r.counts_by_classification[MIG.C_MIGRATION_READY] == 2
        assert r.counts_by_classification[MIG.C_MANUAL_REVIEW] == 1
        assert r.counts_by_classification[MIG.C_DUPLICATE_EXISTING] == 1
        assert r.counts_by_classification[MIG.C_EXCLUDED_LEARNING] == 1
        assert r.counts_by_classification[MIG.C_EXCLUDED_MISSING_USER] == 1
        # eligible = 2 (ready) + 1 (manual) + 1 (dup) = 4
        assert r.eligible_p_star == 4
        assert r.excluded_plearn == 1
        # zero-write invariant intact
        assert r.zero_write_verified is True
    _with_fresh_db(body)


def test_report_generator_writes_json_when_requested():
    async def body(db):
        await _seed_index(db)
        await _seed_legacy_p_won(db)
        out = "/tmp/step4_test_report.json"
        try:
            _ = await MIG._amain([
                "--dry-run", "--report-path", out, "--verbose",
            ])
            assert Path(out).exists()
            data = Path(out).read_text(encoding="utf-8")
            assert '"eligible_p_star"' in data
        finally:
            Path(out).unlink(missing_ok=True)
    _with_fresh_db(body)
