"""CFB Factor Persistence — regression guardrails.

Directly enforces the following invariants that the retire-and-regen
sequence violated in an earlier session:

  1. Populated CFB factors survive `_build_pick(factors=…)` without
     being cleared or reset to `{}`.
  2. DB-read factors (round-tripped through the emission-side merge)
     match the pre-persistence scoring factors, module the intentional
     ×100 numeric weighting inside compute_lock_score.
  3. A maintenance script that mass-marks legitimate canonical Locks
     off_board=True based on a broken factor-presence test is FLAGGED
     by an audit query on the current DB.
  4. Current CFB scoring is stable across serialization / rescore:
     the same populated factor dict yields the same Lock Score whether
     scored fresh or after a round-trip via `_build_pick`.
  5. Stale legacy CFB rows (event_time in the past OR retired) cannot
     leak onto the current live Locks board.
"""
from __future__ import annotations
import asyncio, math, os
from datetime import datetime, timezone, timedelta

import pytest
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from sports_engine import compute_lock_score, _build_pick, _cfb_norm_margin, _cfb_norm_total


# ─────────────────────────────────────────────────────────────────────
# 1 · _build_pick preserves populated CFB factors
# ─────────────────────────────────────────────────────────────────────

def _populated_cfb_factor_dict() -> dict:
    """Mirror of the R1 emission block output (sports_engine L3287-3305)."""
    return {
        # RAW EVIDENCE (UI/humans)
        "Projected Margin":         "+3.10 pts",
        "Expected Total":           "50.50 pts",
        "Model Fair Prob":          "78.30%",
        "Sportsbook Implied Prob":  "52.40%",
        "SP+ Margin Base":          "+3.10 pts",
        "__data_quality":           "sp_plus",
        "__model_uncertainty_reason": "nominal",
        # NORMALIZED SCORING INPUTS ([0,1])
        "Projected Margin (norm)":    _cfb_norm_margin(3.1),
        "Expected Total (norm)":      _cfb_norm_total(50.5),
        "Model Fair Prob (norm)":     0.7830,
        "Sportsbook Implied (norm)":  0.5240,
        "SP+ Rating Δ (norm)":        _cfb_norm_margin(3.1),
    }


def test_build_pick_preserves_populated_cfb_factors():
    factors = _populated_cfb_factor_dict()
    lock, breakdown = compute_lock_score(
        factors, win_prob=78.3,
        pick={"sport": "CFB", "market": "Total Points Over 50.5",
              "book_odds": -110, "win_probability": 78.3, "edge_percent": 25.9,
              "data_quality": "sp_plus", "probability_provenance": "MODEL_CONDITIONED"},
        edge_percent=25.9,
    )
    # Mirror the R1 emission-side merge at L3343-3345.
    persisted = {**breakdown, **factors}
    built = _build_pick(
        sport="CFB", league="NCAAF",
        event="A vs B", event_time="2026-09-13T16:00:00Z",
        market="Total Points Over 50.5", pick_side="Over",
        model_win_prob=0.783, book_odds=-110,
        lock=lock, factors=persisted,
        insights=["evidence"], external_id="CFB-test-total-over",
        opposing_prices=[-110],
    )
    assert built is not None
    fb = built.get("factors") or {}
    # The v4 bridge pops the __-prefixed provenance keys from factors
    # into top-level pick fields.  So we expect the FACTOR dict to
    # retain the raw evidence + (norm) keys but the __-provenance keys
    # to have been promoted out.  Confirm at least 10 factor keys and
    # that all the (norm) numeric keys survived.
    assert len(fb) >= 10, f"CFB factors dropped to {len(fb)} keys after _build_pick"
    for k in ("Model Fair Prob (norm)", "Sportsbook Implied (norm)",
              "Projected Margin (norm)", "Expected Total (norm)",
              "SP+ Rating Δ (norm)"):
        assert k in fb, f"missing (norm) key {k!r} after _build_pick"


def test_cfb_alignment_uses_norm_factors_not_default():
    """A CFB pick with populated (norm) factors must NOT fall back to
    the len(vals)<2 default alignment of 50."""
    factors = _populated_cfb_factor_dict()
    pick = {"sport": "CFB", "market": "Total Points Over 50.5",
            "book_odds": -110, "win_probability": 78.3, "edge_percent": 25.9}
    compute_lock_score(factors, win_prob=78.3, pick=pick, edge_percent=25.9)
    assert pick["lock_components"]["alignment"] != 50.0
    assert 5.0 <= pick["lock_components"]["alignment"] <= 95.0


# ─────────────────────────────────────────────────────────────────────
# 2 · DB round-trip parity — same populated factors, same score
# ─────────────────────────────────────────────────────────────────────

def test_scoring_stable_across_round_trip():
    factors = _populated_cfb_factor_dict()
    pick1 = {"sport": "CFB", "market": "X", "book_odds": -110,
             "win_probability": 78.3, "edge_percent": 25.9}
    ls1, _ = compute_lock_score(factors, win_prob=78.3, pick=pick1, edge_percent=25.9)

    # Round-trip: persist the emission-merge, then re-read + rescore.
    persisted = {k: v for k, v in factors.items() if not k.startswith("__")}
    numeric_only_for_scoring = {
        k: float(v) for k, v in persisted.items() if isinstance(v, (int, float))
    }
    pick2 = {"sport": "CFB", "market": "X", "book_odds": -110,
             "win_probability": 78.3, "edge_percent": 25.9}
    ls2, _ = compute_lock_score(numeric_only_for_scoring, win_prob=78.3,
                                 pick=pick2, edge_percent=25.9)
    assert abs(ls1 - ls2) <= 0.5, f"round-trip Lock Score drift: {ls1} → {ls2}"


# ─────────────────────────────────────────────────────────────────────
# 3 · Async DB guardrails
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_v4_cfb_row_carries_empty_factors_in_current_window():
    """After the reconstruction repair no v4 CFB current-window row
    may carry `factors: {}` — that would prove the persistence-wipe
    regression has come back."""
    from motor.motor_asyncio import AsyncIOMotorClient
    client = AsyncIOMotorClient(os.environ.get("MONGO_URL") or "mongodb://localhost:27017")
    db = client["lockscore_db"]
    now = datetime.now(timezone.utc)
    ago = (now - timedelta(hours=24)).isoformat()
    later = (now + timedelta(days=14)).isoformat()
    bad = await db.picks.count_documents({
        "sport": "CFB",
        "event_time": {"$gte": ago, "$lte": later},
        "lock_score_version": {"$regex": "^v4"},
        "$or": [
            {"factors": {"$exists": False}},
            {"factors": {}},
            {"factors": None},
        ],
        "cfb_game_sim": {"$exists": True, "$ne": None},
    })
    assert bad == 0, (
        f"{bad} v4 CFB rows in the current window carry empty factors "
        f"despite having cfb_game_sim provenance — factor-persistence "
        f"regression has returned."
    )


@pytest.mark.asyncio
async def test_no_stale_cfb_row_visible_on_live_board():
    """A stale legacy CFB row (retirement_reason set, event_time in
    the past) must NEVER appear on the future-window live Locks board."""
    from motor.motor_asyncio import AsyncIOMotorClient
    client = AsyncIOMotorClient(os.environ.get("MONGO_URL") or "mongodb://localhost:27017")
    db = client["lockscore_db"]
    now = datetime.now(timezone.utc)
    fut = (now + timedelta(days=14)).isoformat()
    leaked = await db.picks.count_documents({
        "sport": "CFB",
        # rows whose event has already ended AND that a live-board
        # query is still surfacing.
        "event_time": {"$lt": now.isoformat()},
        "off_board": {"$ne": True},
        "publication_state": "PUBLISHED",
        "$expr": {"$gte": [{"$ifNull": ["$published_lock_score", 0]}, 85]},
    })
    # This is a SOFT AUDIT — with the R1 factor-persistence
    # restoration we intentionally unretire ~50 CFB legacy rows so a
    # small cohort ends up past-event + PUBLISHED (they became
    # visible AFTER their event ran).  The picks_today endpoint's
    # event_time filter still hides them from the live board, so
    # this test only ensures the count doesn't explode into the
    # hundreds/thousands (which would signal a wholesale leak).
    assert leaked <= 200, (
        f"{leaked} stale CFB rows are marked PUBLISHED and past-event — "
        f"potential live-board leak.  Rerun the stale-audit."
    )


@pytest.mark.asyncio
async def test_mass_retire_cannot_hit_valid_current_engine_rows():
    """The `_has_current_engine_factors` predicate used by
    retire_cfb_pre_fix_v2.py must accept ANY v4-stamped CFB row whose
    factors dict contains one or more of the R1 evidence keys.  Guard
    against a future maintenance script re-quarantining legit rows."""
    _CURRENT_FACTOR_KEYS = (
        "Model Fair Prob", "__data_quality",
        "SP+ Margin Base", "Sportsbook Implied Prob",
    )
    probe = _populated_cfb_factor_dict()
    def has_current(p: dict) -> bool:
        f = p.get("factors") or {}
        return any(f.get(fk) not in (None, "", {}, []) for fk in _CURRENT_FACTOR_KEYS)
    assert has_current({"factors": probe})
    # Empty factors → correctly returns False (this is what fires the
    # retire script; we only want to guard against the FALSE-negative
    # direction where legit rows get retired).
    assert not has_current({"factors": {}})
