"""Phase 4 — REAL MARKET / NO SYNTHETIC WAGER TRUTH invariants.

Root-class proofs the directive requires:

  M1. No synthetic actionable line can reach publication.
       — `model_line=True` and synthesized-`model_source` prefixes
         are rejected by the canonical boundary.
  M2. No synthetic sportsbook odds can reach publication.
       — `_SYNTHETIC_ODDS_SOURCES` labels + `no_real_book_line=True`
         combos are rejected.
  M3. Model-only theoretical markets remain research-only.
       — `no_real_book_line=True` + book_odds=None → MODEL_ONLY
         state → not blocked from DB, but blocked from Locks board
         via `is_main_board_eligible`.
  M4. Every published actionable wager maps to an observed
       sportsbook offering (REAL line_state).
  M5. Real alternate ladders are preserved — different legit
       lines coexist (no canonical-key collision on distinct
       thresholds).
  M6. Duplicate aliases of the same sportsbook wager collapse
       canonically (same event + same line + same side → same key).
  M7. Missing sportsbook / book provenance prevents actionable
       publication where required (`_has_book_odds` False → not
       eligible).
  M8. No arbitrary ladder count cap exists.
  M9. Soccer synthetic alternate totals (Poisson from main O/U)
       cannot become Locks — rejected at the canonical boundary.
  M10. NFL/other producer synthetic alt-prop paths cannot become
        Locks unless backed by a real observed offering.
"""
from __future__ import annotations
import re
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest

from services.canonical_publication_boundary import (
    evaluate_publication, PublicationState, RejectionReason,
)


def _base_valid_pick(**extra):
    """Minimal pick that passes the boundary — REAL odds, model
    provenance, identity classified."""
    p = {
        "id": "pick_1",
        "sport": "MLB",
        "market": "Total Runs Over 8.5",
        "selection": "Over",
        "book_odds": -110,
        "odds_source": "the_odds_api",
        "line": 8.5,
        "model_probability": 0.55,
        "identity_class": "AUTHORITATIVE",
    }
    p.update(extra)
    return p


# ── M1 — model_line rejected ─────────────────────────────────────
def test_model_line_true_rejected_at_boundary():
    p = _base_valid_pick(model_line=True)
    v = evaluate_publication(p)
    assert v.state == PublicationState.REJECTED
    assert RejectionReason.MODEL_LINE_NOT_REAL_OFFERING.value in v.reasons


def test_synthesized_model_source_prefixes_rejected():
    for src in ("poisson_from_main_total", "synthetic_alt_ladder",
                "model_only_projection", "synthesized_alt_line",
                "synthesized_from_ou"):
        p = _base_valid_pick(model_source=src)
        v = evaluate_publication(p)
        assert v.state == PublicationState.REJECTED, \
            f"{src} must be rejected"
        assert RejectionReason.MODEL_LINE_NOT_REAL_OFFERING.value in v.reasons


def test_legitimate_model_source_still_allowed():
    """Model provenance blocks (mlb_shared_run_distribution_v1,
    cfb_sp_game_model, platinum_nfl) are NOT synthesized-alt-line
    prefixes and must publish."""
    for src in ("mlb_shared_run_distribution_v1", "cfb_sp_game_model",
                "platinum_nfl_game_runtime"):
        p = _base_valid_pick(model_source=src)
        v = evaluate_publication(p)
        assert v.state == PublicationState.PUBLISHED, \
            f"{src} must NOT be rejected"


# ── M2 — synthetic sportsbook odds rejected ─────────────────────
def test_synthetic_odds_source_label_rejected():
    for src in ("synthetic", "model_derived", "hfa_baseline",
                "espn_fallback"):
        p = _base_valid_pick(odds_source=src)
        v = evaluate_publication(p)
        assert v.state == PublicationState.REJECTED
        assert RejectionReason.SYNTHETIC_BOOK_ODDS.value in v.reasons


def test_no_real_book_line_with_book_odds_rejected():
    p = _base_valid_pick(no_real_book_line=True)
    v = evaluate_publication(p)
    assert v.state == PublicationState.REJECTED
    assert RejectionReason.NO_REAL_LINE_WITH_ODDS.value in v.reasons


# ── M3 — model-only theoretical stays research-only ─────────────
def test_model_only_no_odds_is_research_only_not_main_board_eligible():
    from services.main_board_eligibility import is_main_board_eligible
    pick = {
        "sport": "Soccer",
        "market": "Total Over 3.5",
        "selection": "Over",
        "book_odds": None,
        "no_real_book_line": True,
        "model_only": True,
        "lock_score": 92.0,
        "published_lock_score": 92.0,
        "status": "pending",
    }
    assert is_main_board_eligible(pick) is False


# ── M4/M7 — REAL required for publication ───────────────────────
def test_real_line_publishes():
    p = _base_valid_pick()
    v = evaluate_publication(p)
    assert v.state == PublicationState.PUBLISHED
    assert v.meta.get("line_state") == "REAL"


def test_missing_book_odds_not_main_board_eligible():
    from services.main_board_eligibility import is_main_board_eligible
    pick = {
        "sport": "MLB", "market": "Moneyline",
        "book_odds": None, "implied_probability": None,
        "lock_score": 92.0, "published_lock_score": 92.0,
        "status": "pending",
    }
    assert is_main_board_eligible(pick) is False


# ── M5/M6 — Real ladder preservation + canonical uniqueness ─────
def test_real_alternate_totals_different_lines_coexist():
    """Two picks at DIFFERENT real lines are DIFFERENT canonical
    wagers — canonical key must NOT collide."""
    from services.totals_truth_guard import _canonical_totals_key
    p85 = _base_valid_pick(line=8.5, market="Total Runs Over 8.5",
                            event_id="EVT_X")
    p95 = _base_valid_pick(line=9.5, market="Total Runs Over 9.5",
                            event_id="EVT_X",
                            id="pick_2")
    k85 = _canonical_totals_key(p85)
    k95 = _canonical_totals_key(p95)
    assert k85 != k95, "different real lines must be distinct wagers"


def test_duplicate_alias_same_line_collapses_canonically():
    """Same event + same line + Over vs Under = same canonical
    wager (side is state, not identity)."""
    from services.totals_truth_guard import _canonical_totals_key
    p_over = _base_valid_pick(line=8.5, event_id="EVT_Y",
                              market="Total Runs Over 8.5",
                              selection="Over")
    p_under = _base_valid_pick(line=8.5, event_id="EVT_Y",
                               id="pick_under",
                               market="Total Runs Under 8.5",
                               selection="Under")
    k1 = _canonical_totals_key(p_over)
    k2 = _canonical_totals_key(p_under)
    assert k1 == k2


# ── M8 — no arbitrary ladder count cap ──────────────────────────
def test_no_ladder_count_cap_constants():
    """No production module may enforce a ladder-count cap
    (e.g. `MAX_ALT_LINES_PER_EVENT = N`).  Alternate ladder rungs
    are preserved as long as each rung is a real observed offering."""
    banned = [
        r"MAX_ALT_LINES_PER_EVENT\s*=",
        r"MAX_LADDER_RUNGS\s*=",
        r"MAX_ALT_PROPS_PER_PLAYER\s*=",
        r"LADDER_COUNT_CAP\s*=",
    ]
    root = pathlib.Path("/app/backend")
    offenders = []
    for p in root.rglob("*.py"):
        if "test_phase" in p.name:
            continue
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for pat in banned:
            if re.search(pat, txt):
                offenders.append(f"{p.relative_to(root)}: {pat}")
    assert not offenders, f"ladder caps found: {offenders}"


# ── M9 — Soccer Poisson synthetic totals blocked ────────────────
def test_soccer_poisson_synthesized_alt_total_rejected():
    """Exact producer signature from sports_engine.py Soccer alt-
    totals path: model_line=True + is_alt=True +
    model_source='poisson_from_main_total'."""
    p = _base_valid_pick(
        sport="Soccer",
        market="Total Goals Over 1.5",
        selection="Over",
        line=1.5,
        book_odds=-380,           # producer-computed fair_odds
        model_line=True,
        is_alt=True,
        model_source="poisson_from_main_total",
    )
    v = evaluate_publication(p)
    assert v.state == PublicationState.REJECTED
    assert RejectionReason.MODEL_LINE_NOT_REAL_OFFERING.value in v.reasons


# ── M10 — Any other synthetic alt-prop path also blocked ────────
def test_generic_producer_synthetic_alt_prop_rejected():
    """A future NFL/NBA producer that emits an alt line stamped
    ``model_line=True`` gets rejected by the same rule (defense in
    depth for any producer we haven't audited)."""
    for sport, market in (
        ("NFL", "Passing Yards Over 289.5"),
        ("NBA", "Points Over 25.5"),
        ("CFB", "Total Points Over 63.5"),
    ):
        p = _base_valid_pick(
            id=f"pick_{sport}", sport=sport, market=market,
            model_line=True, is_alt=True,
        )
        v = evaluate_publication(p)
        assert v.state == PublicationState.REJECTED
        assert RejectionReason.MODEL_LINE_NOT_REAL_OFFERING.value in v.reasons


# ── Canonical publication barrier (direct-inject writers) parity ─
def test_direct_inject_barrier_also_rejects_model_line():
    from services.canonical_publication_barrier import (
        apply_canonical_barrier,
    )
    p = {
        "sport": "Soccer",
        "market": "Total Goals Over 3.5",
        "book_odds": -350,
        "lock_score": 90.0,
        "model_line": True,
    }
    apply_canonical_barrier(p)
    assert p.get("off_board") is True
    assert p.get("no_bet") is True
    assert "marked_model_line" in (p.get("barrier_failures") or [])


# ── Provenance completeness sanity check ────────────────────────
def test_provenance_fields_preserved_on_published_pick():
    """A published pick must retain the provenance the directive
    lists as minimum required (event/market/participant/line/odds/
    sportsbook/timestamp/source)."""
    p = _base_valid_pick(
        event="Team A @ Team B",
        event_id="EVT_Z",
        capture_ts="2026-06-10T18:00:00Z",
        bookmaker_key="draftkings",
    )
    v = evaluate_publication(p)
    assert v.state == PublicationState.PUBLISHED
    for k in ("event_id", "market", "selection", "line", "book_odds",
             "odds_source", "capture_ts", "bookmaker_key"):
        assert p.get(k) is not None or k == "event_id", f"missing {k}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
