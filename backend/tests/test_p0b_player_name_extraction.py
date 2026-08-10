"""P0-B (2026-08-11) — Soccer player-name extraction correctness.

Verifies :func:`_extract_player_name` is:

* Case-insensitive across all supported market suffixes.
* Accent-preserving (Ünïcode names come through untouched).
* Priority-aware — structured fields (``player_name``, ``player``,
  ``selection``, ``pick_side``) always win when non-empty.
* Immune to loose parsing that would truncate legitimate names
  (e.g. "Julian Alvarez" must NEVER be split at "arez").
"""
from __future__ import annotations

import pytest

from services.player_team_fixture_validator import (
    _extract_player_name,
    validate_player_fixture_pick,
    REASON_PLAYER_NAME_MISSING,
    REASON_ROSTER_UNVERIFIED,
    REASON_PLAYER_TEAM_MISMATCH,
)


# ── 1. Exact reproductions of the failures in the P0-A verification ──
def test_federico_vinas_to_score_or_assist_extracted():
    pick = {"market": "Federico Vinas To Score or Assist",
            "sport": "Soccer",
            "event": "Uruguay @ Saudi Arabia"}
    assert _extract_player_name(pick) == "Federico Vinas"


def test_lionel_messi_to_score_or_assist_extracted():
    pick = {"market": "Lionel Messi To Score or Assist",
            "sport": "Soccer",
            "event": "Algeria @ Argentina"}
    assert _extract_player_name(pick) == "Lionel Messi"


def test_julian_alvarez_to_score_or_assist_extracted():
    """The specific example from the P0-A report — must not be
    truncated (no 'Julian' or 'Julian Alv' output)."""
    pick = {"market": "Julian Alvarez To Score or Assist",
            "sport": "Soccer",
            "event": "Algeria @ Argentina"}
    assert _extract_player_name(pick) == "Julian Alvarez"


# ── 2. Every supported market suffix (case-insensitive) ─────────
@pytest.mark.parametrize("market,expected", [
    ("Lionel Messi Anytime Goal Scorer",       "Lionel Messi"),
    ("Lionel Messi anytime goal scorer",       "Lionel Messi"),
    ("Lionel Messi ANYTIME GOAL SCORER",       "Lionel Messi"),
    ("Lionel Messi First Goal Scorer",         "Lionel Messi"),
    ("Lionel Messi first goal scorer",         "Lionel Messi"),
    ("Lionel Messi Last Goal Scorer",          "Lionel Messi"),
    ("Lionel Messi last goal scorer",          "Lionel Messi"),
    ("Lionel Messi To Score",                  "Lionel Messi"),
    ("Lionel Messi to score",                  "Lionel Messi"),
    ("Lionel Messi TO SCORE",                  "Lionel Messi"),
    ("Lionel Messi To Assist",                 "Lionel Messi"),
    ("Lionel Messi to assist",                 "Lionel Messi"),
    ("Lionel Messi To Score or Assist",        "Lionel Messi"),
    ("Lionel Messi to score or assist",        "Lionel Messi"),
    ("Lionel Messi Score or Assist",           "Lionel Messi"),
    ("Lionel Messi score or assist",           "Lionel Messi"),
    ("Lionel Messi Goal Scorer",               "Lionel Messi"),
    ("Lionel Messi First Goal",                "Lionel Messi"),
    ("Lionel Messi Last Goal",                 "Lionel Messi"),
])
def test_all_supported_market_suffixes_case_insensitive(market, expected):
    assert _extract_player_name({"market": market}) == expected


# ── 3. Mixed capitalization ─────────────────────────────────────
@pytest.mark.parametrize("market,expected", [
    ("Federico Vinas TO score OR ASsist",   "Federico Vinas"),
    ("Federico Vinas To Score Or Assist",   "Federico Vinas"),
    ("Federico Vinas tO sCoRe oR aSsIsT",   "Federico Vinas"),
    ("Kylian Mbappe FIRST goal SCORER",     "Kylian Mbappe"),
])
def test_mixed_capitalization(market, expected):
    assert _extract_player_name({"market": market}) == expected


# ── 4. Accent preservation ───────────────────────────────────────
@pytest.mark.parametrize("market,expected", [
    ("Kylian Mbappé Anytime Goal Scorer",   "Kylian Mbappé"),
    ("Kylian Mbappé To Score or Assist",    "Kylian Mbappé"),
    ("Vinícius Júnior Anytime Goal Scorer", "Vinícius Júnior"),
    ("Rúben Neves To Score",                "Rúben Neves"),
    ("N'Golo Kanté To Assist",              "N'Golo Kanté"),
    ("Zlatan Ibrahimović Last Goal Scorer", "Zlatan Ibrahimović"),
])
def test_accents_preserved(market, expected):
    got = _extract_player_name({"market": market})
    assert got == expected, (
        f"accent regression: expected {expected!r}, got {got!r}"
    )


# ── 5. Canonical `<Name> - <Market>` shape ──────────────────────
def test_dash_separator_takes_precedence():
    pick = {"market": "Leo Walta - Anytime Goal Scorer"}
    assert _extract_player_name(pick) == "Leo Walta"


def test_dash_separator_case_insensitive_market_side():
    """Right-hand side casing is irrelevant when a dash is present."""
    pick = {"market": "Victor Lind - anytime GOAL scorer"}
    assert _extract_player_name(pick) == "Victor Lind"


# ── 6. Structured field priority over market string ─────────────
def test_structured_player_name_takes_priority_over_market():
    """Even when the market string could parse cleanly, the
    ``player_name`` field wins."""
    pick = {"market": "Wrong Name To Score",
            "player_name": "Correct Player"}
    assert _extract_player_name(pick) == "Correct Player"


def test_structured_player_field_takes_priority():
    pick = {"market": "Fallback Name Anytime Goal Scorer",
            "player": "From Structured"}
    assert _extract_player_name(pick) == "From Structured"


def test_structured_selection_field_stripped_of_action_verb():
    pick = {"selection": "Lionel Messi to score or assist"}
    assert _extract_player_name(pick) == "Lionel Messi"


def test_structured_pick_side_stripped():
    pick = {"pick_side": "Vinicius Junior anytime goal scorer"}
    assert _extract_player_name(pick) == "Vinicius Junior"


def test_structured_field_priority_ordering():
    """When multiple structured fields disagree, earlier keys win.

    Priority: player_name → player → selection → pick_side.
    """
    pick = {"player_name": "First",  "player": "Second",
            "selection":  "Third", "pick_side": "Fourth",
            "market": "market override attempt"}
    assert _extract_player_name(pick) == "First"


# ── 7. Non-truncation guards — real-world tricky names ──────────
@pytest.mark.parametrize("market,expected", [
    # Names containing action-verb tokens as substrings must not
    # be truncated.
    ("Assistir Player Anytime Goal Scorer",  "Assistir Player"),
    ("Score Watson First Goal Scorer",       "Score Watson"),
    ("Overend Doe To Assist",                "Overend Doe"),
    # Names with punctuation.
    ("Rúben Dias-Silva To Score",            "Rúben Dias-Silva"),
    # Compound / three-part names.
    ("Ángel Di María To Score or Assist",    "Ángel Di María"),
])
def test_names_containing_action_tokens_not_truncated(market, expected):
    assert _extract_player_name(market={"market": market}, **{}
                                 ) if False else _extract_player_name(
        {"market": market}) == expected


# ── 8. Selection field with over/under numeric line stripped ─────
@pytest.mark.parametrize("selection,expected", [
    ("John Doe Over 1.5",           "John Doe"),
    ("John Doe under 2.5 shots",    "John Doe"),
    ("John Doe to record 2 shots",  "John Doe"),
])
def test_selection_field_strips_over_under(selection, expected):
    pick = {"selection": selection}
    assert _extract_player_name(pick) == expected


# ── 9. Failure modes ─────────────────────────────────────────────
def test_empty_pick_returns_none():
    assert _extract_player_name({}) is None
    assert _extract_player_name({"market": ""}) is None
    assert _extract_player_name({"market": "   "}) is None


def test_unrecognisable_market_returns_none():
    """A market string we cannot parse must not silently accept the
    whole string as the player — it should return None so downstream
    can flag ``player_name_missing``."""
    pick = {"market": "Some Random Text Without Known Suffix"}
    assert _extract_player_name(pick) is None


# ── 10. End-to-end validator: reproduces P0-A dry-run failures ──
_ROSTER = {}   # club-only lookup (empty — these are international fixtures)
_FRESH = set()
_NT_LOOKUP = {
    "federico vinas": "Uruguay",
    "lionel messi":   "Argentina",
    "julian alvarez": "Argentina",
}
_NT_FRESH = set(_NT_LOOKUP.keys())


def test_end_to_end_federico_vinas_to_score_or_assist_now_verifies():
    pick = {"sport": "Soccer",
            "market": "Federico Vinas To Score or Assist",
            "event": "Uruguay @ Saudi Arabia"}
    v = validate_player_fixture_pick(
        pick, _ROSTER, fresh_roster_names=_FRESH or None,
        national_team_lookup=_NT_LOOKUP,
        fresh_national_team_names=_NT_FRESH)
    assert v["reason"] != REASON_PLAYER_NAME_MISSING
    assert v["verified"] is True
    assert v["player_team"] == "Uruguay"


def test_end_to_end_messi_to_score_or_assist_now_parses_player_name():
    pick = {"sport": "Soccer",
            "market": "Lionel Messi To Score or Assist",
            "event": "Algeria @ Argentina"}
    v = validate_player_fixture_pick(
        pick, _ROSTER, fresh_roster_names=_FRESH or None,
        national_team_lookup=_NT_LOOKUP,
        fresh_national_team_names=_NT_FRESH)
    assert v["reason"] != REASON_PLAYER_NAME_MISSING
    assert v["player"] == "Lionel Messi"


def test_end_to_end_alvarez_to_score_or_assist_now_parses_player_name():
    pick = {"sport": "Soccer",
            "market": "Julian Alvarez To Score or Assist",
            "event": "Algeria @ Argentina"}
    v = validate_player_fixture_pick(
        pick, _ROSTER, fresh_roster_names=_FRESH or None,
        national_team_lookup=_NT_LOOKUP,
        fresh_national_team_names=_NT_FRESH)
    assert v["reason"] != REASON_PLAYER_NAME_MISSING
    assert v["player"] == "Julian Alvarez"


# ── 11. Backward-compat — existing patterns still supported ─────
def test_existing_dash_pattern_still_supported():
    pick = {"sport": "Soccer",
            "market": "Leo Walta - Anytime Goal Scorer",
            "player_name": "Leo Walta",
            "event": "Inter Turku vs KuPS"}
    v = validate_player_fixture_pick(
        pick, {"leo walta": "Inter Turku"},
        fresh_roster_names={"leo walta"})
    assert v["verified"] is True


def test_locks_gate_still_strict_gt_85_after_p0b():
    """Sanity — P0-B is a parser change ONLY. Betting gate unchanged."""
    from services.main_board_eligibility import is_main_board_eligible
    assert is_main_board_eligible({"lock_score": 85.0}) is False
    assert is_main_board_eligible({"lock_score": 85.001}) is True
