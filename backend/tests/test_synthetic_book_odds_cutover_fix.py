"""Regression tests — 2026-10-03 SYNTHETIC_BOOK_ODDS cutover fix.

Context
───────
Production Picks Board was flickering (Soccer) and empty
(MLB/CFB/Tennis) because the canonical publication boundary rejected
every new pick as ``SYNTHETIC_BOOK_ODDS``:

  * Soccer stamps ``odds_source="real_book_line"`` but the label was
    not in ``_REAL_ODDS_SOURCES``.
  * MLB / CFB real-sportsbook picks (via ``sports_engine._build_pick``)
    carried a real Odds-API ``book_odds`` but no ``odds_source`` at
    all → classified SYNTHETIC.
  * Tennis real-odds branch stamped only ``source="tennis_real_odds"``
    (a key the boundary does not read) and relied on ``bookmaker``
    lowercase-matching the small whitelist, which failed for most
    sportsbook name formats.

These tests exercise the smallest-safe fix:
  * Boundary recognises ``real_book_line`` as a real sportsbook
    source (Soccer unlocks).
  * ``_build_pick`` stamps ``odds_source="the_odds_api"`` when a
    real price survives cleaning (MLB/CFB unlock through the shared
    path).
  * Tennis real-odds branch stamps ``odds_source="the_odds_api"``.
  * Synthetic-default (-110 with no provenance) remains REJECTED.
  * Missing provenance is NOT rescued merely because ``book_odds``
    is non-null.
"""
from __future__ import annotations

import pytest

from services.canonical_publication_boundary import (
    PublicationState,
    RejectionReason,
    evaluate_publication,
    _REAL_ODDS_SOURCES,
)


# ───────────────────────── helpers ─────────────────────────────────
def _base_pick(**overrides) -> dict:
    """Build a boundary-eligible pick stub.

    Supplies every unrelated field the boundary needs so a single
    rule-under-test can be isolated.  Any ``overrides`` override the
    defaults (used by individual tests to flip the fields they target).
    """
    pick = {
        "id": "test-pick-id",
        "sport": "MLB",
        "league": "MLB",
        "event": "A @ B",
        "market": "A Moneyline",
        "market_family": "moneyline",
        "selection": "A",
        "book_odds": -125,
        "odds_source": "the_odds_api",
        "model_probability": 0.58,
        "identity_class": "AUTHORITATIVE",
        "edge_percent": 2.5,
        "win_probability": 58.0,
    }
    pick.update(overrides)
    return pick


# ─────────────────── A. Soccer real_book_line ──────────────────────
def test_soccer_real_book_line_is_published():
    """Soccer picks stamped with ``odds_source='real_book_line'`` (the
    canonical label from ``services.real_line_scorer_ingest``) MUST be
    recognised as REAL and PUBLISHED.
    """
    pick = _base_pick(
        sport="Soccer",
        league="Premier League",
        market="Match Winner",
        market_family="moneyline",
        odds_source="real_book_line",
        book_odds=+135,
        no_real_book_line=False,
        bookmaker="FanDuel",
    )
    verdict = evaluate_publication(pick)
    assert verdict.state == PublicationState.PUBLISHED, verdict.reasons
    assert verdict.meta.get("line_state") == "REAL"
    # Ensure the label is explicitly in the real-sources set now.
    assert "real_book_line" in _REAL_ODDS_SOURCES


# ─────────────────── B. MLB via sports_engine ──────────────────────
def test_mlb_the_odds_api_is_published():
    """MLB picks built by ``sports_engine._build_pick`` now stamp
    ``odds_source='the_odds_api'`` whenever a real price survived
    cleaning.  The boundary must accept them as REAL."""
    pick = _base_pick(
        sport="MLB",
        market="Yankees Moneyline",
        market_family="moneyline",
        odds_source="the_odds_api",
        bookmaker="DraftKings",
        book_odds=-140,
    )
    verdict = evaluate_publication(pick)
    assert verdict.state == PublicationState.PUBLISHED, verdict.reasons
    assert verdict.meta.get("line_state") == "REAL"


# ─────────────────── C. CFB via sports_engine ──────────────────────
def test_cfb_the_odds_api_spread_is_published():
    """CFB picks share the ``_build_pick`` return path; same
    ``odds_source='the_odds_api'`` stamp applies.  Previously CFB
    rows carried no odds_source → SYNTHETIC."""
    pick = _base_pick(
        sport="CFB",
        league="NCAAF",
        market="Alabama -6.5 Spread",
        market_family="spread",
        odds_source="the_odds_api",
        bookmaker="FanDuel",
        book_odds=-110,
    )
    verdict = evaluate_publication(pick)
    assert verdict.state == PublicationState.PUBLISHED, verdict.reasons
    assert verdict.meta.get("line_state") == "REAL"


# ─────────────────── D. Tennis real_odds branch ────────────────────
def test_tennis_real_odds_branch_is_published():
    """Tennis real-odds branch stamps ``odds_source='the_odds_api'``
    (plus ``bookmaker=<real sportsbook>``) when a FanDuel/DK price is
    observed via the live odds ingest."""
    pick = _base_pick(
        sport="Tennis",
        league="ATP Washington",
        event="Alcaraz vs Sinner",
        market="Alcaraz Moneyline",
        market_family="moneyline",
        selection="Alcaraz",
        odds_source="the_odds_api",
        bookmaker="FanDuel",
        book_odds=-180,
        no_real_book_line=False,
    )
    verdict = evaluate_publication(pick)
    assert verdict.state == PublicationState.PUBLISHED, verdict.reasons
    assert verdict.meta.get("line_state") == "REAL"


# ─────────────────── E. Synthetic -110 default is REJECTED ─────────
def test_synthetic_minus_110_without_provenance_is_rejected():
    """Legacy symptom — a ``-110`` price stamped with NO sportsbook
    provenance must remain REJECTED as SYNTHETIC_BOOK_ODDS.  The fix
    adds trusted labels but does not loosen the fail-closed path."""
    pick = _base_pick(
        sport="NFL",
        market="Chiefs -3.5 Spread",
        market_family="spread",
        book_odds=-110,
        odds_source="",      # empty / unknown
        bookmaker="",
    )
    pick.pop("odds_source", None)
    pick.pop("bookmaker", None)
    verdict = evaluate_publication(pick)
    assert verdict.state == PublicationState.REJECTED
    assert RejectionReason.SYNTHETIC_BOOK_ODDS.value in verdict.reasons


def test_model_derived_label_with_book_odds_is_rejected():
    """A pick that explicitly labels its provenance as model-derived
    MUST be REJECTED even when ``book_odds`` is non-null.  Verifies
    we did not accidentally loosen ``_real_line_state`` for the
    synthetic label set."""
    pick = _base_pick(odds_source="model_derived", book_odds=-110)
    verdict = evaluate_publication(pick)
    assert verdict.state == PublicationState.REJECTED
    assert RejectionReason.SYNTHETIC_BOOK_ODDS.value in verdict.reasons


# ─────────────────── F. Missing provenance NOT rescued ─────────────
def test_missing_odds_source_with_real_price_still_rejected():
    """Fail-closed invariant from 2026-10-02 P0: ``book_odds`` present
    but ``odds_source`` MISSING / UNKNOWN must be REJECTED.  The
    cutover fix adds ``real_book_line`` + ``the_odds_api`` tagging —
    it must NOT rescue unlabeled rows."""
    pick = _base_pick(book_odds=-140)
    # Strip every provenance key.
    for k in ("odds_source", "provider", "bookmaker"):
        pick.pop(k, None)
    verdict = evaluate_publication(pick)
    assert verdict.state == PublicationState.REJECTED
    assert RejectionReason.SYNTHETIC_BOOK_ODDS.value in verdict.reasons


def test_unknown_provenance_string_still_rejected():
    """An unknown non-whitelisted provenance string (e.g. a producer
    that invented its own label) must remain REJECTED."""
    pick = _base_pick(odds_source="mystery_tagged_producer_v3",
                      book_odds=+145)
    pick.pop("bookmaker", None)
    verdict = evaluate_publication(pick)
    assert verdict.state == PublicationState.REJECTED
    assert RejectionReason.SYNTHETIC_BOOK_ODDS.value in verdict.reasons


# ─────────────────── G. Fallback MODEL_ONLY still accepted ─────────
def test_no_real_book_line_with_null_odds_is_published_model_only():
    """Contract 1(b) — ``no_real_book_line=True`` AND ``book_odds=None``
    is the explicit MODEL_ONLY shape, allowed to publish (frontend
    just can't show an edge).  Tennis fallback branch produces this
    shape; verify it still clears the boundary."""
    pick = _base_pick(
        sport="Tennis",
        event="Alcaraz vs Sinner",
        market="Alcaraz Moneyline",
        selection="Alcaraz",
        book_odds=None,
        no_real_book_line=True,
        odds_source=None,
        bookmaker="TennisExplorer (no book line)",
        edge_percent=None,
    )
    # Boundary reads top-level odds_source; a Python None would fail
    # the str check, so just drop the key when there is no provenance.
    pick.pop("odds_source", None)
    verdict = evaluate_publication(pick)
    assert verdict.state == PublicationState.PUBLISHED, verdict.reasons
    assert verdict.meta.get("line_state") == "MODEL_ONLY"


# ─────────────────── H. _build_pick provenance plumbing ────────────
# Direct unit tests on ``sports_engine._build_pick`` prove that:
#   (1) a call WITHOUT ``odds_source`` yields a pick that OMITS
#       ``odds_source`` entirely (fail-closed at the boundary) — even
#       when ``book_odds`` is real.
#   (2) a call WITH ``odds_source='the_odds_api'`` stamps the field
#       exactly when ``final_odds`` survived cleaning.
#   (3) a call where ``book_odds`` was nulled by cleaning (``None``)
#       NEVER carries ``odds_source`` even if the caller passed one —
#       no_real_book_line=True is the only state stamped.
def test_build_pick_without_odds_source_omits_field_and_is_rejected():
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path("/app/backend").resolve()))
    from sports_engine import _build_pick

    out = _build_pick(
        sport="MLB", league="MLB", event="Yankees @ Red Sox",
        event_time="2026-10-03T23:00:00Z",
        market="Yankees Moneyline", pick_side="Yankees",
        model_win_prob=0.58, book_odds=-140,
        lock=90.0, factors={}, insights=[],
        external_id="MLB-TEST-ML",
        home_team_name="Red Sox", away_team_name="Yankees",
    )
    # odds_source MUST be absent — caller did not pass evidence.
    assert "odds_source" not in out, out
    assert out["book_odds"] == -140
    assert out.get("no_real_book_line") is not True

    # Now feed the row through the boundary — it must be REJECTED
    # as SYNTHETIC_BOOK_ODDS because provenance is missing.
    out.update({
        "id": "MLB-TEST-ML",
        "model_probability": 0.58,
        "identity_class": "AUTHORITATIVE",
    })
    verdict = evaluate_publication(out)
    assert verdict.state == PublicationState.REJECTED
    assert RejectionReason.SYNTHETIC_BOOK_ODDS.value in verdict.reasons


def test_build_pick_with_explicit_odds_source_stamps_and_is_published():
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path("/app/backend").resolve()))
    from sports_engine import _build_pick

    out = _build_pick(
        sport="CFB", league="NCAAF", event="Alabama @ Georgia",
        event_time="2026-10-03T23:00:00Z",
        market="Alabama -6.5 Spread", pick_side="Alabama",
        model_win_prob=0.62, book_odds=-110,
        lock=90.0, factors={}, insights=[],
        external_id="CFB-TEST-SP",
        home_team_name="Georgia", away_team_name="Alabama",
        odds_source="the_odds_api",
    )
    assert out.get("odds_source") == "the_odds_api"
    assert out["book_odds"] == -110

    out.update({
        "id": "CFB-TEST-SP",
        "market_family": "spread",
        "model_probability": 0.62,
        "identity_class": "AUTHORITATIVE",
    })
    verdict = evaluate_publication(out)
    assert verdict.state == PublicationState.PUBLISHED, verdict.reasons
    assert verdict.meta.get("line_state") == "REAL"


def test_build_pick_nulled_price_never_carries_odds_source():
    """When ``book_odds`` is nulled by sanity cleaning (e.g. absurd
    chalk ``-5000``), the final pick MUST carry ``book_odds=None``
    AND ``no_real_book_line=True`` AND NO ``odds_source`` key — even
    if the caller passed ``odds_source='the_odds_api'``.  This proves
    the clamping is honest: a real-provenance-tagged pick that fails
    the sanity bound is NOT laundered as a real-line row.
    """
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path("/app/backend").resolve()))
    from sports_engine import _build_pick

    out = _build_pick(
        sport="MLB", league="MLB", event="A @ B",
        event_time="2026-10-03T23:00:00Z",
        market="A Moneyline", pick_side="A",
        model_win_prob=0.90, book_odds=-5000,   # over -1000 → nulled
        lock=90.0, factors={}, insights=[],
        external_id="MLB-TEST-ML-ABSURD",
        home_team_name="B", away_team_name="A",
        odds_source="the_odds_api",             # provenance passed
    )
    # Even with provenance passed, nulled price → no odds_source stamp.
    assert out["book_odds"] is None
    assert out.get("no_real_book_line") is True
    assert "odds_source" not in out, out
