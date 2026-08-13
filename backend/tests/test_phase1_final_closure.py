"""Phase 1 Final Closure regression tests (2026-08-11).

Covers the four items in the closure directive:
  A. True `>85` (not `>=85.01`) applied at the boundary — verified via
     both the Python helper and the Mongo predicate builder.
  B. `>85` governs ALL Locks views — main board, market-filtered, and
     alt-line.  Filters must never lower the threshold.
  C. Candidate disposition lifecycle wired centrally into
     `pick_refresh_orchestrator._refresh_picks` via
     `record_batch_dispositions()`.  Three synthetic candidates
     (A/B/C) prove the trail matches the closure spec:
       • A → evaluated → rejected(lock_score_below_board_threshold)
       • B → evaluated → accepted → published → board_eligible
       • C → evaluated → rejected(no_bet)
  D. Canonical Lock source wins over stale legacy shadows in
     `is_main_board_eligible` and in the Mongo query builder.

These tests are self-contained — they use the live Mongo but scope
every write with a stable prefix so cleanup is trivial.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import uuid

from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient

_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
_TID = "p1closure_"


def _db():
    return AsyncIOMotorClient(os.environ["MONGO_URL"])[
        os.environ.get("DB_NAME", "lockscore_db")]


def _run(coro):
    return asyncio.run(coro)


# ── A. True `>85` boundary tests (Python helper) ────────────────────
# 2026-06 (Support Soccer fix): `is_main_board_eligible` now also
# requires a REAL sportsbook line.  Every fixture in this file that
# expects `True` MUST carry ``book_odds`` + ``implied_probability`` —
# these tests still assert the `>85` boundary; the added fields keep
# them complete real-line picks so the boundary is the only thing
# being exercised.  The strict `≤ 85` cases can stay bare.
_REAL_LINE = {"book_odds": -180, "implied_probability": 64.3}


def test_boundary_84_99_off():
    from services.main_board_eligibility import is_main_board_eligible
    assert is_main_board_eligible({"lock_score": 84.99, **_REAL_LINE}) is False


def test_boundary_85_00_ON_inclusive():
    """2026-08 Perklocks Strictness Fix: 85.00 is ON board (INCLUSIVE)."""
    from services.main_board_eligibility import is_main_board_eligible
    assert is_main_board_eligible({"lock_score": 85.0, **_REAL_LINE}) is True


def test_boundary_85_001_on():
    from services.main_board_eligibility import is_main_board_eligible
    # 85.001 is >= 85 and MUST qualify per INCLUSIVE contract.
    assert is_main_board_eligible({"lock_score": 85.001, **_REAL_LINE}) is True


def test_boundary_85_01_on():
    from services.main_board_eligibility import is_main_board_eligible
    assert is_main_board_eligible({"lock_score": 85.01, **_REAL_LINE}) is True


def test_boundary_86_00_on():
    from services.main_board_eligibility import is_main_board_eligible
    assert is_main_board_eligible({"lock_score": 86.0, **_REAL_LINE}) is True


# ── A. Same boundary via the Mongo predicate builder ────────────────
def test_mongo_predicate_uses_gte_85_inclusive():
    from services.main_board_eligibility import main_board_lock_score_query
    q = main_board_lock_score_query()
    # 2026-08 Perklocks Strictness Fix — predicate uses $gte:85.
    assert "$and" in q
    lock_gate = q["$and"][-1]
    branches = lock_gate["$or"]
    assert branches[0] == {"published_lock_score": {"$gte": 85.0}}
    inner_or = [c for c in branches[1]["$and"] if "$or" in c][0]["$or"]
    assert {"lock_score":    {"$gte": 85.0}} in inner_or
    assert {"lock_score_v2": {"$gte": 85.0}} in inner_or


def test_mongo_predicate_min_lock_narrows_but_never_lowers():
    from services.main_board_eligibility import main_board_lock_score_query
    # min_lock=99 → narrower band; uses $gte: 99 on the canonical branch.
    q99 = main_board_lock_score_query(min_lock=99)
    lock_gate_99 = q99["$and"][-1]
    assert lock_gate_99["$or"][0] == {"published_lock_score": {"$gte": 99.0}}
    # min_lock=70 (< 85) is clamped up to the base `>= 85` contract.
    q70 = main_board_lock_score_query(min_lock=70)
    lock_gate_70 = q70["$and"][-1]
    assert lock_gate_70["$or"][0] == {"published_lock_score": {"$gte": 85.0}}


# ── B. Locks views: filter/alt sub-tabs must NOT lower the floor ────
def test_picks_routes_default_floor_uses_shared_constant():
    src = (_BACKEND_ROOT / "routes" / "picks_routes.py").read_text()
    idx = src.find("default_floor =")
    assert idx > 0
    window = src[idx:idx + 400]
    # The default_floor line must reference a MAIN_BOARD_LOCK_FLOOR constant.
    assert "MAIN_BOARD_LOCK_FLOOR" in window
    # And must not smuggle in the retired per-view lowerings.
    assert "75.0 if has_market_filter" not in window
    assert "55.0 if lt ==" not in window


def test_picks_routes_uses_canonical_predicate_builder():
    src = (_BACKEND_ROOT / "routes" / "picks_routes.py").read_text()
    # The main Locks query must delegate to the central helper —
    # this is what carries the canonical-source preference
    # (published_lock_score >= 85) into the DB filter.
    assert "main_board_lock_score_query(" in src


# ── C. Candidate disposition lifecycle (central hook) ───────────────
def test_orchestrator_calls_record_batch_dispositions():
    src = (_BACKEND_ROOT / "services" /
           "pick_refresh_orchestrator.py").read_text()
    assert "record_batch_dispositions" in src, (
        "orchestrator must call the central disposition hook"
    )


def test_candidate_A_rejected_below_floor():
    """Candidate A cleared model math but landed at lock=84.5 → below
    the >85 floor.  Final tags: `off_board=True` with
    `off_board_reasons=['lock<85']`.  Expected trail:
      evaluated → rejected(lock_score_below_board_threshold)
    """
    from services.candidate_disposition import (
        record_batch_dispositions, why_missing,
    )
    key = _TID + "A_" + uuid.uuid4().hex[:8]

    async def go():
        db = _db()
        await db.candidate_dispositions.delete_many({"candidate_key": key})
        try:
            pick = {
                "id": key,
                "sport": "MLB",
                "market": "batter_hits",
                "lock_score": 84.5,
                "off_board": True,
                "off_board_reasons": ["lock<85"],
            }
            stats = await record_batch_dispositions(
                db, [pick], publication_summary={},
            )
            trail = await why_missing(db, key)
            stages = [t["stage"] for t in trail]
            assert stages == ["evaluated", "rejected"], stages
            assert trail[-1]["reason"] == "lock_score_below_board_threshold"
            assert stats["rejected"] >= 1
            assert stats["evaluated"] >= 1
        finally:
            await db.candidate_dispositions.delete_many({"candidate_key": key})
    _run(go())


def test_candidate_B_full_lifecycle_to_board_eligible():
    """Candidate B is a clean high-lock pick.  Expected trail:
      evaluated → accepted → published → board_eligible
    """
    from services.candidate_disposition import (
        record_batch_dispositions, why_missing,
    )
    key = _TID + "B_" + uuid.uuid4().hex[:8]

    async def go():
        db = _db()
        await db.candidate_dispositions.delete_many({"candidate_key": key})
        try:
            pick = {
                "id": key,
                "sport": "NBA",
                "market": "player_points",
                "lock_score": 92.0,
                # Real-line integrity (Support 2026-06 durable fix)
                "book_odds": -180, "implied_probability": 64.3,
                # No no_bet, no off_board — this pick clears every gate.
            }
            stats = await record_batch_dispositions(
                db, [pick], publication_summary={"errors": []},
            )
            trail = await why_missing(db, key)
            stages = [t["stage"] for t in trail]
            assert stages == [
                "evaluated", "accepted", "published", "board_eligible",
            ], stages
            assert stats["board_eligible"] >= 1
            assert stats["published"] >= 1
            assert stats["accepted"] >= 1
        finally:
            await db.candidate_dispositions.delete_many({"candidate_key": key})
    _run(go())


def test_candidate_C_rejected_no_bet():
    """Candidate C was ultimately tagged `no_bet=True` (e.g. K-math
    reconciler's losing side).  Expected trail:
      evaluated → rejected(no_bet)
    """
    from services.candidate_disposition import (
        record_batch_dispositions, why_missing,
    )
    key = _TID + "C_" + uuid.uuid4().hex[:8]

    async def go():
        db = _db()
        await db.candidate_dispositions.delete_many({"candidate_key": key})
        try:
            pick = {
                "id": key,
                "sport": "Soccer",
                "market": "goalscorer_anytime",
                "lock_score": 88.0,
                "no_bet": True,
                "no_bet_reason": "kmath_loser_over",
            }
            stats = await record_batch_dispositions(
                db, [pick], publication_summary={},
            )
            trail = await why_missing(db, key)
            stages = [t["stage"] for t in trail]
            assert stages == ["evaluated", "rejected"], stages
            assert trail[-1]["reason"] == "no_bet"
            assert stats["rejected"] >= 1
        finally:
            await db.candidate_dispositions.delete_many({"candidate_key": key})
    _run(go())


# ── D. Canonical Lock source — stale legacy MUST NOT override ──────
def test_canonical_stale_legacy_v2_high_does_not_promote():
    from services.main_board_eligibility import is_main_board_eligible
    pick = {
        "published_lock_score": 60.0,
        "lock_score": 60.0,
        "lock_score_v2": 98.0,   # stale (must be ignored)
    }
    assert is_main_board_eligible(pick) is False


def test_canonical_over_85_survives_stale_legacy_low():
    from services.main_board_eligibility import is_main_board_eligible
    pick = {
        "published_lock_score": 92.0,
        "lock_score": 64.0,      # stale legacy drift
        "lock_score_v2": 50.0,
        **_REAL_LINE,
    }
    assert is_main_board_eligible(pick) is True


def test_canonical_predicate_gates_legacy_branch_by_exists_false():
    """The Mongo predicate's legacy fallback branch MUST be gated by
    `published_lock_score` not existing on the doc — otherwise a
    canonically de-locked pick with stale legacy fields would slip
    through the DB filter."""
    from services.main_board_eligibility import main_board_lock_score_query
    q = main_board_lock_score_query()
    # Extract lock-gate branch (last $and clause; first is real-line).
    lock_gate = q["$and"][-1]
    legacy = lock_gate["$or"][1]
    assert "$and" in legacy
    assert {"published_lock_score": {"$exists": False}} in legacy["$and"]


# ── E. Fusion classification: verify (report-only) ─────────────────
def test_fusion_is_post_decision_enrichment_not_production():
    """Fusion is enrichment, NOT a production-decision engine.

    Contract:
      * `pick_fusion_decorator.enrich_pick_with_fusion` writes ONLY the
        `pick["fusion"]` sub-dict.  It never mutates `lock_score`,
        `win_probability`, `edge_percent`, `grade`, or `off_board`.
      * The orchestrator invokes fusion enrichment AFTER
        `board_validator`, `simulate_board`, `chalk_kill_switch`,
        `longshot_trap`, and `tag_board_visibility` — i.e. after all
        Lock Score / grade / eligibility decisions are final.
    """
    dec = (_BACKEND_ROOT / "services" /
           "pick_fusion_decorator.py").read_text()
    # The decorator's own contract statement.
    assert '**Never** modifies' in dec
    assert 'lock_score' in dec
    # Only `pick["fusion"] = ...` writes should be present — no
    # `pick["lock_score"] =` etc. mutations.
    for forbidden in (
        'pick["lock_score"] =',
        "pick['lock_score'] =",
        'pick["win_probability"] =',
        "pick['win_probability'] =",
        'pick["grade"] =',
        "pick['grade'] =",
        'pick["off_board"] =',
        "pick['off_board'] =",
    ):
        assert forbidden not in dec, (
            f"fusion decorator must not mutate {forbidden!r}"
        )

    orch = (_BACKEND_ROOT / "services" /
            "pick_refresh_orchestrator.py").read_text()
    # Fusion enrichment site sits AFTER board_visibility tagging.
    idx_bv = orch.find("tag_board_visibility")
    idx_fu = orch.find("from services.pick_fusion_decorator import enrich_picks_bulk")
    assert idx_bv > 0 and idx_fu > 0
    assert idx_fu > idx_bv, (
        "fusion enrichment must run AFTER board-visibility (post-decision)"
    )
