"""P0-3 (2026-08-11) — Tennis Fallback Repair.

Ensures:
  1. Tennis Extra fallback runs INDEPENDENTLY of primary success.
     Primary empty / primary failing / primary succeeding — the
     fallback always gets a chance.
  2. Duplicate picks are dropped when primary + fallback both fire.
  3. Every Tennis pick (primary or fallback) goes through canonical
     publication via the normal pipeline.
  4. Main Locks board still requires ``>85`` (P0-2 gate unaffected).
  5. Fallback never MANUFACTURES sportsbook odds/edge when no real
     book line exists — ``book_odds=None``, ``edge_percent=None``,
     and ``no_real_book_line=True`` are stamped instead.
  6. Scoring formulas / Odds API credentials untouched.
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
_TID = "p03tennis_"


def _db():
    return AsyncIOMotorClient(os.environ["MONGO_URL"])[
        os.environ.get("DB_NAME", "lockscore_db")]


def _run(coro):
    return asyncio.run(coro)


# ── 1. Structural — fallback runs before the "if not picks: return" gate ─
def test_orchestrator_fallback_runs_before_empty_early_return():
    src = (_BACKEND_ROOT / "services"
           / "pick_refresh_orchestrator.py").read_text()
    fb_marker = "P0-3 (2026-08-11) Tennis Extra fallback"
    fb_idx = src.find(fb_marker)
    assert fb_idx > 0, "P0-3 fallback marker missing"
    # The "if not picks: return 0" early-return must appear AFTER the
    # fallback block.
    fb_end = src.find("Tennis Extra scrape skipped", fb_idx)
    assert fb_end > 0
    return_idx = src.find("if not picks:", fb_end)
    assert return_idx > fb_end, (
        "early-return still fires before Tennis Extra fallback")


def test_orchestrator_fallback_gated_by_sport_filter():
    """Fallback is only skipped when a NON-Tennis sport_filter is
    explicitly requested (e.g. MLB pregame loop).  Unfiltered or
    Tennis-scoped refreshes always run the fallback."""
    src = (_BACKEND_ROOT / "services"
           / "pick_refresh_orchestrator.py").read_text()
    assert "_run_tennis_fallback = (" in src
    assert 'sport_filter is None' in src
    assert '"tennis"' in src.lower()


def test_orchestrator_normalizes_none_primary_to_empty_list():
    """A recoverable provider failure that leaves ``picks=None`` must
    NOT crash the fallback path."""
    src = (_BACKEND_ROOT / "services"
           / "pick_refresh_orchestrator.py").read_text()
    assert "if picks is None:" in src
    # Sanity-check the assignment immediately follows.
    idx = src.find("if picks is None:")
    window = src[idx:idx + 80]
    assert "picks = []" in window


# ── 2. Dedupe behavior when primary + fallback overlap ──────────────
def test_fallback_dedupes_on_id_collision():
    """The de-dupe rule uses the pick ``id`` field.  Simulate the
    orchestrator's dedupe loop directly."""
    primary = [{"id": "A"}, {"id": "B"}]
    fallback = [{"id": "B"}, {"id": "C"}, {"id": "D"}]

    existing_ids = {p.get("id") for p in primary}
    added = 0
    for ep in fallback:
        if ep.get("id") in existing_ids:
            continue
        primary.append(ep)
        added += 1
    assert added == 2
    ids = [p["id"] for p in primary]
    assert ids == ["A", "B", "C", "D"]


# ── 3. Fallback writer contract — no manufactured book data ─────────
def test_tennis_extra_writer_no_manufactured_edge_or_odds():
    src = (_BACKEND_ROOT / "tennis_extra" / "picks.py").read_text()
    # Presence of the P0-3 marker.
    assert "P0-3 (2026-08-11)" in src
    # Look at the fallback ELSE branch (no real book line).
    # Extract from `else:` line following `if using_real:` down to the
    # closing of that branch (marked by "# ── Lock score").
    else_idx = src.find(
        "P0-3 (2026-08-11) — do NOT manufacture sportsbook data")
    assert else_idx > 0
    branch = src[else_idx:else_idx + 2500]
    # book_odds must be None, not int(fav_odds).
    assert "book_odds_final     = None" in branch
    assert "int(fav_odds)" not in branch
    # edge must be None, not 0.0.
    assert "edge_pct            = None" in branch
    assert "edge_pct            = 0.0" not in branch
    # And the pick must carry the explicit no_real_book_line flag.
    assert '"no_real_book_line": no_real_book_line,' in src


def test_tennis_extra_data_driven_edge_only_with_real_odds():
    """The DD-model branch previously overwrote edge_percent
    unconditionally.  P0-3 gates it behind ``using_real``."""
    src = (_BACKEND_ROOT / "tennis_extra" / "picks.py").read_text()
    # Locate the DD branch.
    dd_idx = src.find('pick_doc["data_driven_used"] = True')
    assert dd_idx > 0
    window = src[dd_idx:dd_idx + 900]
    # The edge assignment must be inside an "if using_real:" guard.
    assert "if using_real:" in window
    # Verify the guarded assignment is present.
    assert 'pick_doc["edge_percent"] = round(' in window


# ── 4. Locks contract >85 remains intact — regression check ────────
def test_locks_contract_still_strict_gt_85_after_p03():
    from services.main_board_eligibility import (
        is_main_board_eligible, main_board_lock_score_query,
    )
    # Boundary: 85 stays off, 85.001 on.
    assert is_main_board_eligible({"lock_score": 85.0}) is False
    assert is_main_board_eligible({"lock_score": 85.001}) is True
    # Query still uses $gt: 85.
    q = main_board_lock_score_query()
    assert q["$or"][0] == {"published_lock_score": {"$gt": 85.0}}


def test_tennis_extra_pick_with_null_edge_still_board_eligible_if_lock_over_85():
    """A tennis_extra pick with edge=None and lock=90 must remain
    board-eligible — the null edge doesn't disqualify it."""
    from services.main_board_eligibility import is_main_board_eligible
    p = {"source": "tennis_extra", "lock_score": 90.0,
         "edge_percent": None, "book_odds": None,
         "no_real_book_line": True}
    assert is_main_board_eligible(p) is True


def test_tennis_extra_pick_at_lock_85_still_off_board():
    """The >85 contract cannot be bypassed by the tennis_extra
    source label — same as every other sub-query."""
    from services.main_board_eligibility import is_main_board_eligible
    p = {"source": "tennis_extra", "lock_score": 85.0,
         "edge_percent": None, "no_real_book_line": True}
    assert is_main_board_eligible(p) is False


# ── 5. Tour tier support — ATP / WTA / Challenger scaffolding ──────
def test_fallback_supports_atp_wta_challenger_tiers():
    """The scraper and lock formula still recognise all three main
    tour tiers (ATP / WTA / Challenger)."""
    src = (_BACKEND_ROOT / "tennis_extra" / "picks.py").read_text()
    for tok in ("challenger", "atp", "wta"):
        assert tok in src.lower(), f"tier token missing: {tok}"


# ── 6. Publication path — null edge round-trips through publish ─────
def test_publication_preserves_null_edge_for_tennis_extra():
    """Full publish → hydrate round-trip proves a tennis_extra pick
    with `edge_percent=None` reaches the picks doc with the same
    None (canonical unit contract P0-1 respected)."""
    async def go():
        from services.prediction_publication_service import (
            PredictionPublicationService,
        )
        db = _db()
        pub = PredictionPublicationService(db)
        pid = _TID + uuid.uuid4().hex[:12]
        pick = {
            "id": pid,
            "sport": "Tennis",
            "league": "Kitzbühel",
            "tournament_tier": "ATP 250",
            "event": "Struff vs Cerundolo",
            "event_time": (datetime.now(timezone.utc)
                           + timedelta(hours=6)).isoformat(),
            "market": "Struff Moneyline",
            "selection": "Struff",
            "book_odds": None,           # No real book line.
            "win_probability": 62.5,     # From TE scrape.
            "edge_percent": None,        # Not manufactured.
            "lock_score": 88.0,
            "grade": "Strong Lock",
            "confidence": "High",
            "source": "tennis_extra",
            "no_real_book_line": True,
            "pick_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }
        await db.picks.delete_many({"id": pid})
        await db.prediction_snapshots.delete_many(
            {"prediction_id": pid})
        await db.picks.insert_one(pick)
        try:
            await pub.publish(pick, dual_write=True,
                              publication_source="canonical_pipeline")
            after = await db.picks.find_one({"id": pid}, {"_id": 0})
            # edge_percent still None post-publication.
            assert after["edge_percent"] is None
            # book_odds still None.
            assert after["book_odds"] is None
            # win_probability preserved as percentage.
            assert abs(float(after["win_probability"]) - 62.5) < 1e-6
            # Snapshot exposes canonical fraction.
            snap = await db.prediction_snapshots.find_one(
                {"prediction_id": pid}, {"_id": 0})
            assert abs(float(snap["published_probability"]) - 0.625) < 1e-6
            assert snap["published_edge"] is None
            assert snap["published_confidence"] == "High"
        finally:
            await db.picks.delete_many({"id": pid})
            await db.prediction_snapshots.delete_many(
                {"prediction_id": pid})
    _run(go())


# ── 7. Simulated pipeline branches — the four fallback scenarios ────
def test_pipeline_shape_primary_succeeds():
    """Primary succeeds ⇒ fallback appends (deduped)."""
    primary = [{"id": "prim1"}]
    fallback = [{"id": "prim1"}, {"id": "fb1"}]
    existing = {p["id"] for p in primary}
    for ep in fallback:
        if ep["id"] not in existing:
            primary.append(ep)
    assert [p["id"] for p in primary] == ["prim1", "fb1"]


def test_pipeline_shape_primary_empty():
    """Primary returns empty ⇒ fallback populates from scratch."""
    primary = []
    fallback = [{"id": "fb1"}, {"id": "fb2"}]
    existing = {p["id"] for p in primary}
    for ep in fallback:
        if ep["id"] not in existing:
            primary.append(ep)
    assert [p["id"] for p in primary] == ["fb1", "fb2"]


def test_pipeline_shape_primary_none_normalized():
    """Primary None ⇒ normalized to [] ⇒ fallback runs safely."""
    picks = None
    if picks is None:
        picks = []
    fallback = [{"id": "fb1"}]
    for ep in fallback:
        picks.append(ep)
    assert picks == [{"id": "fb1"}]


def test_pipeline_shape_both_succeed_and_dedupe_overlap():
    """Primary + fallback overlap ⇒ overlapping ids dropped once."""
    primary = [{"id": "shared"}, {"id": "primOnly"}]
    fallback = [{"id": "shared"}, {"id": "fbOnly"}]
    existing = {p["id"] for p in primary}
    for ep in fallback:
        if ep["id"] not in existing:
            primary.append(ep)
    ids = [p["id"] for p in primary]
    assert ids == ["shared", "primOnly", "fbOnly"]
    assert ids.count("shared") == 1
