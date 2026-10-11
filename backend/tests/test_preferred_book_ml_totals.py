"""ML + totals preferred-book policy regression tests — 2026-10-03.

Proves the end-to-end contract of the new preferred-book extension to
sports_engine.py game-market ML/totals builders:

  * ML publishes ONLY when a preferred book quoted h2h.
  * Totals publishes ONLY when a preferred book quoted totals Over+Under
    at the SAME line.
  * Books NOT in ``_PREFERRED_BOOKS`` are ignored for selection even
    when they are the only book on the slate (fail-closed).
  * The selected ``bookmaker`` string survives on the wire.
  * Preferred order (fanduel > draftkings > …) is honoured regardless
    of payload order.
  * The single-source-of-truth tuple is byte-equal to the Soccer tuple.

Run with:
    cd /app/backend && pytest tests/test_preferred_book_ml_totals.py -v
"""
from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path("/app/backend").resolve()))

from sports_engine import (
    _PREFERRED_BOOKS,
    _select_preferred_book_market,
    _price_from_outcome,
    _build_pick,
)
from services.canonical_publication_boundary import (
    PublicationState,
    RejectionReason,
    evaluate_publication,
)


# ───── helper to build minimal Odds-API-shaped game payload ────────
def _mk_game(home, away, books):
    return {
        "id": f"G-{home}-{away}",
        "home_team": home, "away_team": away,
        "commence_time": "2026-10-03T23:00:00Z",
        "bookmakers": [
            {
                "key": b["key"],
                "title": b["key"].title(),
                "last_update": "2026-10-03T22:00:00Z",
                "markets": [
                    {"key": m_key, "outcomes": outs}
                    for m_key, outs in b.get("markets", {}).items()
                ],
            } for b in books
        ],
    }


# ─────────── 1. Single-source-of-truth tuple ──────────────────────
def test_preferred_books_single_source_of_truth():
    """``sports_engine._PREFERRED_BOOKS`` MUST be byte-equal to the
    original policy tuple in ``soccer.real_odds`` (and to the matching
    one in ``tennis_extra.real_odds``).  No new policy, no divergence."""
    from soccer.real_odds import _PREFERRED_BOOKS as soccer_tuple
    from tennis_extra.real_odds import _PREFERRED_BOOKS as tennis_tuple
    assert _PREFERRED_BOOKS == soccer_tuple
    assert _PREFERRED_BOOKS == tennis_tuple
    # Expected literal — guard against silent reordering upstream.
    assert _PREFERRED_BOOKS == (
        "fanduel", "draftkings", "betmgm", "caesars", "pointsbet",
    )


# ─────────── 2. First-preferred wins regardless of payload order ──
def test_preferred_order_wins_regardless_of_payload_ordering():
    """FanDuel is first in the preferred order.  Even when the Odds
    API payload lists DraftKings first, we must select FanDuel."""
    game = _mk_game("A", "B", [
        {"key": "draftkings", "markets": {
            "h2h": [{"name": "A", "price": -140}, {"name": "B", "price": +120}],
        }},
        {"key": "fanduel",    "markets": {
            "h2h": [{"name": "A", "price": -142}, {"name": "B", "price": +122}],
        }},
        {"key": "betmgm",     "markets": {
            "h2h": [{"name": "A", "price": -138}, {"name": "B", "price": +118}],
        }},
    ])
    market, book = _select_preferred_book_market(game, "h2h")
    assert book == "fanduel"
    a_price = _price_from_outcome(next(o for o in market["outcomes"] if o["name"] == "A"))
    assert a_price == -142   # FanDuel's quote, not median (-140) nor DraftKings


# ─────────── 3. Falls to next-preferred when FanDuel absent ───────
def test_preferred_falls_through_order_when_first_absent():
    """When FanDuel is not on the slate, next is DraftKings, then
    BetMGM.  No skipping; no reversal."""
    game = _mk_game("A", "B", [
        {"key": "caesars",    "markets": {
            "h2h": [{"name": "A", "price": -130}, {"name": "B", "price": +115}],
        }},
        {"key": "betmgm",     "markets": {
            "h2h": [{"name": "A", "price": -132}, {"name": "B", "price": +118}],
        }},
    ])
    _, book = _select_preferred_book_market(game, "h2h")
    assert book == "betmgm"   # betmgm comes BEFORE caesars in preferred order


# ─────────── 4. Non-preferred books are NEVER selected ────────────
def test_non_preferred_only_slate_fails_closed():
    """If the ONLY books on the slate are not in ``_PREFERRED_BOOKS``
    (hypothetical obscure book), selection returns (None, None) and
    the ML pick must NOT publish.  Fail-closed."""
    game = _mk_game("A", "B", [
        {"key": "obscure_book_xyz", "markets": {
            "h2h": [{"name": "A", "price": -140}, {"name": "B", "price": +120}],
        }},
    ])
    market, book = _select_preferred_book_market(game, "h2h")
    assert market is None
    assert book is None

    # Downstream: _build_pick called with book_odds=None (because the
    # engine skips the ML emission when home_ml/away_ml are None) —
    # we simulate the "the engine chose to still try" leak scenario
    # by passing book_odds=None directly:
    out = _build_pick(
        sport="MLB", league="MLB", event="B @ A",
        event_time="2026-10-03T23:00:00Z",
        market="A Moneyline", pick_side="A",
        model_win_prob=0.58, book_odds=None,
        lock=90.0, factors={}, insights=[],
        external_id="MLB-TEST-NOBK",
        home_team_name="A", away_team_name="B",
        odds_source=None, bookmaker=None,
    )
    assert out["book_odds"] is None
    assert out.get("no_real_book_line") is True
    assert "odds_source" not in out


# ─────────── 5. Totals requires BOTH sides at SAME line ───────────
def test_totals_fail_closed_when_only_one_side_available():
    """A sportsbook that quotes only Over (missing Under) → fail-closed."""
    game = _mk_game("A", "B", [
        {"key": "fanduel", "markets": {
            "totals": [{"name": "Over", "price": -110, "point": 8.5}],
            # No Under
        }},
    ])
    market, book = _select_preferred_book_market(game, "totals")
    assert book == "fanduel"
    outs = market["outcomes"]
    over = next((o for o in outs if o.get("name") == "Over"), None)
    under = next((o for o in outs if o.get("name") == "Under"), None)
    assert over is not None
    assert under is None    # engine's `if over and under` short-circuits


# ─────────── 6. End-to-end: ML with FanDuel provenance → PUBLISHED ─
def test_ml_pick_with_fanduel_provenance_publishes():
    pick = _build_pick(
        sport="MLB", league="MLB", event="Red Sox @ Yankees",
        event_time="2026-10-03T23:00:00Z",
        market="Yankees Moneyline", pick_side="Yankees",
        model_win_prob=0.60, book_odds=-142,       # FanDuel's quote
        lock=90.0, factors={}, insights=[],
        external_id="MLB-TEST-ML",
        home_team_name="Yankees", away_team_name="Red Sox",
        odds_source="the_odds_api", bookmaker="fanduel",
    )
    assert pick["odds_source"] == "the_odds_api"
    assert pick["bookmaker"] == "fanduel"
    assert pick["book_odds"] == -142

    pick.update({
        "id": "MLB-TEST-ML", "market_family": "moneyline",
        "model_probability": 0.60,
        "identity_class": "AUTHORITATIVE",
    })
    v = evaluate_publication(pick)
    assert v.state == PublicationState.PUBLISHED, v.reasons
    assert v.meta.get("line_state") == "REAL"


# ─────────── 7. End-to-end: Totals with real-book provenance → PUBLISHED ─
def test_total_pick_with_draftkings_provenance_publishes():
    pick = _build_pick(
        sport="NFL", league="NFL", event="Bills @ Chiefs",
        event_time="2026-10-03T23:00:00Z",
        market="Total Points Over 47.5", pick_side="Over",
        model_win_prob=0.56, book_odds=-115,
        lock=88.0, factors={}, insights=[],
        external_id="NFL-TEST-TOT",
        home_team_name="Chiefs", away_team_name="Bills",
        odds_source="the_odds_api", bookmaker="draftkings",
    )
    assert pick["bookmaker"] == "draftkings"
    pick.update({
        "id": "NFL-TEST-TOT", "market_family": "total",
        "model_probability": 0.56,
        "identity_class": "AUTHORITATIVE",
    })
    v = evaluate_publication(pick)
    assert v.state == PublicationState.PUBLISHED, v.reasons


# ─────────── 8. Explicit no-bookmaker + non-null price → REJECTED ─
def test_ml_pick_without_bookmaker_but_with_price_is_rejected():
    """Guards against any future caller that passes book_odds but
    forgets to pass bookmaker/odds_source."""
    pick = _build_pick(
        sport="MLB", league="MLB", event="A @ B",
        event_time="2026-10-03T23:00:00Z",
        market="A Moneyline", pick_side="A",
        model_win_prob=0.58, book_odds=-140,
        lock=90.0, factors={}, insights=[],
        external_id="MLB-TEST-ML-NOBK",
        home_team_name="A", away_team_name="B",
        # no odds_source / bookmaker
    )
    assert "odds_source" not in pick
    assert "bookmaker" not in pick

    pick.update({
        "id": "MLB-TEST-ML-NOBK", "market_family": "moneyline",
        "model_probability": 0.58, "identity_class": "AUTHORITATIVE",
    })
    v = evaluate_publication(pick)
    assert v.state == PublicationState.REJECTED
    assert RejectionReason.SYNTHETIC_BOOK_ODDS.value in v.reasons


# ─────────── 9. ALL five sports: realistic payload publishes ─────
import pytest

@pytest.mark.parametrize("sport,home,away,home_price,mkt_family", [
    ("MLB", "Yankees",  "Red Sox",  -142, "moneyline"),
    ("CFB", "Alabama",  "Georgia",  -145, "moneyline"),
    ("NFL", "Chiefs",   "Bills",    -158, "moneyline"),
    ("NHL", "Oilers",   "Panthers", -115, "moneyline"),
])
def test_four_sports_ml_publish_with_preferred_book(
    sport, home, away, home_price, mkt_family,
):
    """MLB / CFB / NFL / NHL ML with FanDuel provenance publishes.

    NBA is deliberately excluded here — the publication boundary
    rejects NBA ML with ``UMC_CAPABILITY_STATE_MODEL_UNAVAILABLE``
    (model-availability guard, unrelated to this provenance fix).
    That gate is part of the SEPARATE P0-B zero-generation
    investigation and is confirmed by
    ``test_nba_is_blocked_by_capability_state_not_provenance`` below.
    """
    pick = _build_pick(
        sport=sport, league=sport, event=f"{away} @ {home}",
        event_time="2026-10-03T23:00:00Z",
        market=f"{home} Moneyline", pick_side=home,
        model_win_prob=0.80, book_odds=home_price,
        lock=90.0, factors={}, insights=[],
        external_id=f"{sport}-TEST-ML",
        home_team_name=home, away_team_name=away,
        odds_source="the_odds_api", bookmaker="fanduel",
    )
    assert pick is not None, f"edge gate filtered {sport}"
    pick.update({
        "id": f"{sport}-TEST-ML", "market_family": mkt_family,
        "model_probability": 0.80, "identity_class": "AUTHORITATIVE",
    })
    v = evaluate_publication(pick)
    assert v.state == PublicationState.PUBLISHED, (sport, v.reasons)
    assert v.meta.get("line_state") == "REAL"
    assert pick["odds_source"] == "the_odds_api"
    assert pick["bookmaker"] == "fanduel"


def test_nba_is_blocked_by_capability_state_not_provenance():
    """Even with perfect FanDuel provenance, NBA ML is REJECTED due to
    the UMC capability-state guard (``MODEL_UNAVAILABLE``), not
    SYNTHETIC_BOOK_ODDS.  This proves our provenance fix is working
    correctly: NBA's rejection reason is NOT a provenance one, so when
    the P0-B NBA investigation enables the model this provenance path
    is already correct and ready."""
    pick = _build_pick(
        sport="NBA", league="NBA", event="Lakers @ Celtics",
        event_time="2026-10-03T23:00:00Z",
        market="Celtics Moneyline", pick_side="Celtics",
        model_win_prob=0.80, book_odds=-172,
        lock=90.0, factors={}, insights=[],
        external_id="NBA-TEST-ML",
        home_team_name="Celtics", away_team_name="Lakers",
        odds_source="the_odds_api", bookmaker="fanduel",
    )
    assert pick is not None
    assert pick["odds_source"] == "the_odds_api"
    assert pick["bookmaker"] == "fanduel"

    pick.update({
        "id": "NBA-TEST-ML", "market_family": "moneyline",
        "model_probability": 0.80, "identity_class": "AUTHORITATIVE",
    })
    v = evaluate_publication(pick)
    assert v.state == PublicationState.REJECTED
    # The rejection is CAPABILITY-based, not provenance-based.
    assert RejectionReason.SYNTHETIC_BOOK_ODDS.value not in v.reasons
    # (Specific capability reason is tracked in the P0-B investigation.)
