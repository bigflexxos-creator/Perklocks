"""PERKLOCKS-MAIN 35 · P1-10 — PICK BREAKDOWN 2.0 (READ-ONLY TRUTH).

Contracts:
  * `PublishedPickContract.from_pick(pick)` is deterministic: two
    calls on the same pick return equal contracts.
  * Adding decoration fields (explanation, signal score, streak
    chips, fusion block) does NOT change the canonical wager
    identity in the resulting contract.
  * Mutating the pick's lock_score / evidence_score does NOT change
    the canonical wager identity.
  * A pick that only has canonical fields → contract fields all
    provenance="canonical".
  * A pick that only has legacy fields → contract still resolves
    (with provenance labeled "legacy:*").
  * The contract's `as_dict()` output is JSON-serialisable and
    stable across calls.
"""
from __future__ import annotations
import json

import pytest


def _canonical_pick():
    return {
        "id":                        "pick-abc-1",
        "canonical_pick_id":         "pick-abc-1",
        "canonical_event_id":        "event-xyz-7",
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
        "published_grade":           None,
        "publication_state":         "PUBLISHED",
        "publication_revision":      1,
        "board_version":             "2026-06-01T12:00:00Z",
        "published_at":              "2026-06-01T12:00:00Z",
        "evidence_snapshot_version": 42,
    }


def test_contract_is_deterministic():
    from services.published_pick_contract import PublishedPickContract

    p = _canonical_pick()
    a = PublishedPickContract.from_pick(dict(p))
    b = PublishedPickContract.from_pick(dict(p))
    assert a == b
    assert a.as_dict() == b.as_dict()


def test_contract_identity_unchanged_by_decoration_fields():
    from services.published_pick_contract import PublishedPickContract

    p = _canonical_pick()
    base = PublishedPickContract.from_pick(dict(p)).as_dict()

    # Simulate every decoration Pick Breakdown adds.
    p["explanation"]        = "AI copy about the pick..."
    p["ai_pending"]         = False
    p["signal_score"]       = 87
    p["signal_components"]  = [{"label": "Volume", "points": 22}]
    p["player_streak"]      = {"hits": 5}
    p["fusion_block"]       = {"trained_ml": 0.71}
    p["insights"]           = ["Volume: 80/100 — strong."]
    p["evidence_score"]     = 91

    dec = PublishedPickContract.from_pick(dict(p)).as_dict()
    # Every canonical wager field must be identical before/after.
    for k in (
        "canonical_pick_id", "event_id", "sport", "league",
        "player_identity", "team_identity", "opponent_identity",
        "canonical_market_family", "provider_market_key",
        "selection", "side", "line",
        "sportsbook", "published_odds", "win_expected",
        "publication_state", "publication_revision", "board_version",
        "published_at", "evidence_snapshot_version",
    ):
        assert dec.get(k) == base.get(k), (k, dec.get(k), base.get(k))


def test_contract_identity_unchanged_by_lock_score_mutations():
    from services.published_pick_contract import PublishedPickContract

    p = _canonical_pick()
    base = PublishedPickContract.from_pick(dict(p)).as_dict()

    # Simulate the display-cap / coherence pipeline mutating lock_score
    # and lock_score_v2 without touching wager identity.
    p["lock_score"]      = 75.0
    p["lock_score_v2"]   = 78.0
    p["lock_score_peak"] = 88.0
    p["coherence_cap_ceiling"] = 78.0

    dec = PublishedPickContract.from_pick(dict(p)).as_dict()
    # Wager identity fields identical; only the display-cap fields
    # differ on the pick itself, not on the frozen contract.
    for k in (
        "selection", "side", "line", "published_odds",
        "canonical_pick_id", "canonical_market_family",
    ):
        assert dec.get(k) == base.get(k), (k, dec.get(k), base.get(k))


def test_contract_provenance_is_canonical_when_canonical_fields_present():
    from services.published_pick_contract import PublishedPickContract

    p = _canonical_pick()
    c = PublishedPickContract.from_pick(dict(p))
    prov = c.provenance()
    for key in (
        "canonical_pick_id", "event_id", "player_identity",
        "team_identity", "opponent_identity",
        "canonical_market_family", "provider_market_key",
        "selection", "line", "published_odds",
        "publication_state", "board_version",
    ):
        assert prov.get(key) == "canonical", (key, prov.get(key))


def test_contract_legacy_shape_still_resolves():
    from services.published_pick_contract import PublishedPickContract

    legacy = {
        "id": "pick-legacy",
        "sport": "MLB",
        "player_name":  "Aaron Judge",
        "home_team_name": "New York Yankees",
        "away_team_name": "Baltimore Orioles",
        "market_family": "hitter_home_runs",
        "market_key":    "batter_home_runs",
        "selection":     "Over",
        "side":          "over",
        "line":          0.5,
        "book_odds":     -140,
        "model_win_prob": 0.72,
        "lock_score":     88.0,
    }
    c = PublishedPickContract.from_pick(dict(legacy)).as_dict()
    assert c["canonical_pick_id"] == "pick-legacy"
    assert c["sport"] == "MLB"
    assert c["player_identity"] == "Aaron Judge"
    assert c["canonical_market_family"] == "hitter_home_runs"
    assert c["line"] == 0.5
    assert c["published_odds"] == -140


def test_contract_as_dict_is_json_serialisable():
    from services.published_pick_contract import PublishedPickContract

    p = _canonical_pick()
    c = PublishedPickContract.from_pick(p)
    payload = json.dumps(c.as_dict())
    parsed = json.loads(payload)
    assert parsed["canonical_pick_id"] == p["canonical_pick_id"]
    assert parsed["line"] == p["published_line"]
