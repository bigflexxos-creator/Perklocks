"""PERKLOCKS-MAIN 35 · P1-11 + P1-12 — ANALYTICS DETERMINISTIC PROOF
+ SAME-SNAPSHOT COMPLETE PARITY.

Contracts:
  * A single canonical pick, when read through EVERY consumer's
    attach path, yields IDENTICAL PublishedPickContract fields.
  * `canonical_pick_id`, `sport`, `selection`, `canonical_market_
    family`, `line`, `side`, `publication_state` are byte-for-byte
    equal across:
        Locks, Pick Breakdown, Rollover, Parlay, My Bets, History,
        Analytics, Lab references.
  * Analytics returns the contract even when live steam data is
    empty (deterministic — does not depend on "steam is quiet today").
  * Adding decorations at any consumer does NOT alter the contract
    fields — decorations are read-only surface.

Since every consumer routes through `PublishedPickContract.from_pick`,
parity is proven by running that function on the same pick object
after each consumer's decoration is applied.
"""
from __future__ import annotations

import pytest


PARITY_FIELDS = (
    "canonical_pick_id",
    "sport",
    "selection",
    "side",
    "canonical_market_family",
    "line",
    "published_odds",
    "publication_state",
    "publication_revision",
    "board_version",
)


def _snapshot_pick():
    return {
        "id":                        "pick-parity-1",
        "canonical_pick_id":         "pick-parity-1",
        "canonical_event_id":        "event-parity-1",
        "sport":                     "MLB",
        "league":                    "MLB",
        "canonical_player_id":       "aaron-judge-nyy-1992",
        "canonical_team_id":         "NYY",
        "canonical_opponent_id":     "BAL",
        "canonical_market_family":   "hitter_home_runs",
        "provider_market_key":       "batter_home_runs",
        "line_type":                 "standard",
        "market_class":              "player_prop",
        "canonical_selection":       "Over",
        "published_side":            "over",
        "published_line":            0.5,
        "sportsbook":                "fanduel",
        "published_odds":            -140,
        "published_probability":     0.72,
        "published_lock_score":      88.0,
        "publication_state":         "PUBLISHED",
        "publication_revision":      1,
        "board_version":             "2026-06-01T12:00:00Z",
        "published_at":              "2026-06-01T12:00:00Z",
        "evidence_snapshot_version": 42,
    }


def _extract_contract(pick):
    from services.published_pick_contract import PublishedPickContract
    return PublishedPickContract.from_pick(pick).as_dict()


def _apply_locks_decoration(pick):
    # Locks board attaches contract + lightweight fields only.
    return pick


def _apply_pick_breakdown_decoration(pick):
    pick["explanation"] = "AI copy about the pick..."
    pick["ai_pending"] = False
    pick["signal_score"] = 87
    pick["evidence_score"] = 91
    return pick


def _apply_rollover_decoration(pick):
    pick["rollover_source_pick_id"] = "prev-pick-99"
    return pick


def _apply_parlay_decoration(pick):
    pick["parlay_membership"] = ["parlay-uuid-A", "parlay-uuid-B"]
    return pick


def _apply_my_bets_decoration(pick):
    pick["user_bet_id"] = "bet-uuid-99"
    pick["staked_units"] = 1
    return pick


def _apply_history_decoration(pick):
    pick["settlement_freshness"] = 0
    pick["settlement_state"] = "SETTLED"
    return pick


def _apply_analytics_decoration(pick):
    # Steam detector attaches magnitude / direction; contract unchanged.
    pick["steam_magnitude_pp"] = 3.5
    pick["steam_direction"] = "toward"
    return pick


def _apply_lab_decoration(pick):
    pick["lab_correlation_family"] = "MLB_HR"
    return pick


_CONSUMERS = (
    ("Locks",          _apply_locks_decoration),
    ("PickBreakdown",  _apply_pick_breakdown_decoration),
    ("Rollover",       _apply_rollover_decoration),
    ("Parlay",         _apply_parlay_decoration),
    ("MyBets",         _apply_my_bets_decoration),
    ("History",        _apply_history_decoration),
    ("Analytics",      _apply_analytics_decoration),
    ("Lab",            _apply_lab_decoration),
)


def test_same_snapshot_parity_across_all_consumers():
    base = _extract_contract(_snapshot_pick())
    for name, apply in _CONSUMERS:
        pick = _snapshot_pick()
        apply(pick)
        got = _extract_contract(pick)
        for f in PARITY_FIELDS:
            assert got.get(f) == base.get(f), (name, f, got.get(f), base.get(f))


def test_stacked_decorations_still_produce_identical_contract():
    """Apply EVERY consumer's decoration to the same pick; the contract
    fields must still exactly match the pristine one."""
    base = _extract_contract(_snapshot_pick())
    pick = _snapshot_pick()
    for _, apply in _CONSUMERS:
        apply(pick)
    stacked = _extract_contract(pick)
    for f in PARITY_FIELDS:
        assert stacked.get(f) == base.get(f), (f, stacked.get(f), base.get(f))


def test_analytics_returns_contract_deterministically():
    """Analytics does not depend on "steam is quiet today" — running
    `PublishedPickContract.from_pick` on ANY historical row yields the
    same wager identity regardless of live steam data availability."""
    from services.published_pick_contract import PublishedPickContract

    pick = _snapshot_pick()
    # Simulate multiple analytics reads (live-quiet / live-active).
    c1 = PublishedPickContract.from_pick(dict(pick)).as_dict()
    pick_live_quiet = {**pick, "steam_magnitude_pp": 0.0, "steam_direction": None}
    c2 = PublishedPickContract.from_pick(pick_live_quiet).as_dict()
    pick_live_active = {**pick, "steam_magnitude_pp": 4.2, "steam_direction": "toward"}
    c3 = PublishedPickContract.from_pick(pick_live_active).as_dict()
    for f in PARITY_FIELDS:
        assert c1.get(f) == c2.get(f) == c3.get(f), (f, c1.get(f), c2.get(f), c3.get(f))


def test_history_settlement_fields_are_separate_from_frozen_pregame_identity():
    """Settlement fields (settled_at, result, actual_score) are ALLOWED
    to change over time; frozen pregame wager identity must not."""
    from services.published_pick_contract import PublishedPickContract

    pick = _snapshot_pick()
    before = PublishedPickContract.from_pick(dict(pick)).as_dict()

    # Simulate settlement adding fields (never mutating pregame identity).
    pick["settled_at"] = "2026-06-02T00:00:00Z"
    pick["result"]     = "won"
    pick["actual"]     = {"player_home_runs": 1}
    pick["settlement_state"] = "SETTLED"

    after = PublishedPickContract.from_pick(dict(pick)).as_dict()
    for f in PARITY_FIELDS:
        assert before.get(f) == after.get(f), (f, before.get(f), after.get(f))


def test_multiple_calls_are_byte_for_byte_stable():
    """Regression: `from_pick` must be deterministic — no clock,
    no random, no db read."""
    from services.published_pick_contract import PublishedPickContract

    pick = _snapshot_pick()
    calls = [
        PublishedPickContract.from_pick(dict(pick)).as_dict()
        for _ in range(50)
    ]
    for c in calls[1:]:
        assert c == calls[0]
