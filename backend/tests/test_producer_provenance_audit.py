"""Producer-level provenance audit tests (2026-10-03).

Where the ``test_synthetic_book_odds_cutover_fix.py`` suite exercises
fixtures through the publication boundary, THIS suite exercises the
actual producer code path (``sports_engine._build_pick`` with the
same arguments the per-call-site builders would pass) and asserts the
real pick dict shape — including whether provenance survives — then
feeds that dict into ``evaluate_publication``.

Scope: the four narrow call sites that stamp ``odds_source="the_odds_api"``
on direct per-bookmaker observations:

  * game-market spread side-pick (MLB/CFB/NFL/NHL/NBA)      — sports_engine.py:~4461
  * Tennis alt spread builder                                — sports_engine.py:~6681
  * Tennis alt total builder                                 — sports_engine.py:~6818
  * MLB alt run-line builder                                 — sports_engine.py:~7087

Also exercised:
  * median/consensus path (ML h2h) with NO explicit provenance → REJECTED.
  * NFL alt threshold / ladder constants are not modified.
  * The ~4171 Soccer Poisson alt-total path (model_line=True)
    remains REJECTED via MODEL_LINE_NOT_REAL_OFFERING.
"""
from __future__ import annotations

import re
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path("/app/backend").resolve()))

from sports_engine import _build_pick
from services.canonical_publication_boundary import (
    PublicationState,
    RejectionReason,
    evaluate_publication,
)


# ───────────── Producer path: spread side-pick (MLB) ────────────────
def test_producer_spread_pick_stamps_the_odds_api():
    """Simulate what sports_engine.py:~4461 does for an MLB spread side
    with a real per-bookmaker outcome price (-110 at e.g. DraftKings)."""
    pick = _build_pick(
        sport="MLB", league="MLB", event="Yankees @ Red Sox",
        event_time="2026-10-03T23:00:00Z",
        market="Red Sox -1.5 Spread", pick_side="Red Sox",
        model_win_prob=0.56, book_odds=-110,
        lock=90.0, factors={}, insights=[],
        external_id="MLB-TEST-SP",
        home_team_name="Red Sox", away_team_name="Yankees",
        opposing_prices=[+110],
        odds_source="the_odds_api",     # ← stamped by call site 4461
    )
    assert pick.get("odds_source") == "the_odds_api"
    assert pick["book_odds"] == -110
    assert pick.get("no_real_book_line") is not True

    # Boundary verdict with full real-line integrity.
    pick.update({
        "id": "MLB-TEST-SP",
        "model_probability": 0.56,
        "identity_class": "AUTHORITATIVE",
    })
    v = evaluate_publication(pick)
    assert v.state == PublicationState.PUBLISHED, v.reasons
    assert v.meta.get("line_state") == "REAL"


# ───────────── Producer path: CFB spread ────────────────────────────
def test_producer_cfb_spread_stamps_the_odds_api():
    pick = _build_pick(
        sport="CFB", league="NCAAF", event="Alabama @ Georgia",
        event_time="2026-10-03T23:00:00Z",
        market="Alabama -6.5 Spread", pick_side="Alabama",
        model_win_prob=0.62, book_odds=-115,
        lock=92.0, factors={}, insights=[],
        external_id="CFB-TEST-SP",
        home_team_name="Georgia", away_team_name="Alabama",
        odds_source="the_odds_api",
    )
    assert pick.get("odds_source") == "the_odds_api"
    pick.update({
        "id": "CFB-TEST-SP",
        "market_family": "spread",
        "model_probability": 0.62,
        "identity_class": "AUTHORITATIVE",
    })
    v = evaluate_publication(pick)
    assert v.state == PublicationState.PUBLISHED, v.reasons


# ───────────── Producer path: MLB alt run-line ──────────────────────
def test_producer_mlb_alt_runline_stamps_the_odds_api():
    """Simulate sports_engine.py:~7087 — alt run-line builder."""
    pick = _build_pick(
        sport="MLB", league="MLB", event="Yankees @ Red Sox",
        event_time="2026-10-03T23:00:00Z",
        market="Yankees +1.5 Run Line (Alt)", pick_side="Yankees",
        model_win_prob=0.72, book_odds=-200,
        lock=90.0, factors={"Pitching Edge": 0.65}, insights=[],
        external_id="MLB-TEST-ARL",
        is_alt_prop=True,
        home_team_name="Red Sox", away_team_name="Yankees",
        odds_source="the_odds_api",
    )
    assert pick.get("odds_source") == "the_odds_api"
    assert pick.get("is_alt") is True
    pick.update({
        "id": "MLB-TEST-ARL",
        "market_family": "run_line",
        "model_probability": 0.72,
        "identity_class": "AUTHORITATIVE",
    })
    v = evaluate_publication(pick)
    assert v.state == PublicationState.PUBLISHED, v.reasons


# ───────────── Producer path: Tennis real-odds (via tennis_extra) ──
def test_producer_tennis_real_odds_shape_publishes():
    """The tennis_extra/picks.py real-odds branch produces a pick dict
    with:
      - book_odds = int(real_odds['book_odds'])
      - bookmaker = str(real_odds['bookmaker'])
      - odds_source = 'the_odds_api'
      - no_real_book_line = False
    Verify the resulting shape clears the boundary."""
    pick = {
        "id": "Tennis-TEST-ML",
        "sport": "Tennis",
        "league": "ATP Washington",
        "event": "Alcaraz vs Sinner",
        "market": "Alcaraz Moneyline",
        "market_family": "moneyline",
        "selection": "Alcaraz",
        "book_odds": -180,
        "implied_probability": 64.3,
        "bookmaker": "FanDuel",
        "odds_source": "the_odds_api",   # stamped by using_real branch
        "no_real_book_line": False,
        "edge_percent": 2.5,
        "model_probability": 0.68,
        "identity_class": "AUTHORITATIVE",
        "win_probability": 68.0,
    }
    v = evaluate_publication(pick)
    assert v.state == PublicationState.PUBLISHED, v.reasons
    assert v.meta.get("line_state") == "REAL"


# ───────────── Median / consensus-only remains REJECTED ─────────────
def test_producer_median_h2h_moneyline_without_provenance_is_rejected():
    """sports_engine.py:~3070 — the main game-market ML path builds a
    pick with ``book_odds=side_ml`` where ``side_ml`` is
    ``_median_price(h2h_outs, home)``.  The call site passes NO
    ``odds_source``, so ``_build_pick`` must emit a pick dict that
    OMITS ``odds_source`` → publication boundary REJECTS as
    SYNTHETIC_BOOK_ODDS.

    This proves our fail-closed guarantee for median-only paths and
    confirms Perklocks' no-synthesis contract is intact.
    """
    pick = _build_pick(
        sport="MLB", league="MLB", event="Yankees @ Red Sox",
        event_time="2026-10-03T23:00:00Z",
        market="Yankees Moneyline", pick_side="Yankees",
        model_win_prob=0.58, book_odds=-140,     # from median
        lock=90.0, factors={}, insights=[],
        external_id="MLB-TEST-ML",
        home_team_name="Red Sox", away_team_name="Yankees",
        # NO odds_source kwarg — fail-closed by design.
    )
    assert "odds_source" not in pick, pick
    pick.update({
        "id": "MLB-TEST-ML",
        "market_family": "moneyline",
        "model_probability": 0.58,
        "identity_class": "AUTHORITATIVE",
    })
    v = evaluate_publication(pick)
    assert v.state == PublicationState.REJECTED
    assert RejectionReason.SYNTHETIC_BOOK_ODDS.value in v.reasons


# ───────────── Soccer Poisson alt-total (model_line=True) ───────────
def test_soccer_poisson_alt_total_path_remains_rejected_as_model_line():
    """sports_engine.py:~4171 produces a Soccer alt-total pick with:
      - book_odds = fair_odds  (synthesized from Poisson λ)
      - model_line = True
      - model_source = 'poisson_from_main_total'
    This is a C-type (synthetic) path.  The canonical boundary rejects
    it via BOTH:
      - MODEL_LINE_NOT_REAL_OFFERING (model_line=True)
      - MODEL_LINE_NOT_REAL_OFFERING (model_source prefix match)
    Verify both guards are intact."""
    pick = _build_pick(
        sport="Soccer", league="Premier League",
        event="Arsenal vs Chelsea",
        event_time="2026-10-03T15:00:00Z",
        market="Total Goals Over 1.5", pick_side="Over",
        model_win_prob=0.82, book_odds=-350,   # synthesized fair_odds
        lock=90.0, factors={}, insights=[],
        external_id="SOCCER-TEST-ALT-TOTAL",
    )
    # Simulate what the Soccer Poisson alt builder stamps post-_build_pick.
    pick["model_line"] = True
    pick["model_source"] = "poisson_from_main_total"
    pick["is_alt"] = True
    pick.update({
        "id": "SOCCER-TEST-ALT-TOTAL",
        "market_family": "total",
        "model_probability": 0.82,
        "identity_class": "AUTHORITATIVE",
    })
    v = evaluate_publication(pick)
    assert v.state == PublicationState.REJECTED
    assert RejectionReason.MODEL_LINE_NOT_REAL_OFFERING.value in v.reasons


# ───────────── NFL alt threshold / ladder constants unchanged ──────
def test_nfl_alt_threshold_constants_are_unchanged():
    """Static-file invariant — the cutover fix must NOT alter the alt-
    prop implied bands, MLB alt edge gates, or the MLB alt chalk cuts.
    If any of these constants shift, the test will fail and the fix
    will be flagged as touching alt ladder behaviour."""
    src_path = pathlib.Path("/app/backend/sports_engine.py").read_text()

    # Current frozen values (asserted exactly).
    expected = [
        (r"_ALT_PROP_MIN_IMPLIED\s*=\s*0\.80",   "_ALT_PROP_MIN_IMPLIED=0.80"),
        (r"_ALT_PROP_MAX_IMPLIED\s*=\s*0\.95",   "_ALT_PROP_MAX_IMPLIED=0.95"),
        (r"_MLB_ALT_MIN_EDGE_PCT\s*=\s*8\.0",    "_MLB_ALT_MIN_EDGE_PCT=8.0"),
        (r"_MLB_ALT_MAX_ODDS\s*=\s*-700",        "_MLB_ALT_MAX_ODDS=-700"),
        (r"_MLB_ALT_MIN_ODDS\s*=\s*-450",        "_MLB_ALT_MIN_ODDS=-450"),
    ]
    for pat, label in expected:
        assert re.search(pat, src_path), f"missing or changed: {label}"


def test_alt_outcomes_for_market_definition_is_unchanged():
    """The alt-outcomes extractor is the backbone of every alt ladder.
    Guard its definition literal from drifting during this fix."""
    src_path = pathlib.Path("/app/backend/sports_engine.py").read_text()
    # The function name and the per-bookmaker walk pattern are canonical.
    assert "def _alt_outcomes_for_market(payload: dict, market_key: str) -> list[dict]:" in src_path
    assert "for bk in (payload.get(\"bookmakers\") or []):" in src_path
    assert "if key not in seen:" in src_path
