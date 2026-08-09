"""P0-2 (2026-08-11) — Canonical Locks Board Repair.

Ensures the main Locks board enforces ONE eligibility rule everywhere:

    FINAL LOCK SCORE > 85  ⇒ eligible
    FINAL LOCK SCORE ≤ 85  ⇒ NOT eligible

No sub-query, carve-out, ESPN fallback, or filter is allowed to
lower the threshold below the base ``>85`` contract.

Covered acceptance points:
  1. A pick at exactly 85.0 is rejected.
  2. A pick at 85.001 / 85.01 / 90 is eligible.
  3. Sub-query floors below 85 (elite=80, model_only=75, tennis_ml=80,
     tennis_extra=75, tennis_alt=70, mlb_k=70, mlb_hitter=70) cannot
     smuggle lock ≤85 picks onto the board — the global gate rejects
     them.
  4. `source=espn_fallback` bypass in `standard_q` cannot skip the
     `>85` contract.
  5. `chalk_verified=True` bypass in `standard_q` cannot skip the
     `>85` contract.
  6. `min_lock` no longer overwrites the base ``$and`` (date +
     72-hour horizon) conditions.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import uuid
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient


_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
_TID = "p02board_"


def _db():
    return AsyncIOMotorClient(os.environ["MONGO_URL"])[
        os.environ.get("DB_NAME", "lockscore_db")]


def _run(coro):
    return asyncio.run(coro)


# ── Source-level structural invariants ──────────────────────────────
def test_min_lock_appends_not_overwrites_and_clause():
    """P0-2 bug: `q["$and"] = [...]` overwrote the base date/horizon
    conditions.  Fixed to APPEND."""
    src = (_BACKEND_ROOT / "routes" / "picks_routes.py").read_text()
    # Locate the min_lock user-floor block.
    idx = src.find("User-supplied min_lock floor — NARROW ONLY")
    assert idx > 0, "P0-2 comment marker missing"
    window = src[idx:idx + 1400]
    # The assignment MUST be an append via (q.get("$and") or []) + [...]
    assert '(q.get("$and") or []) + [{"$or"' in window
    # And must NOT contain a plain overwrite of the shape
    # ``q["$and"] = [{"$or":`` (blanket wipe).
    assert 'q["$and"] = [{"$or":' not in window


def test_global_lock_gate_uses_canonical_predicate():
    """The global gate must delegate to
    ``main_board_lock_score_query`` (the canonical predicate that
    prefers ``published_lock_score`` and enforces strict ``>85``)."""
    src = (_BACKEND_ROOT / "routes" / "picks_routes.py").read_text()
    idx = src.find("P0-2 GLOBAL LOCKS THRESHOLD ENFORCEMENT")
    assert idx > 0, "P0-2 global gate comment missing"
    window = src[idx:idx + 2000]
    assert "main_board_lock_score_query(" in window
    # Predicate is AND-ed, never OR-ed.
    assert "q[\"$and\"] = (q.get(\"$and\") or []) + [_global_lock_gate]" in window


# ── Contract enforcement — Python-level ─────────────────────────────
def test_boundary_85_00_rejected():
    from services.main_board_eligibility import is_main_board_eligible
    for pick in (
        {"lock_score": 85.0},
        {"lock_score_v2": 85.0},
        {"published_lock_score": 85.0},
        {"lock_score": 85.0, "lock_score_v2": 85.0},
    ):
        assert is_main_board_eligible(pick) is False


def test_boundary_over_85_eligible():
    from services.main_board_eligibility import is_main_board_eligible
    for v in (85.001, 85.01, 85.5, 90.0, 99.0):
        assert is_main_board_eligible({"lock_score": v}) is True, v


# ── Sub-query floors below 85 are neutralized by the global gate ────
def test_global_gate_defeats_elite_80_floor():
    """elite_q admits lock ≥ 80.  A pick at 82 must NOT reach the
    board via that carve-out — the global gate rejects it."""
    from services.main_board_eligibility import is_main_board_eligible
    p = {"elite_player": True, "lock_score": 82.0}
    assert is_main_board_eligible(p) is False


def test_global_gate_defeats_model_only_75_floor():
    from services.main_board_eligibility import is_main_board_eligible
    p = {"is_model_only": True, "lock_score": 78.0}
    assert is_main_board_eligible(p) is False


def test_global_gate_defeats_tennis_extra_75_floor():
    from services.main_board_eligibility import is_main_board_eligible
    p = {"sport": "Tennis", "source": "tennis_extra", "lock_score": 80.0}
    assert is_main_board_eligible(p) is False


def test_global_gate_defeats_tennis_alt_70_floor():
    from services.main_board_eligibility import is_main_board_eligible
    p = {"sport": "Tennis", "is_alt_prop": True, "lock_score": 74.9}
    assert is_main_board_eligible(p) is False


def test_global_gate_defeats_mlb_k_70_floor():
    from services.main_board_eligibility import is_main_board_eligible
    p = {"sport": "MLB", "market": "Cole Over 6.5 Strikeouts",
         "lock_score": 72.0}
    assert is_main_board_eligible(p) is False


def test_global_gate_defeats_mlb_hitter_70_floor():
    from services.main_board_eligibility import is_main_board_eligible
    p = {"sport": "MLB", "market": "Judge Over 0.5 Hits",
         "lock_score": 74.0}
    assert is_main_board_eligible(p) is False


def test_espn_fallback_cannot_bypass_threshold():
    """`source=espn_fallback` had a bypass in standard_q with NO lock
    filter — it must be neutralized by the global gate."""
    from services.main_board_eligibility import is_main_board_eligible
    p = {"source": "espn_fallback", "lock_score": 55.0}
    assert is_main_board_eligible(p) is False, (
        "espn_fallback with lock 55 must NOT be board-eligible"
    )


def test_chalk_verified_cannot_bypass_threshold():
    """`chalk_verified=True` had a bypass in standard_q with NO lock
    filter — global gate rejects sub-85 chalk-verified picks."""
    from services.main_board_eligibility import is_main_board_eligible
    p = {"chalk_verified": True, "lock_score": 80.0}
    assert is_main_board_eligible(p) is False


def test_high_lock_bypass_90_still_qualifies():
    """The high_lock_bypass_q at 90+ is above the ``>85`` line so it
    remains valid.  Sanity check that we didn't over-clamp."""
    from services.main_board_eligibility import is_main_board_eligible
    p = {"lock_score": 91.0}
    assert is_main_board_eligible(p) is True


# ── Mongo predicate — strict >85, canonical preference ─────────────
def test_mongo_predicate_strict_gt_85():
    from services.main_board_eligibility import main_board_lock_score_query
    q = main_board_lock_score_query()
    # Canonical branch: $gt 85 (strict), NOT $gte 85.
    assert q["$or"][0] == {"published_lock_score": {"$gt": 85.0}}


def test_mongo_predicate_min_lock_below_85_clamped_up():
    """A caller supplying min_lock=70 must NOT lower the floor —
    the base ``>85`` contract applies."""
    from services.main_board_eligibility import main_board_lock_score_query
    q = main_board_lock_score_query(min_lock=70)
    assert q["$or"][0] == {"published_lock_score": {"$gt": 85.0}}


def test_mongo_predicate_min_lock_above_85_narrows():
    from services.main_board_eligibility import main_board_lock_score_query
    q = main_board_lock_score_query(min_lock=95)
    assert q["$or"][0] == {"published_lock_score": {"$gte": 95.0}}


# ── End-to-end via /picks/today (Mongo-level) ──────────────────────
def test_picks_today_query_rejects_lock_85_and_below_across_bypass_paths():
    """Insert picks that match each bypass path but with lock ≤ 85
    and confirm the /picks/today endpoint filters them out.  Uses
    the same query builder path the endpoint uses."""
    async def go():
        from services.main_board_eligibility import (
            main_board_lock_score_query,
        )
        db = _db()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        et = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
        _tid_prefix = _TID + uuid.uuid4().hex[:8] + "_"

        # Six picks — each one matches a different sub-query bypass
        # AND has a lock score at or below 85.  All must be rejected.
        docs = [
            {"id": _tid_prefix + "elite80",
             "lock_score": 82.0, "elite_player": True,
             "pick_date": today, "event_time": et,
             "no_bet": False, "grade": "Playable Bet"},
            {"id": _tid_prefix + "model75",
             "lock_score": 78.0, "is_model_only": True,
             "pick_date": today, "event_time": et,
             "no_bet": False, "grade": "Playable Bet"},
            {"id": _tid_prefix + "espn_fb",
             "lock_score": 55.0, "source": "espn_fallback",
             "pick_date": today, "event_time": et,
             "no_bet": False, "grade": "Playable Bet"},
            {"id": _tid_prefix + "chalk_v",
             "lock_score": 78.0, "chalk_verified": True,
             "pick_date": today, "event_time": et,
             "no_bet": False, "grade": "Playable Bet"},
            {"id": _tid_prefix + "boundary85",
             "lock_score": 85.0, "lock_score_v2": 85.0,
             "pick_date": today, "event_time": et,
             "no_bet": False, "grade": "Strong Lock"},
            # One control: 85.5 — must pass.
            {"id": _tid_prefix + "control_855",
             "lock_score": 85.5, "lock_score_v2": 85.5,
             "pick_date": today, "event_time": et,
             "no_bet": False, "grade": "Strong Lock",
             "publication_source": "canonical_pipeline"},
        ]
        await db.picks.delete_many({"id": {"$regex": f"^{_tid_prefix}"}})
        await db.picks.insert_many(docs)
        try:
            # Simulate the endpoint's global gate query.
            q = {
                "id": {"$regex": f"^{_tid_prefix}"},
                "$and": [main_board_lock_score_query()],
            }
            rows = [r async for r in db.picks.find(q, {"_id": 0})]
            ids_returned = {r["id"] for r in rows}
            # Only the control (85.5, canonical publication) survives.
            assert ids_returned == {_tid_prefix + "control_855"}, (
                f"expected only control to survive, got {ids_returned}"
            )
        finally:
            await db.picks.delete_many({"id": {"$regex": f"^{_tid_prefix}"}})
    _run(go())


def test_min_lock_preserves_date_and_horizon_conditions():
    """P0-2 bug repro: with min_lock=99, the previous code overwrote
    q['$and'] entirely — dropping the base date + 72h horizon
    guard. Fixed to append.  Prove the guard survives."""
    src = (_BACKEND_ROOT / "routes" / "picks_routes.py").read_text()
    # Base $and is initialised with both (pick_date OR event_time
    # window) AND (event_time <= horizon).  Both markers must exist.
    assert '"$and": [' in src
    assert '"pick_date": _today' in src
    assert '_horizon_end' in src
    # And the min_lock block must NOT wipe it — check for the
    # append idiom.
    idx = src.find("User-supplied min_lock floor — NARROW ONLY")
    assert idx > 0
    window = src[idx:idx + 1400]
    assert '(q.get("$and") or []) + [{"$or"' in window
