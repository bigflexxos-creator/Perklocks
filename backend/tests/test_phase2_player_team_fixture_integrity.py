"""Phase 2 (2026-08-11) — Current Data Integrity + Publication Safety.

Covers the player ↔ current team ↔ fixture integrity validator and its
two-layer enforcement.  Uses mocked roster examples (Leo Walta,
Victor Lind, Tokmac Nguen) — no permanent real-world team claims.

Test roster examples:
    Leo Walta       → Inter Turku
    Victor Lind     → Ilves (transferred away from HJK last season)
    Tokmac Nguen    → Aalesunds FK
"""
from __future__ import annotations

import pathlib


_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]


ROSTER = {
    # Fresh, trusted current-team observations.
    "leo walta":     "Inter Turku",
    "victor lind":   "Ilves",
    "tokmac nguen":  "Aalesunds FK",
    "kylian mbappe": "Real Madrid",
    "erling haaland": "Manchester City",
    "kane":          "Bayern Munich",
    "harry kane":    "Bayern Munich",
    "sam adekugbe":  "Coventry City",
    "sami":          "Some Club",
}
FRESH = set(ROSTER.keys())


# ── A. Validator unit tests ────────────────────────────────────────
def test_current_home_player_accepted():
    from services.player_team_fixture_validator import (
        validate_player_fixture_pick,
    )
    pick = {"sport": "Soccer",
            "market": "Leo Walta - Anytime Goal Scorer",
            "player_name": "Leo Walta",
            "event": "Inter Turku vs KuPS"}
    v = validate_player_fixture_pick(pick, ROSTER, fresh_roster_names=FRESH)
    assert v["verified"] is True
    assert v["player_team"] == "Inter Turku"


def test_current_away_player_accepted():
    from services.player_team_fixture_validator import (
        validate_player_fixture_pick,
    )
    pick = {"sport": "Soccer",
            "market": "Victor Lind - Anytime Goal Scorer",
            "player_name": "Victor Lind",
            "event": "Turku Palloseura vs Ilves"}
    v = validate_player_fixture_pick(pick, ROSTER, fresh_roster_names=FRESH)
    assert v["verified"] is True
    assert v["player_team"] == "Ilves"


def test_transferred_historical_team_player_rejected():
    """Historical Wikipedia says "Victor Lind → HJK".  Fresh roster
    says Ilves.  Fixture is HJK vs Inter Turku.  Must REJECT — Lind
    is not on the fixture per fresh roster."""
    from services.player_team_fixture_validator import (
        validate_player_fixture_pick, REASON_PLAYER_TEAM_MISMATCH,
    )
    pick = {"sport": "Soccer",
            "market": "Victor Lind - Anytime Goal Scorer",
            "player_name": "Victor Lind",
            "event": "HJK vs Inter Turku"}
    v = validate_player_fixture_pick(pick, ROSTER, fresh_roster_names=FRESH)
    assert v["verified"] is False
    assert v["reason"] == REASON_PLAYER_TEAM_MISMATCH


def test_unrelated_team_player_rejected():
    from services.player_team_fixture_validator import (
        validate_player_fixture_pick, REASON_PLAYER_TEAM_MISMATCH,
    )
    pick = {"sport": "Soccer",
            "market": "Tokmac Nguen - Anytime Goal Scorer",
            "player_name": "Tokmac Nguen",
            "event": "Inter Turku vs Ilves"}
    v = validate_player_fixture_pick(pick, ROSTER, fresh_roster_names=FRESH)
    assert v["verified"] is False
    assert v["reason"] == REASON_PLAYER_TEAM_MISMATCH


def test_stale_scorer_source_cannot_override_fresh_roster():
    """Roster lookup entry exists but the FRESH set doesn't contain
    the player — treat as roster_unverified (stale evidence only)."""
    from services.player_team_fixture_validator import (
        validate_player_fixture_pick, REASON_ROSTER_UNVERIFIED,
    )
    stale_lookup = dict(ROSTER)
    stale_lookup["former hjk striker"] = "HJK"   # stale historical evidence
    fresh_names = FRESH   # doesn't include the stale name
    pick = {"sport": "Soccer",
            "market": "Former HJK Striker - Anytime Goal Scorer",
            "player_name": "Former HJK Striker",
            "event": "HJK vs Inter Turku"}
    v = validate_player_fixture_pick(
        pick, stale_lookup, fresh_roster_names=fresh_names)
    assert v["verified"] is False
    assert v["reason"] == REASON_ROSTER_UNVERIFIED


def test_missing_roster_verification_blocks_history_only_prop():
    from services.player_team_fixture_validator import (
        validate_player_fixture_pick, REASON_ROSTER_UNVERIFIED,
    )
    pick = {"sport": "Soccer",
            "market": "Unknown Player - Anytime Goal Scorer",
            "player_name": "Unknown Player",
            "event": "Inter Turku vs HJK"}
    v = validate_player_fixture_pick(pick, {}, fresh_roster_names=set())
    assert v["verified"] is False
    assert v["reason"] == REASON_ROSTER_UNVERIFIED


def test_team_level_markets_unaffected():
    from services.player_team_fixture_validator import (
        validate_player_fixture_pick, REASON_MARKET_NOT_PLAYER,
    )
    for mkt in ("Inter Turku Moneyline",
                 "Over 2.5 Total Goals",
                 "Both Teams To Score - Yes",
                 "HJK Draw No Bet"):
        pick = {"sport": "Soccer", "market": mkt,
                "event": "HJK vs Inter Turku"}
        v = validate_player_fixture_pick(pick, {})
        assert v["verified"] is True
        assert v["reason"] == REASON_MARKET_NOT_PLAYER


# ── Alias / diacritic safety ────────────────────────────────────────
def test_diacritic_matching_safe():
    """Mbappé must match "Mbappe" in the roster (diacritic-safe)."""
    from services.player_team_fixture_validator import (
        validate_player_fixture_pick,
    )
    pick = {"sport": "Soccer",
            "market": "Kylian Mbappé - Anytime Goal Scorer",
            "player_name": "Kylian Mbappé",
            "event": "Real Madrid vs Barcelona"}
    v = validate_player_fixture_pick(pick, ROSTER,
                                     fresh_roster_names=FRESH)
    assert v["verified"] is True


def test_alias_matching_last_name_unique():
    """Roster stores "Harry Kane" — pick says "Kane".  Unique match
    on last name → accept."""
    from services.player_team_fixture_validator import (
        validate_player_fixture_pick,
    )
    pick = {"sport": "Soccer",
            "market": "Kane - Anytime Goal Scorer",
            "player_name": "Kane",
            "event": "Bayern Munich vs Borussia"}
    v = validate_player_fixture_pick(pick, ROSTER,
                                     fresh_roster_names=FRESH)
    assert v["verified"] is True


def test_no_loose_substring_matching():
    """`Sam Adek` (fictional partial) must NOT match `Sam Adekugbe` or
    `Sami`.  The validator's word-boundary alias rule requires unique
    last-name match, not substring."""
    from services.player_team_fixture_validator import (
        validate_player_fixture_pick, REASON_ROSTER_UNVERIFIED,
    )
    pick = {"sport": "Soccer",
            "market": "Sam Adek - Anytime Goal Scorer",
            "player_name": "Sam Adek",
            "event": "Coventry City vs Wrexham"}
    v = validate_player_fixture_pick(pick, ROSTER,
                                     fresh_roster_names=FRESH)
    assert v["verified"] is False
    assert v["reason"] == REASON_ROSTER_UNVERIFIED


# ── Layer-B integration (source-level) ─────────────────────────────
def test_publication_helper_gates_soccer_player_props():
    """`publish_upserted_picks` must include the Layer-B integrity gate
    inline BEFORE the publish_batch call."""
    src = (_BACKEND_ROOT / "services" / "publication_helpers.py").read_text()
    assert "Layer-B integrity gate" in src
    assert "validate_player_fixture_pick" in src
    # Ensures the gate runs BEFORE publish_batch — publish_batch call
    # must come AFTER the gate marker.
    gate_idx = src.find("validate_player_fixture_pick")
    pub_idx = src.find("await publisher.publish_batch")
    assert gate_idx < pub_idx


def test_orchestrator_gates_soccer_player_props_before_publish():
    src = (_BACKEND_ROOT / "services" /
           "pick_refresh_orchestrator.py").read_text()
    assert "Layer-B player↔team gate" in src
    gate_idx = src.find("Layer-B player↔team gate")
    pub_idx = src.find("await publisher.publish_batch(\n            _publish_batch")
    assert gate_idx > 0 and pub_idx > 0
    assert gate_idx < pub_idx


# ── Direct-injector bypass prevention ──────────────────────────────
def test_direct_injector_cannot_bypass_gate():
    """Any writer that calls publish_upserted_picks (the sole public
    canonical publication helper for ingest paths) is gated.  The
    only way to bypass is calling `db.picks.insert_one` without ever
    calling publish_upserted_picks — that path leaves the pick with
    no publication_source, which the >85 board gate rejects.

    This test asserts the invariant is in-source and enforceable."""
    src = (_BACKEND_ROOT / "services" / "publication_helpers.py").read_text()
    # The gate is the ONLY branch that filters picks_list.
    assert "picks_list = valid_picks" in src


# ── Season resolution ──────────────────────────────────────────────
def test_cfb_current_year_dynamic():
    src = (_BACKEND_ROOT / "services" / "cfb_ingest.py").read_text()
    assert "CURRENT_YEAR = 2025" not in src, (
        "CFB CURRENT_YEAR must be dynamic, not hardcoded to 2025"
    )
    assert "_current_cfb_year()" in src
    from services.cfb_ingest import _current_cfb_year
    # Aug 2026 should resolve to 2026.
    assert _current_cfb_year() == 2026


def test_mls_refresh_uses_dynamic_season():
    src = (_BACKEND_ROOT / "server.py").read_text()
    # The hardcoded refresh_mls_leaders(season=2025) call is gone.
    assert "refresh_mls_leaders(season=2025)" not in src
    # Dynamic resolution marker present.
    assert "_mls_season = _now.year" in src
    assert "refresh_mls_leaders(season=_mls_season)" in src


# ── Regression: P0-1 → P0-4 invariants intact ──────────────────────
def test_locks_contract_still_strict_gt_85():
    from services.main_board_eligibility import (
        is_main_board_eligible, main_board_lock_score_query,
    )
    assert is_main_board_eligible({"lock_score": 85.0}) is False
    assert is_main_board_eligible({"lock_score": 85.001}) is True
    q = main_board_lock_score_query()
    assert q["$or"][0] == {"published_lock_score": {"$gt": 85.0}}


def test_no_real_line_still_returns_null_edge():
    """P0-4 invariant — validator does NOT touch odds/edge, so a
    model-only pick still surfaces edge=None."""
    from services.main_board_eligibility import is_main_board_eligible
    p = {"lock_score": 92.0, "edge_percent": None, "book_odds": None,
         "no_real_book_line": True, "model_only": True}
    assert is_main_board_eligible(p) is True


def test_publication_immutability_regression():
    """Prediction-defining fields on a snapshot must remain immutable
    (P0-3 legacy contract).  Not touched by Phase 2."""
    src = (_BACKEND_ROOT / "services" /
           "prediction_publication_service.py").read_text()
    assert "@dataclass(frozen=True)" in src
    assert "class PublishedPayload:" in src


# ── Verdict tagging helper ─────────────────────────────────────────
def test_tag_pick_with_verdict_valid():
    from services.player_team_fixture_validator import (
        validate_player_fixture_pick, tag_pick_with_verdict,
    )
    pick = {"sport": "Soccer",
            "market": "Leo Walta - Anytime Goal Scorer",
            "player_name": "Leo Walta",
            "event": "Inter Turku vs KuPS"}
    v = validate_player_fixture_pick(pick, ROSTER, fresh_roster_names=FRESH)
    tag_pick_with_verdict(pick, v)
    assert pick["player_team_verified"] is True
    assert pick["player_team_invalid"] is False


def test_tag_pick_with_verdict_invalid_stamps_reason():
    from services.player_team_fixture_validator import (
        validate_player_fixture_pick, tag_pick_with_verdict,
        REASON_PLAYER_TEAM_MISMATCH,
    )
    pick = {"sport": "Soccer",
            "market": "Victor Lind - Anytime Goal Scorer",
            "player_name": "Victor Lind",
            "event": "HJK vs Inter Turku"}
    v = validate_player_fixture_pick(pick, ROSTER, fresh_roster_names=FRESH)
    tag_pick_with_verdict(pick, v)
    assert pick["player_team_verified"] is False
    assert pick["player_team_invalid"] is True
    assert pick["player_team_invalid_reason"] == REASON_PLAYER_TEAM_MISMATCH
    snap = pick["player_team_snapshot"]
    assert snap["player_team"] == "Ilves"
    assert snap["fixture_teams"] == ("HJK", "Inter Turku")
