"""NHL FanDuel exact-wager-identity regression tests.

Proves the 2026-10 one-pass fix: an NHL pick's attached odds MUST
come from a FanDuel quote whose full wager identity equals the pick's
exactly — event + player + market + side + exact line + book=fanduel.

Six collision/refusal scenarios + two must-pass identity assertions:
  (a) mixed Over 1.5 + Over 2.5 bucket, pick wants Over 2.5 →
      ONLY the 2.5 price attaches.
  (b) mixed Over + Under bucket, pick wants Over →
      Under refused.
  (c) FanDuel missing, DK/BetMGM/Caesars present → attach refuses
      (no non-FanDuel fallback).
  (d) wrong event_id → refused.
  (e) wrong player → refused.
  (f) same (player, market, side, line) but TWO different events
      → quote from the OTHER event never attaches.
  (g) the actual selected quote OBJECT carries the matching
      FanDuel identity (not merely the pick's own fields after
      attachment).

Guardrails:
  * NHL-only — the tests also assert non-NHL (MLB) behavior is
    UNCHANGED by this patch.
  * No scoring/formula changes.
  * No frontend changes.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from services.observed_quote_adapter import (
    extract_observed_quotes, quotes_for, _canon_line,
)
from services.observed_quote import (
    select_exact_fanduel_quote, PREFERRED_BOOK_ORDER,
)
from services.quote_lineage_guard import harden_lineage
from services.prepublication_lineage_pass import _upgrade_pick_lineage


# ───────────────────────────── helpers ─────────────────────────────

_OBSERVED_AT = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _mk_payload(bookmakers: list[dict]) -> dict:
    return {"last_update": _OBSERVED_AT, "bookmakers": bookmakers}


def _book(key: str, markets: list[dict]) -> dict:
    return {"key": key, "title": key.title(), "markets": markets}


def _market(mk: str, outcomes: list[dict]) -> dict:
    return {"key": mk, "outcomes": outcomes}


def _outcome(side: str, player: str, point, price: int) -> dict:
    o = {"name": side, "description": player, "price": price}
    if point is not None:
        o["point"] = point
    return o


def _nhl_pick(event_id: str, player: str, market: str,
              side: str, line, book_hint: str = "fanduel") -> dict:
    return {
        "sport":                "NHL",
        "event_id":             event_id,
        "provider_event_id":    event_id,
        "provider_market_key":  market,
        "market":               market,
        "player_name":          player,
        "pick_side":            side,
        "line":                 line,
        "bookmaker":            book_hint,
    }


# ──────────────────── Scenario (a) CROSS-LINE ─────────────────────

def test_a_cross_line_collision_resolved():
    """Over 1.5 and Over 2.5 live in SEPARATE buckets after the fix;
    a pick for Over 2.5 attaches ONLY the +146 price, never the -170
    from Over 1.5."""
    payload = _mk_payload([
        _book("fanduel", [
            _market("player_shots_on_goal", [
                _outcome("Over",  "John McDoe", 1.5, -170),
                _outcome("Over",  "John McDoe", 2.5,  146),
                _outcome("Under", "John McDoe", 2.5, -180),
            ]),
        ]),
    ])
    extracted = extract_observed_quotes(
        payload, sport="NHL", event_id="EVENT_X",
        allowed_markets=["player_shots_on_goal"],
    )

    # Line IS part of the bucket key now.
    buckets = [k for k in extracted.keys() if k[0] == "player_shots_on_goal"]
    assert any(k[-1] == 2.5 for k in buckets), buckets
    assert any(k[-1] == 1.5 for k in buckets), buckets

    only_2_5 = quotes_for(extracted,
                           market_key="player_shots_on_goal",
                           player="John McDoe", side="over",
                           line=2.5, bookmaker="fanduel",
                           require_line_match=True)
    assert len(only_2_5) == 1
    assert only_2_5[0]["price"] == 146
    assert only_2_5[0]["line"] == 2.5


# ──────────────────── Scenario (b) CROSS-SIDE ─────────────────────

def test_b_cross_side_collision_resolved():
    """Over + Under at the same line do NOT collapse into one bucket;
    a pick for Over 2.5 never gets the Under -180 price."""
    payload = _mk_payload([
        _book("fanduel", [
            _market("player_shots_on_goal", [
                _outcome("Over",  "John McDoe", 2.5,  146),
                _outcome("Under", "John McDoe", 2.5, -180),
            ]),
        ]),
    ])
    extracted = extract_observed_quotes(
        payload, sport="NHL", event_id="EVENT_X",
        allowed_markets=["player_shots_on_goal"],
    )
    over_only = quotes_for(extracted,
                            market_key="player_shots_on_goal",
                            player="John McDoe", side="over",
                            line=2.5, bookmaker="fanduel",
                            require_line_match=True)
    assert len(over_only) == 1
    assert over_only[0]["side"] == "over"
    assert over_only[0]["price"] == 146


# ────────────── Scenario (c) NON-FANDUEL FALLBACK FORBIDDEN ───────

def test_c_non_fanduel_fallback_refused():
    """When FanDuel is missing from the payload, the strict NHL
    selector must refuse to publish — DK/BetMGM/Caesars are NOT
    fallback books for NHL."""
    payload = _mk_payload([
        _book("draftkings", [
            _market("player_shots_on_goal", [
                _outcome("Over", "John McDoe", 2.5, 150),
            ]),
        ]),
        _book("betmgm", [
            _market("player_shots_on_goal", [
                _outcome("Over", "John McDoe", 2.5, 155),
            ]),
        ]),
    ])
    extracted = extract_observed_quotes(
        payload, sport="NHL", event_id="EVENT_X",
        allowed_markets=["player_shots_on_goal"],
    )
    quotes = quotes_for(extracted,
                         market_key="player_shots_on_goal",
                         player="John McDoe", side="over",
                         line=2.5, bookmaker="fanduel",
                         require_line_match=True)
    assert quotes == []

    # And the strict selector refuses even with the raw mixed list.
    all_rows = []
    for v in extracted.values():
        all_rows.extend(v)
    best, reason = select_exact_fanduel_quote(
        all_rows,
        pick_event_id="EVENT_X", pick_player="John McDoe",
        pick_market_key="player_shots_on_goal", pick_side="over",
        pick_line=2.5,
    )
    assert best is None
    assert reason == "no_fanduel_quote"


# ──────────────────── Scenario (d) CROSS-EVENT ────────────────────

def test_d_cross_event_collision_refused():
    """A FanDuel quote from a DIFFERENT event must never attach to a
    pick's observed_quotes list — even if player+market+side+line
    all line up."""
    other_event_quote = {
        "sport": "NHL", "event_id": "EVENT_OTHER",
        "provider_event_id": "EVENT_OTHER",
        "provider_market_key": "player_shots_on_goal",
        "market_family":       "player_shots_on_goal",
        "line": 2.5, "side": "over", "bookmaker": "fanduel",
        "price": 146, "odds_source": "the_odds_api",
        "observed_at": _OBSERVED_AT, "player_name": "John McDoe",
    }
    best, reason = select_exact_fanduel_quote(
        [other_event_quote],
        pick_event_id="EVENT_X", pick_player="John McDoe",
        pick_market_key="player_shots_on_goal", pick_side="over",
        pick_line=2.5,
    )
    assert best is None
    assert reason == "cross_event_rejected"


# ──────────────────── Scenario (e) CROSS-PLAYER ───────────────────

def test_e_cross_player_collision_refused():
    """A FanDuel quote for a DIFFERENT player must never attach."""
    wrong_player = {
        "sport": "NHL", "event_id": "EVENT_X",
        "provider_event_id": "EVENT_X",
        "provider_market_key": "player_shots_on_goal",
        "market_family":       "player_shots_on_goal",
        "line": 2.5, "side": "over", "bookmaker": "fanduel",
        "price": 146, "odds_source": "the_odds_api",
        "observed_at": _OBSERVED_AT, "player_name": "Someone Else",
    }
    best, reason = select_exact_fanduel_quote(
        [wrong_player],
        pick_event_id="EVENT_X", pick_player="John McDoe",
        pick_market_key="player_shots_on_goal", pick_side="over",
        pick_line=2.5,
    )
    assert best is None
    assert reason == "cross_player_rejected"


# ──────────── Scenario (f) SAME IDENTITY, TWO EVENTS ──────────────

def test_f_same_identity_two_events_isolated():
    """Two different events share identical (player, market, side,
    line, book=fanduel).  The quote from the OTHER event must never
    attach, even though the identity-minus-event is identical."""
    quotes = [
        {
            "sport": "NHL", "event_id": "EVENT_X",
            "provider_event_id": "EVENT_X",
            "provider_market_key": "player_shots_on_goal",
            "market_family":       "player_shots_on_goal",
            "line": 2.5, "side": "over", "bookmaker": "fanduel",
            "price": 146, "odds_source": "the_odds_api",
            "observed_at": _OBSERVED_AT, "player_name": "John McDoe",
        },
        {
            "sport": "NHL", "event_id": "EVENT_Y",
            "provider_event_id": "EVENT_Y",
            "provider_market_key": "player_shots_on_goal",
            "market_family":       "player_shots_on_goal",
            "line": 2.5, "side": "over", "bookmaker": "fanduel",
            "price":  999, "odds_source": "the_odds_api",   # obvious tell
            "observed_at": _OBSERVED_AT, "player_name": "John McDoe",
        },
    ]
    best, reason = select_exact_fanduel_quote(
        quotes,
        pick_event_id="EVENT_X", pick_player="John McDoe",
        pick_market_key="player_shots_on_goal", pick_side="over",
        pick_line=2.5,
    )
    assert reason is None
    assert best is not None
    # Must be the EVENT_X quote, not the +999 planted one.
    assert str(best.get("event_id")) == "EVENT_X"
    assert best["price"] == 146


# ───────── Scenario (g) SELECTED QUOTE OBJECT CARRIES IDENTITY ────

def test_g_selected_quote_object_identity_verified():
    """The identity check must verify the ACTUAL quote object — not
    the pick fields after attachment.  This test uses the full
    pre-publication lineage pass end-to-end and asserts
    ``pick['selected_quote_identity']`` equals the expected tuple."""
    good_quote = {
        "sport": "NHL", "event_id": "EVENT_X",
        "provider_event_id": "EVENT_X",
        "provider_market_key": "player_shots_on_goal",
        "market_family":       "player_shots_on_goal",
        "line": 2.5, "side": "over", "bookmaker": "fanduel",
        "price": 146, "odds_source": "the_odds_api",
        "observed_at": _OBSERVED_AT, "player_name": "John McDoe",
    }
    pick = _nhl_pick("EVENT_X", "John McDoe",
                      "player_shots_on_goal", "over", 2.5)
    pick["observed_quotes"] = [good_quote]

    res = _upgrade_pick_lineage(pick)
    assert res["migrated"] is True, res

    identity = pick.get("selected_quote_identity")
    assert identity is not None
    assert identity["bookmaker"]  == "fanduel"
    assert identity["event_id"]   == "EVENT_X"
    assert identity["player"]     == "john mcdoe"
    assert identity["market_key"] == "player_shots_on_goal"
    assert identity["side"]       == "over"
    assert identity["line"]       == 2.5

    assert pick["book_odds"]   == 146
    assert pick["bookmaker"]   == "fanduel"
    assert pick["odds_source"] == "the_odds_api"
    assert pick["no_real_book_line"] is False


# ───────────── FAIL-CLOSED on every rejection path ────────────────

@pytest.mark.parametrize("quotes,expected_reason", [
    # DK present, FanDuel missing.
    ([{
        "sport": "NHL", "event_id": "EVENT_X",
        "provider_event_id": "EVENT_X",
        "provider_market_key": "player_shots_on_goal",
        "market_family":       "player_shots_on_goal",
        "line": 2.5, "side": "over", "bookmaker": "draftkings",
        "price": 150, "odds_source": "the_odds_api",
        "observed_at": _OBSERVED_AT, "player_name": "John McDoe",
    }], "identity_book_mismatch"),
    # Wrong line on a FanDuel quote.
    ([{
        "sport": "NHL", "event_id": "EVENT_X",
        "provider_event_id": "EVENT_X",
        "provider_market_key": "player_shots_on_goal",
        "market_family":       "player_shots_on_goal",
        "line": 1.5, "side": "over", "bookmaker": "fanduel",
        "price": -170, "odds_source": "the_odds_api",
        "observed_at": _OBSERVED_AT, "player_name": "John McDoe",
    }], "identity_line_mismatch"),
    # Wrong side on a FanDuel quote.
    ([{
        "sport": "NHL", "event_id": "EVENT_X",
        "provider_event_id": "EVENT_X",
        "provider_market_key": "player_shots_on_goal",
        "market_family":       "player_shots_on_goal",
        "line": 2.5, "side": "under", "bookmaker": "fanduel",
        "price": -180, "odds_source": "the_odds_api",
        "observed_at": _OBSERVED_AT, "player_name": "John McDoe",
    }], "identity_side_mismatch"),
])
def test_h_harden_lineage_fail_closed(quotes, expected_reason):
    pick = _nhl_pick("EVENT_X", "John McDoe",
                      "player_shots_on_goal", "over", 2.5)
    pick["observed_quotes"] = quotes

    res = _upgrade_pick_lineage(pick)
    assert res["migrated"] is False
    assert pick["no_real_book_line"] is True
    assert pick["book_odds"] is None
    assert pick["bookmaker"] is None
    assert pick["odds_source"] == "MODEL_ONLY"
    assert pick.get("lineage_reason") == expected_reason


# ─────────── Non-NHL (MLB) behavior remains UNCHANGED ─────────────

def test_i_non_nhl_mlb_unchanged_no_identity_strictness():
    """MLB pick must keep the legacy behavior: aggregate across lines
    via partial lookup, no FanDuel-only enforcement, no identity
    re-check.  This proves the NHL fix is scoped and doesn't drift
    into other sports."""
    mlb_quote = {
        "sport": "MLB", "event_id": "EVENT_MLB",
        "provider_event_id": "EVENT_MLB",
        "provider_market_key": "batter_hits",
        "market_family":       "batter_hits",
        "line": 1.5, "side": "over", "bookmaker": "draftkings",
        "price": 110, "odds_source": "the_odds_api",
        "observed_at": _OBSERVED_AT, "player_name": "Some Batter",
    }
    mlb_pick = {
        "sport":             "MLB",
        "event_id":          "EVENT_MLB",
        "provider_event_id": "EVENT_MLB",
        "provider_market_key": "batter_hits",
        "market":            "batter_hits",
        "player_name":       "Some Batter",
        "pick_side":         "over",
        "line":              0.5,                # DIFFERENT line
        "bookmaker":         "fanduel",
        "observed_quotes":   [mlb_quote],
    }
    res = _upgrade_pick_lineage(mlb_pick)
    # Legacy MLB path: no identity check → the DK quote at line 1.5
    # STILL hardens the pick's book_odds (that's the pre-fix behavior
    # we must preserve for MLB).
    assert res["migrated"] is True
    assert mlb_pick["book_odds"] == 110
    assert mlb_pick["bookmaker"] == "draftkings"
