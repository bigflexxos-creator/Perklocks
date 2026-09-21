"""P0 · Historical Intelligence H2H Truth Contract Regression Suite
─────────────────────────────────────────────────────────────────────
Locks in the fail-closed identity contract in
``services.historical_intelligence._opp_matches``:

  TIER 1 — canonical ID exact match
  TIER 2 — exact normalized name match
  TIER 3 — verified alias registry (soccer / NFL abbreviations)

NEVER matches on shared word / token overlap / substring / mascot /
city / fuzzy similarity — that's what produced the SMU vs Missouri
State canary bug where "Baylor Bears" & "Florida State Seminoles"
falsely counted as prior meetings via the shared tokens "bears" and
"state".
"""
from __future__ import annotations
import pytest

from services.historical_intelligence import _opp_matches


# ─── TEST 1 — SMU CANARY ────────────────────────────────────────────
def test_smu_canary_missouri_state_only_matches_itself():
    target = ("mo-state-canon-id", "Missouri State Bears")
    # Real prior meeting.
    assert _opp_matches(None, "Missouri State Bears", *target) is True
    # ex-canary — "Bears" shared token, "Baylor Bears".
    assert _opp_matches(None, "Baylor Bears", *target) is False
    # ex-canary — "State" shared token, "Florida State Seminoles".
    assert _opp_matches(None, "Florida State Seminoles", *target) is False


# ─── TEST 2 — GENERIC "STATE" COLLISION ─────────────────────────────
def test_generic_state_token_never_matches_across_schools():
    for school in ("Iowa State Cyclones", "Kansas State Wildcats",
                   "Michigan State Spartans", "Ohio State Buckeyes",
                   "Penn State Nittany Lions", "Oklahoma State Cowboys"):
        assert _opp_matches(None, school, None, "Missouri State Bears") is False


# ─── TEST 3 — MASCOT COLLISION ──────────────────────────────────────
def test_mascot_collision_never_matches():
    # "Bears" family
    for x in ("Chicago Bears", "Baylor Bears", "Cal Golden Bears",
              "Mercer Bears"):
        assert _opp_matches(None, x, None, "Missouri State Bears") is False
    # "Tigers" family
    for x in ("Auburn Tigers", "LSU Tigers", "Missouri Tigers",
              "Clemson Tigers"):
        assert _opp_matches(None, x, None, "Detroit Tigers") is False


# ─── TEST 4 — VERIFIED ALIAS (SOCCER) ───────────────────────────────
def test_verified_soccer_alias_matches():
    assert _opp_matches(None, "Nott'm Forest", None, "Nottingham Forest") is True
    assert _opp_matches(None, "Nott'm Forest", None, "Brighton") is False


# ─── TEST 5 — VERIFIED ALIAS (NFL ABBREVIATION) ─────────────────────
def test_nfl_abbreviation_matches_canonical_name():
    # Observation carries "BUF" (nflverse code); pick carries "Buffalo Bills".
    assert _opp_matches("BUF", "BUF", None, "Buffalo Bills") is True
    # Provider gives opponent_name="BUF" and no id.
    assert _opp_matches(None, "BUF", None, "Buffalo Bills") is True
    # Cross: pick has id="BUF", historical has full name.
    assert _opp_matches(None, "Buffalo Bills", "BUF", None) is True
    # Different NFL team — no match.
    assert _opp_matches("KC", "KC", None, "Buffalo Bills") is False
    assert _opp_matches(None, "KC", None, "Buffalo Bills") is False


# ─── TEST 6 — SUBSTRING NEVER MATCHES ───────────────────────────────
def test_substring_alone_never_matches_h2h():
    # "Buffalo" ⊂ "Buffalo Bills" but the observation opponent is only
    # "Buffalo" (some other team or truncated string).  Must NOT match.
    assert _opp_matches(None, "Buffalo", None, "Buffalo Bills") is False


# ─── TEST 7 — CANONICAL ID PRECEDENCE ───────────────────────────────
def test_canonical_id_exact_match_takes_precedence():
    # Different names but same canonical id → verified match.
    assert _opp_matches("team-42", "Ambiguous A",
                        "team-42", "Ambiguous B") is True
    # Different-ish names, no IDs, and clearly different teams → False.
    assert _opp_matches(None, "New York Yankees",
                        None, "New York Mets") is False


# ─── TEST 8 — EMPTY INPUTS RETURN FALSE ─────────────────────────────
def test_empty_or_missing_inputs_return_false():
    assert _opp_matches(None, None, None, "Buffalo Bills") is False
    assert _opp_matches(None, "Buffalo Bills", None, None) is False
    assert _opp_matches("", "", "", "") is False


# ─── TEST 9 — PUNCTUATION / WHITESPACE NORMALIZATION ────────────────
def test_normalization_handles_punctuation_and_whitespace():
    assert _opp_matches(None, "  L.A. Rams  ", None, "LA Rams") is True
    assert _opp_matches(None, "St. Louis Blues", None, "St Louis Blues") is True


# ─── TEST 10 — UNIVERSITY / CITY GENERIC TOKENS BANNED ──────────────
def test_generic_tokens_never_match():
    # "University" alone must never match.
    assert _opp_matches(None, "Duke University", None, "Rice University") is False
    # "City" alone must never match.
    assert _opp_matches(None, "Manchester City", None, "New York City FC") is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
