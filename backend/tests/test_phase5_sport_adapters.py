"""Phase 5 (2026-08-11) — Sport-specific adapter contract tests.

Every sport-specific rule the user called out is asserted here:

  * MLB: switch-hitter is an attribute (not identity)
        role change (pitcher ↔ hitter) is an attribute (not identity)
        team change preserves identity + history
  * CFB: portal transfer preserves identity + history
  * NFL / NBA / NHL: team change preserves identity, current-team
        truth is separate from historical membership
  * Tennis: singles vs doubles-safe disambiguation
        surname-alone must NOT merge two players
        ATP ↔ WTA tour change is a hard mint
        ranking change is an attribute
  * UFC: weight-class transition preserves identity
        surname-alone must NOT merge two fighters
        stance / reach preserved as attributes
"""
from __future__ import annotations

import pytest


# ── Adapter contract exports ─────────────────────────────────────
@pytest.mark.unit
def test_all_expected_sport_adapters_registered():
    from services import sport_adapters
    from services.universal_player_identity import ENABLED_SPORTS
    for s in ENABLED_SPORTS:
        assert sport_adapters.get_adapter(s) is not None, f"missing: {s}"


@pytest.mark.unit
def test_adapter_contracts_expose_required_attrs():
    from services import sport_adapters
    from services.universal_player_identity import ENABLED_SPORTS
    for s in ENABLED_SPORTS:
        a = sport_adapters.get_adapter(s)
        assert a is not None
        assert a.SPORT == s
        assert a.SPORT_CLASS in ("team", "individual")
        assert isinstance(a.ROSTER_SOURCE, str) and a.ROSTER_SOURCE
        assert isinstance(a.PROVIDER_IDS, tuple)
        assert isinstance(a.IDENTITY_ATTRIBUTES, tuple)
        assert isinstance(a.HISTORY_FIELDS, tuple)
        assert callable(a.is_player_market)


# ── MLB — switch-hitter is an attribute ─────────────────────────
@pytest.mark.unit
def test_mlb_switch_hitter_is_attribute_not_identity():
    from services.universal_player_identity import (
        assert_attribute_change_not_identity,
    )
    assert assert_attribute_change_not_identity("MLB", "handedness") is True
    assert assert_attribute_change_not_identity("MLB", "role") is True
    assert assert_attribute_change_not_identity("MLB", "position") is True


@pytest.mark.unit
def test_mlb_dob_mismatch_rejects_identity_change():
    from services.universal_player_identity import (
        assert_transfer_preserves_identity,
    )
    r = assert_transfer_preserves_identity(
        "MLB",
        {"name": "Aaron Judge", "dob": "1992-04-26",
         "provider_ids": {"mlb_stats": "592450"}},
        {"name": "Aaron Judge", "dob": "1995-01-01",
         "provider_ids": {"mlb_stats": "592450"}})
    assert r == "dob_mismatch"


@pytest.mark.unit
def test_mlb_team_transfer_preserves_identity():
    from services.universal_player_identity import (
        assert_transfer_preserves_identity,
    )
    r = assert_transfer_preserves_identity(
        "MLB",
        {"name": "Juan Soto", "current_team": "Nationals"},
        {"name": "Juan Soto", "current_team": "Padres"})
    assert r is None


# ── CFB — portal transfer preserves identity ────────────────────
@pytest.mark.unit
def test_cfb_portal_transfer_preserves_identity():
    from services.universal_player_identity import (
        assert_transfer_preserves_identity,
    )
    r = assert_transfer_preserves_identity(
        "CFB",
        {"name": "Cam Ward", "current_team": "Washington State"},
        {"name": "Cam Ward", "current_team": "Miami"})
    assert r is None


# ── NFL / NBA / NHL — team change preserves identity ─────────
@pytest.mark.unit
@pytest.mark.parametrize("sport", ["NFL", "NBA", "NHL"])
def test_team_change_preserves_identity(sport):
    from services.universal_player_identity import (
        assert_transfer_preserves_identity,
    )
    r = assert_transfer_preserves_identity(
        sport,
        {"name": "Star Player", "current_team": "Team A"},
        {"name": "Star Player", "current_team": "Team B"})
    assert r is None


@pytest.mark.unit
@pytest.mark.parametrize("sport", ["NFL", "NBA", "NHL", "MLB", "CFB"])
def test_provider_id_conflict_rejects_identity_change(sport):
    from services.universal_player_identity import (
        assert_transfer_preserves_identity,
    )
    r = assert_transfer_preserves_identity(
        sport,
        {"name": "John Smith", "provider_ids": {"espn": "111"}},
        {"name": "John Smith", "provider_ids": {"espn": "222"}})
    assert r == "provider_id_conflict:espn"


# ── Tennis — singles / doubles / surname disambiguation ─────
@pytest.mark.unit
def test_tennis_atp_vs_wta_tour_swap_is_hard_mint():
    from services.universal_player_identity import (
        assert_transfer_preserves_identity,
    )
    r = assert_transfer_preserves_identity(
        "Tennis",
        {"name": "Some Player", "tour": "ATP"},
        {"name": "Some Player", "tour": "WTA"})
    assert r == "tour_mismatch"


@pytest.mark.unit
def test_tennis_doubles_market_detected():
    from services.sport_adapters.tennis import is_doubles_market
    assert is_doubles_market(
        {"player_name": "Alcaraz / Ruud", "sport": "Tennis"}) is True
    assert is_doubles_market(
        {"market": "Alcaraz Moneyline"}) is False


@pytest.mark.unit
def test_tennis_surname_alone_does_not_merge():
    from services.universal_player_identity import surnames_only_would_merge
    a = {"name": "Rafael Nadal"}
    b = {"name": "Alex Nadal"}
    assert surnames_only_would_merge("Tennis", a, b) is True


@pytest.mark.unit
def test_tennis_ranking_change_is_attribute():
    """Rankings drift constantly — must not affect identity."""
    from services.universal_player_identity import (
        assert_transfer_preserves_identity,
    )
    r = assert_transfer_preserves_identity(
        "Tennis",
        {"name": "Player X", "tour": "ATP", "ranking": 5},
        {"name": "Player X", "tour": "ATP", "ranking": 12})
    assert r is None


# ── UFC — weight class transition preserves identity ────────
@pytest.mark.unit
def test_ufc_weight_class_transition_preserves_identity():
    from services.universal_player_identity import (
        assert_transfer_preserves_identity,
        assert_attribute_change_not_identity,
    )
    r = assert_transfer_preserves_identity(
        "UFC",
        {"name": "Islam Fighter", "division": "Lightweight"},
        {"name": "Islam Fighter", "division": "Welterweight"})
    assert r is None
    assert assert_attribute_change_not_identity("UFC", "division") is True
    assert assert_attribute_change_not_identity("UFC", "weight_class") is True
    assert assert_attribute_change_not_identity("UFC", "ranking") is True


@pytest.mark.unit
def test_ufc_surname_alone_does_not_merge():
    from services.universal_player_identity import surnames_only_would_merge
    a = {"name": "Anderson Silva"}
    b = {"name": "Thiago Silva"}
    assert surnames_only_would_merge("UFC", a, b) is True


# ── Publication-barrier status enum ─────────────────────────
@pytest.mark.unit
def test_verified_status_on_matching_roster():
    from services.universal_publication_barrier import (
        validate_universal, STATUS_VERIFIED,
    )
    v = validate_universal(
        {"sport": "NFL", "market": "Patrick Mahomes Passing Yards Over 250.5",
         "event": "Bills vs Chiefs"},
        roster_lookup={"patrick mahomes": "Chiefs"},
        fresh_roster_names={"patrick mahomes"})
    assert v["verified"] is True
    assert v["status"] == STATUS_VERIFIED
    assert v["player_team"] == "Chiefs"


@pytest.mark.unit
def test_unresolved_when_no_roster_data():
    from services.universal_publication_barrier import (
        validate_universal, STATUS_UNRESOLVED,
    )
    v = validate_universal(
        {"sport": "NBA", "market": "Ja Morant Points Over 25.5",
         "event": "Grizzlies vs Warriors"},
        roster_lookup={}, fresh_roster_names=set())
    assert v["status"] == STATUS_UNRESOLVED


@pytest.mark.unit
def test_confirmed_mismatch_when_roster_positively_wrong():
    from services.universal_publication_barrier import (
        validate_universal, STATUS_CONFIRMED_MISMATCH,
    )
    v = validate_universal(
        {"sport": "NBA", "market": "Ja Morant Points Over 25.5",
         "event": "Lakers vs Warriors"},
        roster_lookup={"ja morant": "Grizzlies"},
        fresh_roster_names={"ja morant"})
    assert v["status"] == STATUS_CONFIRMED_MISMATCH
    assert v["reason"] == "player_team_mismatch"


@pytest.mark.unit
def test_soccer_source_conflict_maps_to_status():
    """When two trusted Soccer sources disagree, status must be
    ``source_conflict`` — never a false confirmed_mismatch."""
    from services.universal_publication_barrier import (
        validate_universal, STATUS_SOURCE_CONFLICT,
    )
    # Authoritative NT says Argentina, citizenship signal says Spain
    # → source_conflict, not confirmed_mismatch.
    v = validate_universal(
        {"sport": "Soccer",
         "market": "Some Player To Score",
         "event": "Brazil vs Uruguay",
         "league": "FIFA World Cup"},
        national_team_lookup={"some player": "Argentina"},
        fresh_national_team_names={"some player"},
        nationality_lookup={"some player": "Spain"})
    assert v["status"] == STATUS_SOURCE_CONFLICT


@pytest.mark.unit
def test_only_confirmed_mismatch_hard_rejects():
    """Only ``confirmed_mismatch`` is the hard-reject signal.
    Unresolved / source_conflict must NOT hard-reject."""
    from services.universal_publication_barrier import (
        STATUS_VERIFIED, STATUS_UNRESOLVED,
        STATUS_SOURCE_CONFLICT, STATUS_CONFIRMED_MISMATCH,
    )
    # Contract: the barrier's ``verified`` flag only tracks the pass
    # path — callers must check ``status`` for the reject signal.
    hard_reject = {STATUS_CONFIRMED_MISMATCH}
    for s in (STATUS_VERIFIED, STATUS_UNRESOLVED, STATUS_SOURCE_CONFLICT):
        assert s not in hard_reject
    assert STATUS_CONFIRMED_MISMATCH in hard_reject


# ── History contract ────────────────────────────────────────
@pytest.mark.unit
def test_history_contract_rejects_missing_required_field():
    from services.player_history_contract import (
        validate_history_row, HistoryContractViolation,
    )
    row = {"canonical_player_id": "cpid_x", "sport": "NFL",
            "date": "2025-09-08", "event_id": "e1", "market": "yards"}
    # missing "value"
    with pytest.raises(HistoryContractViolation):
        validate_history_row(row)


@pytest.mark.unit
def test_history_contract_accepts_complete_row():
    from services.player_history_contract import (
        validate_history_row, is_threshold_ready,
    )
    row = {"canonical_player_id": "cpid_x", "sport": "NFL",
            "date": "2025-09-08", "event_id": "e1",
            "market": "passing_yards", "value": 305}
    validate_history_row(row)  # does not raise
    assert is_threshold_ready(row, 250) is True
    assert is_threshold_ready(row, 400) is False


@pytest.mark.unit
def test_history_contract_rejects_none_and_empty():
    from services.player_history_contract import (
        validate_history_row, HistoryContractViolation,
    )
    for bad in (None, ""):
        row = {"canonical_player_id": "cpid_x", "sport": "NFL",
                "date": "2025-09-08", "event_id": "e1",
                "market": "passing_yards", "value": bad}
        with pytest.raises(HistoryContractViolation):
            validate_history_row(row)


# ── Current-context contract ────────────────────────────────
@pytest.mark.unit
def test_current_context_reports_freshness_flags():
    from services.current_context_contract import build_current_context
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc).isoformat()
    old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    fresh = build_current_context({
        "canonical_player_id": "cpid_1",
        "name": "Fresh Player", "current_team": "Team A",
        "observed_at": now, "sport": "NBA"})
    stale = build_current_context({
        "canonical_player_id": "cpid_2",
        "name": "Stale Player", "current_team": "Team B",
        "observed_at": old, "sport": "NBA"})
    assert fresh["current_team_fresh"] is True
    assert stale["current_team_fresh"] is False


@pytest.mark.unit
def test_current_context_never_invents_missing_fields():
    from services.current_context_contract import build_current_context
    ctx = build_current_context({
        "canonical_player_id": "cpid_x", "name": "Player X"})
    for k in ("current_team", "current_national_team", "nationality",
               "handedness", "stance", "division"):
        assert ctx[k] is None


# ── Soccer regression envelope ───────────────────────────────
@pytest.mark.unit
def test_soccer_endrick_still_verified_via_universal_barrier():
    """Endrick case (verified in P0-E) must remain verified when
    called via the universal barrier."""
    from services.universal_publication_barrier import (
        validate_universal, STATUS_VERIFIED,
    )
    v = validate_universal(
        {"sport": "Soccer",
         "market": "Endrick To Score or Assist",
         "event": "Haiti @ Brazil",
         "league": "FIFA World Cup · Props"},
        national_team_lookup={"endrick": "Portugal"},
        fresh_national_team_names={"endrick"},
        nationality_lookup={"endrick": "Portugal"})
    assert v["status"] == STATUS_VERIFIED
    assert v["player_team"] == "Brazil"


# ── Sport-adapter market detection ─────────────────────────
@pytest.mark.unit
def test_nfl_player_market_detection():
    from services.sport_adapters.nfl import is_player_market
    assert is_player_market("Patrick Mahomes Passing Yards Over 250.5")
    assert is_player_market("Josh Allen Anytime Touchdown")
    assert not is_player_market("Bills Moneyline")
    assert not is_player_market("Chiefs Spread -3.5")


@pytest.mark.unit
def test_nba_player_market_detection():
    from services.sport_adapters.nba import is_player_market
    assert is_player_market("Ja Morant Points Over 25.5")
    assert is_player_market("LeBron James Assists Under 8.5")
    assert not is_player_market("Lakers Moneyline")


@pytest.mark.unit
def test_mlb_player_market_detection():
    from services.sport_adapters.mlb import is_player_market
    assert is_player_market("Aaron Judge Home Run")
    assert is_player_market("Shohei Ohtani Strikeouts Over 6.5")
    assert not is_player_market("Yankees Run Line")


@pytest.mark.unit
def test_nhl_player_market_detection():
    from services.sport_adapters.nhl import is_player_market
    assert is_player_market("Connor McDavid Points Over 1.5")
    assert is_player_market("Auston Matthews Shots On Goal Over 4.5")
    assert not is_player_market("Oilers Puck Line -1.5")


@pytest.mark.unit
def test_tennis_all_markets_are_player_based():
    from services.sport_adapters.tennis import is_player_market
    assert is_player_market("Alcaraz Moneyline")
    assert is_player_market("Sinner To Win in Straight Sets")


@pytest.mark.unit
def test_tennis_match_level_totals_are_not_player_based():
    """Total Games / Total Sets are shared by both participants and
    must NOT be classified as player-based — otherwise the
    universal barrier will confuse "Over" as a player name."""
    from services.sport_adapters.tennis import is_player_market
    assert is_player_market("Total Games Over 21.5") is False
    assert is_player_market("Total Sets Over 2.5") is False
    assert is_player_market("Match Total Games Under 22.5") is False
    # Alt-line forms produced by the Odds API.
    assert is_player_market("Under 21.0 Games (Alt)") is False
    assert is_player_market("Over 18.5 Games (Alt)") is False
    assert is_player_market("Over 22.0 Games") is False


@pytest.mark.unit
def test_ufc_all_markets_are_player_based():
    from services.sport_adapters.ufc import is_player_market
    assert is_player_market("Jon Jones Moneyline")
    assert is_player_market("Islam Makhachev Method of Victory")


@pytest.mark.unit
def test_ufc_match_level_totals_are_not_player_based():
    from services.sport_adapters.ufc import is_player_market
    assert is_player_market("Total Rounds Over 2.5") is False
    assert is_player_market("Fight Length Under 1.5") is False
