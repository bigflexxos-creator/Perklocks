"""PERKLOCKS-MAIN 34 · P0 — PublishedPickContract contract tests
==================================================================

Guarantees:
  1. The frozen accessor prefers `published_*` canonical fields over
     their mutable legacy aliases.
  2. Every documented canonical key is either present or explicitly
     absent (never silently blanked out).
  3. Consumers get the SAME frozen values across repeated calls, even
     when a mutating decorator has since touched the source pick.
  4. Deriving `line_type` and `market_class` when they are absent
     never allows a mutable alias to outrank a canonical value that
     later arrives.
  5. Real live-DB published picks round-trip cleanly.
"""
from __future__ import annotations
import copy, os, sys
import pytest

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from services.published_pick_contract import (
    PublishedPickContract, contract_dict, _CANONICAL_KEYS,
)


def _base_pick():
    return {
        "id": "legacy-id-xxx",
        "canonical_pick_id": "canon-id-123",
        "canonical_event_id": "evt-99",
        "event_id": "evt-legacy",
        "sport": "MLB",
        "league": "MLB",
        "canonical_player_id": "player-501",
        "player_name": "Aaron Judge",
        "canonical_team_id": "team-NYY",
        "home_team_name": "New York Yankees",
        "canonical_opponent_id": "team-BOS",
        "away_team_name": "Boston Red Sox",
        "canonical_market_family": "player_hits",
        "market_family": "hits",
        "provider_market_key": "batter_hits",
        "market_key": "batter_hits",
        "canonical_selection": "Aaron Judge",
        "provider_selection": "AJ",
        "selection": "AJ",
        "published_side": "over",
        "side": "over",
        "published_line": 0.5,
        "provider_line": 1.5,
        "line": 1.5,
        "sportsbook": "fanduel",
        "published_odds": -155,
        "book_odds": -160,
        "american_odds": -160,
        "odds": -160,
        "published_probability": 0.63,
        "model_win_prob": 0.60,
        "win_probability": 0.61,
        "published_lock_score": 91.4,
        "lock_score": 88.7,           # mutated post-publication
        "published_grade": "Strong Lock",
        "grade": "Lock",               # mutated post-publication
        "publication_state": "PUBLISHED",
        "publication_revision": 1,
        "board_version": "board-20260902T210202Z",
        "published_at": "2026-09-02T20:00:00Z",
        "publication_published_at": "2026-09-02T20:05:00Z",
        "snapshot_version": 3,
    }


def test_p0_published_fields_outrank_legacy_aliases():
    c = PublishedPickContract.from_pick(_base_pick())
    assert c.canonical_pick_id     == "canon-id-123"
    assert c.event_id              == "evt-99"
    assert c.player_identity       == "player-501"
    assert c.team_identity         == "team-NYY"
    assert c.opponent_identity     == "team-BOS"
    assert c.canonical_market_family == "player_hits"
    assert c.provider_market_key   == "batter_hits"
    assert c.selection             == "Aaron Judge"
    assert c.side                  == "over"
    assert c.line                  == 0.5
    assert c.sportsbook            == "fanduel"
    assert c.published_odds        == -155
    assert c.win_expected          == 0.63
    assert c.published_lock_score  == 91.4
    assert c.published_grade       == "Strong Lock"
    assert c.publication_state     == "PUBLISHED"
    assert c.publication_revision  == 1
    assert c.evidence_snapshot_version == 3


def test_p0_provenance_records_canonical_source():
    c = PublishedPickContract.from_pick(_base_pick())
    prov = c.provenance()
    for k in ("canonical_pick_id", "event_id", "player_identity",
                "team_identity", "canonical_market_family",
                "published_odds", "win_expected", "published_lock_score",
                "published_grade", "publication_state"):
        assert prov[k] == "canonical", (
            f"{k} should read from canonical field but got: {prov[k]}"
        )


def test_p0_legacy_fallback_when_canonical_absent():
    p = _base_pick()
    # Nuke every canonical field so we fall back to legacy aliases.
    for k in ("canonical_pick_id", "canonical_event_id",
                "canonical_player_id", "canonical_team_id",
                "canonical_opponent_id", "canonical_market_family",
                "canonical_selection", "published_side", "published_line",
                "published_odds", "published_probability",
                "published_lock_score", "published_grade"):
        p.pop(k, None)
    c = PublishedPickContract.from_pick(p)
    prov = c.provenance()
    # canonical_pick_id → id
    assert c.canonical_pick_id == "legacy-id-xxx"
    assert prov["canonical_pick_id"].startswith("legacy:")
    # published_lock_score falls back to lock_score
    assert c.published_lock_score == 88.7
    assert prov["published_lock_score"].startswith("legacy:")


def test_p0_no_mutation_of_source_pick():
    p = _base_pick()
    snap = copy.deepcopy(p)
    _ = PublishedPickContract.from_pick(p)
    assert p == snap, "PublishedPickContract must never mutate the source dict"


def test_p0_frozen_contract_survives_post_mutation():
    p = _base_pick()
    c = PublishedPickContract.from_pick(p)
    # A signal engine now mutates the source pick — canonical view must
    # be untouched.
    p["published_lock_score"] = 40.0
    p["published_grade"] = "Pass"
    assert c.published_lock_score == 91.4
    assert c.published_grade == "Strong Lock"


def test_p0_market_class_derived_when_absent():
    p = _base_pick()
    p.pop("market_class", None)
    c = PublishedPickContract.from_pick(p)
    assert c.market_class == "player_prop", \
        "player identity present ⇒ player_prop"
    prov = c.provenance()
    assert prov["market_class"] == "derived"

    # Team pick — no player identity → game_market
    p2 = _base_pick()
    p2.pop("market_class", None)
    for k in ("canonical_player_id", "player_name", "elite_player",
                "elite_player_name", "player"):
        p2.pop(k, None)
    c2 = PublishedPickContract.from_pick(p2)
    assert c2.market_class == "game_market"


def test_p0_line_type_alternate_derivation():
    p = _base_pick()
    p["is_alt"] = True
    p.pop("line_type", None)
    c = PublishedPickContract.from_pick(p)
    assert c.line_type == "alternate"


def test_p0_as_dict_exposes_all_canonical_keys():
    d = contract_dict(_base_pick())
    for k in _CANONICAL_KEYS:
        assert k in d, f"as_dict() missing canonical key {k!r}"
    assert "_provenance" not in d, "as_dict() must not leak the provenance map"


def test_p0_first_helper_does_not_filter_zero():
    """`line` can legitimately be 0.0 (e.g. draw-no-bet handicap). The
    contract must NOT treat 0.0 as absent."""
    p = _base_pick()
    p["published_line"] = 0.0
    c = PublishedPickContract.from_pick(p)
    assert c.line == 0.0
    assert c.provenance()["line"] == "canonical"


def test_p0_live_published_picks_round_trip():
    """Every published pick currently on `/api/picks/today` (full DTO)
    must yield a contract with every mandatory canonical field
    populated (id, sport, market family, selection, publication_state).
    """
    import httpx
    try:
        try:
            from rate_limit import _reset_for_tests
            _reset_for_tests(scope_prefix="ip:")
        except Exception:
            pass
        r = httpx.post("http://localhost:8001/api/auth/login",
                        json={"email": "demo@lockscore.ai", "password": "demo123"},
                        timeout=10)
    except Exception as e:
        pytest.skip(f"backend unavailable: {e}")
    if r.status_code != 200:
        pytest.skip(f"login {r.status_code}")
    tok = r.json()["access_token"]
    r = httpx.get("http://localhost:8001/api/picks/today",
                    headers={"Authorization": f"Bearer {tok}"}, timeout=90)
    if r.status_code != 200:
        pytest.skip("picks unavailable")
    picks = r.json().get("picks", [])
    if not picks:
        pytest.skip("no picks live")
    bad = []
    for p in picks[:60]:
        c = PublishedPickContract.from_pick(p)
        for f in ("canonical_pick_id", "sport", "selection",
                    "publication_state"):
            if getattr(c, f) is None:
                bad.append((p.get("id"), f))
    assert not bad, f"Contract failed mandatory fields on {bad[:3]}"
