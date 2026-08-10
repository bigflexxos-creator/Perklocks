"""FINAL SOCCER INTEGRITY CLEANUP (2026-08-11).

Locks in the tiny, targeted fixes for the remaining false rejections
identified by the P0-E dry-run:

  * Team/nation alias equivalence (USA ↔ United States,
    Beijing FC ↔ Beijing Guoan, and a handful of documented CSL
    club-rename variants).
  * Canonical player-name matching for accented / full-name
    variants already in ``player_identity``.
  * Endrick regression — the ESPN Portugal roster misfile must NOT
    hard-reject his Brazil pick.
"""
from __future__ import annotations

from services.player_team_fixture_validator import (
    validate_player_fixture_pick, _teams_match,
    _alias_equivalent, _lookup_with_alias, _norm,
    REASON_PLAYER_TEAM_MISMATCH,
    REASON_ROSTER_UNVERIFIED,
)


# ── 1. Team / nation aliases ─────────────────────────────────────
def test_usa_and_united_states_are_equivalent():
    assert _alias_equivalent(_norm("USA"), _norm("United States")) is True
    assert _teams_match("USA", ("Belgium", "United States")) is True
    assert _teams_match("United States", ("Belgium", "USA")) is True


def test_beijing_fc_and_beijing_guoan_are_equivalent():
    assert _teams_match("Beijing Guoan",
                         ("Shandong Taishan", "Beijing FC")) is True


def test_shanghai_and_shandong_variants():
    assert _teams_match("Shandong Luneng",
                         ("Shandong Taishan", "Beijing FC")) is True
    assert _teams_match("Shandong Luneng Taishan FC",
                         ("Shandong Taishan", "Beijing FC")) is True


def test_turkey_turkiye_and_korea_variants():
    assert _teams_match("Türkiye", ("Turkey", "Australia")) is True
    assert _teams_match("Korea Republic",
                         ("Portugal", "South Korea")) is True


# ── 2. Full-name / head-of-name lookup ───────────────────────────
def test_full_name_matches_last_name_in_lookup():
    """`Gonçalo Matias Ramos` must resolve to `Gonçalo Ramos`
    when only the shorter name is in the lookup."""
    lookup = {"goncalo ramos": "Portugal"}
    assert _lookup_with_alias("goncalo matias ramos", lookup) == "Portugal"


def test_short_name_matches_full_name_in_lookup():
    lookup = {"nicolas gonzalez iglesias": "Argentina"}
    assert _lookup_with_alias("nicolas gonzalez", lookup) == "Argentina"


def test_accents_stripped_before_lookup():
    # `_norm` strips accents; the raw lookup key can carry them.
    lookup = {_norm("Nicolás González"): "Argentina"}
    assert _lookup_with_alias(_norm("Nicolas Gonzalez"), lookup) == "Argentina"


# ── 3. Endrick regression ────────────────────────────────────────
def test_endrick_regression_override_verifies_brazil_fixture():
    """Even when ESPN's authoritative NT feed (wrongly) puts Endrick
    on Portugal's squad and citizenship agrees on Portugal, the
    curated NT-correction override must:

      * verify a Haiti @ Brazil pick,
      * NEVER produce ``player_team_mismatch``.
    """
    v = validate_player_fixture_pick(
        {"sport": "Soccer",
         "market": "Endrick Felipe Moreira de Sousa To Score or Assist",
         "event": "Haiti @ Brazil",
         "league": "FIFA World Cup · Props"},
        {},
        national_team_lookup={"endrick felipe moreira de sousa": "Portugal"},
        fresh_national_team_names={"endrick felipe moreira de sousa"},
        nationality_lookup={"endrick felipe moreira de sousa": "Portugal"})
    assert v["verified"] is True
    assert v["player_team"] == "Brazil"
    assert v.get("evidence") == "known_nt_correction"


def test_endrick_regression_short_form_also_matches_override():
    v = validate_player_fixture_pick(
        {"sport": "Soccer",
         "market": "Endrick To Score",
         "event": "Haiti @ Brazil",
         "league": "FIFA World Cup"},
        {},
        national_team_lookup={"endrick": "Portugal"},
        fresh_national_team_names={"endrick"},
        nationality_lookup={"endrick": "Portugal"})
    assert v["verified"] is True
    assert v["player_team"] == "Brazil"


def test_endrick_override_does_not_hard_reject_when_fixture_isnt_brazil():
    """Even if the fixture is Argentina @ France (Brazil not on the
    card), the override must NOT emit `team_mismatch` — instead
    return `roster_unverified`.  A stale ESPN Portugal record must
    never hard-reject."""
    v = validate_player_fixture_pick(
        {"sport": "Soccer",
         "market": "Endrick Anytime Goal Scorer",
         "event": "Argentina @ France",
         "league": "FIFA World Cup"},
        {},
        national_team_lookup={"endrick": "Portugal"},
        fresh_national_team_names={"endrick"},
        nationality_lookup={"endrick": "Portugal"})
    assert v["reason"] == REASON_ROSTER_UNVERIFIED
    assert v["reason"] != REASON_PLAYER_TEAM_MISMATCH


# ── 4. Regression — nothing else broke ───────────────────────────
def test_previous_regression_cases_still_pass():
    # P0-C: Messi on Argentina fixture verifies.
    v = validate_player_fixture_pick(
        {"sport": "Soccer",
         "market": "Lionel Messi Anytime Goal Scorer",
         "event": "Algeria @ Argentina"},
        {},
        national_team_lookup={"lionel messi": "Argentina"},
        fresh_national_team_names={"lionel messi"},
        nationality_lookup={"lionel messi": "Argentina"})
    assert v["verified"] is True
    # Wrong national team is still a hard mismatch (agreeing sources).
    v2 = validate_player_fixture_pick(
        {"sport": "Soccer",
         "market": "Lionel Messi Anytime Goal Scorer",
         "event": "France @ Spain"},
        {},
        national_team_lookup={"lionel messi": "Argentina"},
        fresh_national_team_names={"lionel messi"},
        nationality_lookup={"lionel messi": "Argentina"})
    assert v2["reason"] == REASON_PLAYER_TEAM_MISMATCH


def test_locks_gate_still_strict_gt_85_after_final_cleanup():
    from services.main_board_eligibility import is_main_board_eligible
    assert is_main_board_eligible({"lock_score": 85.0}) is False
    assert is_main_board_eligible({"lock_score": 85.001}) is True
